"""Tareas programadas — RCOF diario (22:30) + recordatorio mensual IECV (día 1).

Se ejecuta como background task dentro del proceso FastAPI (asyncio).
Itera todas las empresas activas.

Multi-tenant: accede a cada BD de empresa de forma independiente,
sin requerir contexto HTTP ni autenticacion.

Tareas:
- **RCOF diario**: genera+envia el RCOF del dia para cada empresa que
  tenga boletas emitidas. Corre a las 22:30 (Chile cierra tiendas a esa hora).
- **Recordatorio IECV mensual**: el día 1 de cada mes a las 09:00 emite un
  log WARNING por cada empresa activa recordando enviar el Libro de Ventas
  (y Libro de Guías si emite T52) del mes anterior. El envío NO se automatiza
  aquí porque:
  a) El Libro de Guías siempre requiere FolioNotificacion (número de atención
     del SII), que debe obtenerse manualmente en el portal SII antes del día 10.
  b) El Libro de Ventas puede enviarse vía POST /api/libros/ventas/generar.
"""
import asyncio
import logging
import os
import base64
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from crumbpos.db.multi_tenant import (
    listar_empresas,
    get_empresa_db_session,
    EmpresaRegistro,
    get_scheduler_estado,
    set_scheduler_estado,
)
from crumbpos.db.models import Empresa, DteEmitido, RcofDiario
from crumbpos.api.services.rcof_service import ServicioRCOF
from crumbpos.config import settings

logger = logging.getLogger(__name__)

# Timezone canónico para todos los cálculos de scheduling.
# El SII opera en hora local de Chile continental. Anclamos los cálculos
# de "próxima ejecución" a este tz para evitar drift en los dos cambios
# de horario al año (abril y septiembre), durante los cuales un cálculo
# basado en datetime naive puede desplazar la ejecución ±1 hora.
TZ_CHILE = ZoneInfo("America/Santiago")

# Hora de envio diario (configurable via env)
# 22:30 por defecto: en Chile el cambio de hora es ±1h,
# a las 22:30 las tiendas estan cerradas y si el reloj
# salta una hora (23:30) sigue siendo el mismo dia.
RCOF_HORA = int(os.getenv("RCOF_HORA", "22"))
RCOF_MINUTO = int(os.getenv("RCOF_MINUTO", "30"))


def _generar_rcof_empresa(registro: EmpresaRegistro, fecha: date) -> dict:
    """Genera y envia RCOF para una empresa en una fecha.

    Abre sesion a la BD del tenant, carga el certificado,
    y usa ServicioRCOF para generar+firmar+enviar.

    Args:
        registro: EmpresaRegistro del master.db
        fecha: Fecha del RCOF

    Returns:
        dict con resultado (ok, track_id, error, etc.)
    """
    db = get_empresa_db_session(registro.rut, registro.ambiente_activo)
    try:
        empresa = db.query(Empresa).filter(Empresa.rut == registro.rut).first()
        if not empresa:
            return {"ok": False, "error": f"Empresa {registro.rut} no encontrada en BD tenant"}

        if not empresa.fecha_resolucion or not empresa.cert_rut_firmante:
            return {"ok": False, "error": "Sin fecha_resolucion o cert_rut_firmante"}

        # Resolver ruta al certificado
        cert_path = None
        cert_password = empresa.cert_password
        tmp_file = None

        if empresa.cert_data:
            pfx_bytes = base64.b64decode(empresa.cert_data)
            fd, tmp_pfx = tempfile.mkstemp(suffix=".pfx")
            os.write(fd, pfx_bytes)
            os.close(fd)
            cert_path = tmp_pfx
            tmp_file = tmp_pfx
        elif empresa.cert_path and Path(empresa.cert_path).exists():
            cert_path = empresa.cert_path

        if not cert_path:
            base_dir = Path(settings.BASE_DIR)
            for d in [base_dir / "certificados", base_dir / "cert"]:
                if d.is_dir():
                    pfx_files = list(d.glob("*.pfx")) + list(d.glob("*.p12"))
                    if pfx_files:
                        cert_path = str(pfx_files[0])
                        break

        if not cert_path:
            return {"ok": False, "error": "Certificado .pfx no encontrado"}

        try:
            servicio = ServicioRCOF(
                empresa=empresa,
                cert_path=cert_path,
                cert_password=cert_password,
            )
            return servicio.generar_rcof_diario(db=db, fecha=fecha, enviar=True)
        finally:
            if tmp_file and os.path.exists(tmp_file):
                os.unlink(tmp_file)

    except Exception as e:
        logger.error("Error RCOF para %s: %s", registro.rut, e, exc_info=True)
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def ejecutar_rcof_todas_empresas(fecha: date | None = None) -> list[dict]:
    """Genera RCOF del dia para todas las empresas activas.

    Puede llamarse desde el scheduler o manualmente.

    Args:
        fecha: Fecha del RCOF (default: hoy en hora Chile).
            CRÍTICO: si el server corre en UTC y son las 22:30 CLT
            (= 01:30 UTC del día siguiente), `date.today()` sin TZ devuelve
            el día siguiente y el RCOF dispara para un día sin boletas.
            Anclamos siempre a `America/Santiago`.

    Returns:
        Lista de resultados por empresa.
    """
    if fecha is None:
        fecha = datetime.now(TZ_CHILE).date()

    registros = listar_empresas()
    resultados = []

    for reg in registros:
        if not reg.activa:
            continue

        logger.info("RCOF diario: procesando %s (%s)...", reg.rut, reg.razon_social)
        resultado = _generar_rcof_empresa(reg, fecha)
        resultado["empresa_rut"] = reg.rut
        resultado["empresa_nombre"] = reg.razon_social
        resultados.append(resultado)

        if resultado.get("ok"):
            boletas = resultado.get("total_boletas", 0)
            track = resultado.get("track_id")
            if boletas:
                logger.info(
                    "  -> %s: %d boletas, track_id=%s",
                    reg.rut, boletas, track,
                )
            else:
                logger.info("  -> %s: sin boletas hoy", reg.rut)
        else:
            logger.warning(
                "  -> %s: error — %s",
                reg.rut, resultado.get("error"),
            )

    return resultados


# ══════════════════════════════════════════════════════════════
# A6 — Reintento intra-día + backfill al boot (RCOF)
# ══════════════════════════════════════════════════════════════
#
# Problema: si el SII está caído entre las 22:30 y las 22:34 (ventana
# de disparo del scheduler), el RCOF del día se pierde.  Boletas emitidas
# después de las 22:30 también quedan sin reporte hasta el intento del día
# siguiente, momento en el que el SII ya no acepta RCOFs viejos.
#
# Solución en tres capas:
# 1. _empresa_necesita_reintento_rcof  — comprueba en BD si falta enviado.
# 2. reintentar_rcof_fallidos           — itera empresas y reintenta.
# 3. _loop_reintentos_rcof_intraday     — llama al anterior cada 30 min
#    desde las 22:30 hasta las 23:55 del mismo día.
# 4. ejecutar_rcof_backfill             — al arrancar, revisa los últimos
#    7 días por si algún RCOF quedó en error_envio.

_RCOF_REINTENTO_LIMITE_HORA = 23
_RCOF_REINTENTO_LIMITE_MINUTO = 55
_RCOF_REINTENTO_INTERVALO_SEG = 30 * 60  # 30 minutos entre reintentos


def _empresa_necesita_reintento_rcof(registro: EmpresaRegistro, fecha: date) -> bool:
    """Determina si la empresa necesita reintento de RCOF para *fecha*.

    Devuelve ``True`` si:
    - Existen boletas (T39/T41) del día **y**
    - No hay ningún ``RcofDiario`` con ``estado_sii='enviado'`` para ese día.

    Cubre tanto el caso "registro con error_envio" como el caso "crash antes
    de guardar" (sin registro alguno en la tabla).
    """
    db = get_empresa_db_session(registro.rut, registro.ambiente_activo)
    try:
        tiene_boletas = (
            db.query(DteEmitido)
            .filter(
                DteEmitido.tipo_dte.in_([39, 41]),
                DteEmitido.fecha_emision == fecha,
            )
            .limit(1)
            .count() > 0
        )
        if not tiene_boletas:
            return False

        rcof_enviado = (
            db.query(RcofDiario)
            .filter(
                RcofDiario.fecha == fecha,
                RcofDiario.estado_sii == "enviado",
            )
            .first()
        )
        return rcof_enviado is None
    except Exception as exc:
        logger.warning(
            "No se pudo verificar RCOF de %s para %s: %s",
            registro.rut, fecha, exc,
        )
        return False
    finally:
        db.close()


def reintentar_rcof_fallidos(fecha: date) -> list[dict]:
    """Reintenta el RCOF de *fecha* para empresas sin envío exitoso.

    Detecta dos situaciones:
    - Existe un ``RcofDiario`` con ``estado_sii='error_envio'``.
    - Hay boletas del día pero ningún registro con ``estado_sii='enviado'``
      (crash antes de persistir el resultado).

    Returns:
        Lista de resultados para las empresas que se reintentaron.
        Vacía si no hubo nada que reintentar.
    """
    resultados = []
    for reg in listar_empresas():
        if not reg.activa:
            continue
        if not _empresa_necesita_reintento_rcof(reg, fecha):
            continue

        logger.info(
            "RCOF reintento: %s (%s) para %s",
            reg.rut, reg.razon_social, fecha,
        )
        resultado = _generar_rcof_empresa(reg, fecha)
        resultado["empresa_rut"] = reg.rut
        resultado["empresa_nombre"] = reg.razon_social
        resultado["es_reintento"] = True
        resultados.append(resultado)

        if resultado.get("ok"):
            logger.info(
                "  -> %s: reintento OK, track_id=%s",
                reg.rut, resultado.get("track_id"),
            )
        else:
            logger.warning(
                "  -> %s: reintento fallido — %s",
                reg.rut, resultado.get("error"),
            )

    return resultados


def ejecutar_rcof_backfill(dias: int = 7) -> list[dict]:
    """Al arrancar, reintenta RCOFs de los últimos N días que no se enviaron.

    Revisa desde ayer hacia atrás hasta ``dias`` días.  No procesa el día
    actual (eso es responsabilidad del loop diario de las 22:30).

    Args:
        dias: Días hacia atrás a revisar (default 7).

    Returns:
        Lista consolidada de resultados de todos los reintentos efectuados.
    """
    hoy = datetime.now(TZ_CHILE).date()
    todos: list[dict] = []

    for delta in range(1, dias + 1):
        fecha = hoy - timedelta(days=delta)
        reint = reintentar_rcof_fallidos(fecha)
        for r in reint:
            r["backfill_fecha"] = fecha.isoformat()
        todos.extend(reint)

    if todos:
        ok = sum(1 for r in todos if r.get("ok"))
        err = sum(1 for r in todos if not r.get("ok"))
        logger.info(
            "RCOF backfill (últimos %d días): %d OK, %d errores",
            dias, ok, err,
        )
    else:
        logger.info("RCOF backfill: sin pendientes en los últimos %d días", dias)

    return todos


async def _loop_reintentos_rcof_intraday(fecha: date) -> None:
    """Reintentos intra-día: cada 30 min desde el disparo inicial hasta las 23:55.

    Se llama justo después de ``ejecutar_rcof_todas_empresas``.  Termina si:
    - Se supera el límite horario (23:55).
    - No quedan empresas con RCOF pendiente.
    - El task es cancelado (shutdown).
    """
    while True:
        ahora = datetime.now(TZ_CHILE)
        limite = ahora.replace(
            hour=_RCOF_REINTENTO_LIMITE_HORA,
            minute=_RCOF_REINTENTO_LIMITE_MINUTO,
            second=0,
            microsecond=0,
        )

        if ahora >= limite:
            break

        segundos_restantes = (limite - ahora).total_seconds()
        espera = min(_RCOF_REINTENTO_INTERVALO_SEG, segundos_restantes)

        await asyncio.sleep(espera)

        # Re-evaluar límite después del sleep (podría haber pasado DST)
        ahora = datetime.now(TZ_CHILE)
        limite = ahora.replace(
            hour=_RCOF_REINTENTO_LIMITE_HORA,
            minute=_RCOF_REINTENTO_LIMITE_MINUTO,
            second=0,
            microsecond=0,
        )
        if ahora >= limite:
            break

        reint = reintentar_rcof_fallidos(fecha)
        if not reint:
            logger.info("RCOF reintentos intra-día: sin pendientes para %s", fecha)
            break

        ok = sum(1 for r in reint if r.get("ok"))
        err = sum(1 for r in reint if not r.get("ok"))
        logger.info(
            "RCOF reintentos intra-día (%s): %d OK, %d errores",
            fecha, ok, err,
        )
        if err == 0:
            break  # Todos resueltos


async def _backfill_rcof_al_boot() -> None:
    """Reintenta RCOFs fallidos de los últimos 7 días al arrancar el servidor.

    Espera 30 s para que la app complete su inicialización (sesiones de BD,
    super-admin, etc.) antes de intentar acceder a las BDs de tenant.
    """
    try:
        await asyncio.sleep(30)
        logger.info("=== RCOF backfill al boot: verificando últimos 7 días ===")
        ejecutar_rcof_backfill(dias=7)
    except asyncio.CancelledError:
        logger.info("RCOF backfill al boot cancelado")
        raise
    except Exception as exc:
        logger.error("Error en RCOF backfill al boot: %s", exc, exc_info=True)


def _segundos_hasta(hora: int, minuto: int) -> float:
    """Calcula segundos hasta la próxima ocurrencia de hora:minuto en Chile.

    Anclado a TZ_CHILE (America/Santiago) para evitar drift durante los
    cambios de horario (abril/septiembre). Si el servidor corre en UTC
    o en otra zona, `datetime.now(TZ_CHILE)` devuelve el instante
    correcto en hora chilena.
    """
    ahora = datetime.now(TZ_CHILE)
    objetivo = ahora.replace(hour=hora, minute=minuto, second=0, microsecond=0)

    if ahora >= objetivo:
        # Ya pasó hoy, programar para mañana
        objetivo += timedelta(days=1)

    return (objetivo - ahora).total_seconds()


async def _loop_rcof_diario():
    """Loop infinito que ejecuta el RCOF diario a la hora configurada.

    Calcula el tiempo hasta la proxima ejecucion, duerme,
    ejecuta, y repite.
    """
    logger.info(
        "Scheduler RCOF iniciado — ejecucion diaria a las %02d:%02d",
        RCOF_HORA, RCOF_MINUTO,
    )

    while True:
        try:
            espera = _segundos_hasta(RCOF_HORA, RCOF_MINUTO)
            proxima = datetime.now(TZ_CHILE) + timedelta(seconds=espera)
            logger.info(
                "Proxima ejecucion RCOF: %s (en %.0f min)",
                proxima.strftime("%Y-%m-%d %H:%M %Z"),
                espera / 60,
            )

            await asyncio.sleep(espera)

            # Capturar la fecha DESPUÉS de despertar (en Chile puede ser otro día
            # si el server estaba en UTC y dormimos cerca de la medianoche).
            fecha_rcof = datetime.now(TZ_CHILE).date()
            logger.info("=== Ejecutando RCOF diario %s ===", fecha_rcof)
            resultados = ejecutar_rcof_todas_empresas(fecha=fecha_rcof)

            ok = sum(1 for r in resultados if r.get("ok"))
            err = sum(1 for r in resultados if not r.get("ok"))
            logger.info(
                "=== RCOF diario completado: %d OK, %d errores ===",
                ok, err,
            )

            # A6: reintentos intra-día cada 30 min hasta las 23:55
            if err > 0 or any(r.get("estado_sii") == "error_envio" for r in resultados):
                logger.info(
                    "RCOF: %d empresas con error, iniciando reintentos hasta 23:55",
                    err,
                )
                await _loop_reintentos_rcof_intraday(fecha_rcof)

        except asyncio.CancelledError:
            logger.info("Scheduler RCOF detenido")
            break
        except Exception as e:
            logger.error("Error en loop RCOF: %s", e, exc_info=True)
            # Esperar 5 minutos antes de reintentar
            await asyncio.sleep(300)


# ══════════════════════════════════════════════════════════════
# Recordatorio mensual IECV (Libros de Compras, Ventas y Guías)
# ══════════════════════════════════════════════════════════════
#
# El SII exige el envío del Libro de Ventas y Libro de Guías
# antes del día 10 del mes siguiente.
# - Libro de Ventas (T33/T34/T56/T61) → POST /api/libros/ventas/generar
#   con folio_notificacion=0 (modo MENSUAL de LibroCV; NO aplica a guías).
# - Libro de Guías (T52) → POST /api/libros/guias/generar
#   con folio_notificacion=<N°Atención obtenido del portal SII>.
#   NO se puede automatizar sin el número de atención.
#
# Este scheduler emite WARNING el día 1 de cada mes a las 09:00 para
# cada empresa activa que tenga DTEs del mes anterior.
# El admin debe ver el log y ejecutar el envío correspondiente.


def _mes_anterior(hoy: date) -> str:
    """Retorna el periodo anterior en formato 'YYYY-MM'."""
    if hoy.month == 1:
        return f"{hoy.year - 1}-12"
    return f"{hoy.year}-{hoy.month - 1:02d}"


def _empresa_tiene_dtes_periodo(registro: EmpresaRegistro, periodo: str, tipos: list[int]) -> bool:
    """Comprueba si la empresa tiene DTEs de los tipos indicados en el periodo."""
    año, mes = periodo.split("-")
    db = get_empresa_db_session(registro.rut, registro.ambiente_activo)
    try:
        count = (
            db.query(DteEmitido)
            .filter(
                DteEmitido.tipo_dte.in_(tipos),
                DteEmitido.fecha_emision >= f"{año}-{mes}-01",
                DteEmitido.fecha_emision < _next_month(año, mes),
            )
            .count()
        )
        return count > 0
    except Exception:
        return False
    finally:
        db.close()


def _next_month(año: str, mes: str) -> str:
    """Retorna el primer día del mes siguiente en formato 'YYYY-MM-DD'."""
    m = int(mes)
    y = int(año)
    if m == 12:
        return f"{y + 1}-01-01"
    return f"{y}-{m + 1:02d}-01"


def recordar_iecv_mensual():
    """Emite logs de recordatorio para el envío del IECV del mes anterior.

    Itera todas las empresas activas. Para cada una que tenga DTEs del mes
    anterior, emite un WARNING indicando qué libros deben enviarse y cómo.

    A7: tras emitir los avisos, persiste el periodo en ``SchedulerEstado``
    con la clave ``iecv_ultimo_periodo_recordado``. Esto permite el catch-up
    al boot: si el server estuvo caído el día 1 a las 09:00, al reiniciar
    detecta que el periodo aún no fue recordado y dispara aquí.
    """
    # Anclar a TZ_CHILE para evitar que a las 22:30 CLT (= 01:30 UTC+1)
    # date.today() devuelva el día siguiente.
    hoy = datetime.now(TZ_CHILE).date()
    periodo = _mes_anterior(hoy)
    registros = listar_empresas()

    for reg in registros:
        if not reg.activa:
            continue

        # Tipos para Libro de Ventas
        tiene_ventas = _empresa_tiene_dtes_periodo(reg, periodo, [33, 34, 56, 61])
        # Tipos para Libro de Guías
        tiene_guias = _empresa_tiene_dtes_periodo(reg, periodo, [52])

        if tiene_ventas:
            logger.warning(
                "IECV PENDIENTE — %s (%s): Libro de Ventas para %s. "
                "Enviar vía POST /api/libros/ventas/generar "
                "{\"periodo\": \"%s\", \"enviar\": true}. "
                "Plazo: día 10 del mes en curso.",
                reg.rut, reg.razon_social, periodo, periodo,
            )

        if tiene_guias:
            logger.warning(
                "IECV PENDIENTE — %s (%s): Libro de Guías para %s. "
                "Requiere FolioNotificacion del portal SII "
                "(https://zeus.sii.cl/AUT2/AS/accAut.html) ANTES de enviar. "
                "Enviar vía POST /api/libros/guias/generar con folio_notificacion=<N>. "
                "Plazo: día 10 del mes en curso.",
                reg.rut, reg.razon_social, periodo,
            )

        if not tiene_ventas and not tiene_guias:
            logger.info(
                "IECV: %s (%s) sin DTEs en %s — no requiere envío.",
                reg.rut, reg.razon_social, periodo,
            )

    # A7: marcar el periodo como recordado para que el catch-up al boot
    # no lo vuelva a disparar si el server reinicia durante el mismo mes.
    try:
        set_scheduler_estado("iecv_ultimo_periodo_recordado", periodo)
    except Exception as exc:
        logger.warning("No se pudo persistir estado IECV: %s", exc)


# Hora del recordatorio mensual (día 1, 09:00)
IECV_HORA = int(os.getenv("IECV_HORA", "9"))
IECV_MINUTO = int(os.getenv("IECV_MINUTO", "0"))


async def _loop_iecv_mensual():
    """Loop que emite el recordatorio mensual el día 1 de cada mes a las 09:00."""
    logger.info(
        "Scheduler IECV mensual iniciado — recordatorio el día 1 de cada mes a las %02d:%02d",
        IECV_HORA, IECV_MINUTO,
    )

    while True:
        try:
            ahora = datetime.now(TZ_CHILE)
            # Próximo día 1 del mes siguiente a las IECV_HORA:IECV_MINUTO (hora Chile)
            if ahora.month == 12:
                proximo = datetime(ahora.year + 1, 1, 1, IECV_HORA, IECV_MINUTO, tzinfo=TZ_CHILE)
            else:
                proximo = datetime(ahora.year, ahora.month + 1, 1, IECV_HORA, IECV_MINUTO, tzinfo=TZ_CHILE)

            # Si todavía no llegamos al día 1 de este mes a la hora indicada
            # (ej: el servidor arranca el día 1 a las 08:00), disparar HOY.
            candidato = datetime(ahora.year, ahora.month, 1, IECV_HORA, IECV_MINUTO, tzinfo=TZ_CHILE)
            if ahora < candidato:
                proximo = candidato

            espera = (proximo - ahora).total_seconds()
            logger.info(
                "Próximo recordatorio IECV: %s (en %.0f h)",
                proximo.strftime("%Y-%m-%d %H:%M %Z"),
                espera / 3600,
            )

            await asyncio.sleep(espera)

            logger.info("=== Recordatorio IECV mensual ===")
            recordar_iecv_mensual()
            logger.info("=== Recordatorio IECV mensual completado ===")

        except asyncio.CancelledError:
            logger.info("Scheduler IECV mensual detenido")
            break
        except Exception as e:
            logger.error("Error en loop IECV mensual: %s", e, exc_info=True)
            await asyncio.sleep(300)


async def _iecv_catch_up_al_boot() -> None:
    """A7: si el server estuvo caído el día 1 a las IECV_HORA, dispara el
    recordatorio IECV para el periodo anterior al arrancar.

    Lógica:
    - Si ``ahora < día_1_este_mes_a_IECV_HORA`` → el loop normal aún lo
      disparará; no hacer nada.
    - Si ``ahora >= día_1_este_mes_a_IECV_HORA`` y el estado persistido
      indica que el periodo anterior YA fue recordado → no hacer nada.
    - En cualquier otro caso → disparar ``recordar_iecv_mensual()`` ahora.
    """
    try:
        # Pequeña espera para no competir con el arranque de la app
        await asyncio.sleep(35)

        ahora = datetime.now(TZ_CHILE)
        candidato_este_mes = datetime(
            ahora.year, ahora.month, 1,
            IECV_HORA, IECV_MINUTO,
            tzinfo=TZ_CHILE,
        )

        if ahora < candidato_este_mes:
            # Todavía no llegó la hora del recordatorio de este mes:
            # el loop _loop_iecv_mensual lo disparará. Nada que hacer.
            logger.debug(
                "IECV catch-up: aún antes del candidato (%s) — loop normal se encarga",
                candidato_este_mes.strftime("%Y-%m-%d %H:%M"),
            )
            return

        periodo_pendiente = _mes_anterior(ahora.date())
        ultimo_recordado = get_scheduler_estado("iecv_ultimo_periodo_recordado")

        if ultimo_recordado == periodo_pendiente:
            logger.debug(
                "IECV catch-up: periodo %s ya fue recordado, nada que hacer",
                periodo_pendiente,
            )
            return

        logger.info(
            "=== IECV catch-up al boot: server caído el día 1 — "
            "disparando recordatorio para %s (último recordado: %s) ===",
            periodo_pendiente, ultimo_recordado,
        )
        recordar_iecv_mensual()
        logger.info("=== IECV catch-up completado para %s ===", periodo_pendiente)

    except asyncio.CancelledError:
        logger.info("IECV catch-up al boot cancelado")
        raise
    except Exception as exc:
        logger.error("Error en IECV catch-up al boot: %s", exc, exc_info=True)


# ══════════════════════════════════════════════════════════════
# Control de tasks
# ══════════════════════════════════════════════════════════════

# Handle del task para poder cancelarlo en shutdown
_rcof_task: asyncio.Task | None = None
_iecv_task: asyncio.Task | None = None
_polling_task: asyncio.Task | None = None
_backfill_task: asyncio.Task | None = None
_iecv_catch_up_task: asyncio.Task | None = None


# ══════════════════════════════════════════════════════════════
# Polling de estado SII (cada N minutos)
# ══════════════════════════════════════════════════════════════
#
# El envío al SII devuelve un trackid pero el estado real (DOK aceptado /
# DNK rechazado / FAU falla de autenticación / RFR rechazo formal / etc.)
# llega minutos u horas después. Sin polling agendado, los DTEs quedan en
# `estado_sii="enviado"` para siempre, y rechazos posteriores son invisibles
# al admin de la empresa.
#
# Este loop consulta QueryEstUp + QueryEstDte cada N minutos para cada
# empresa activa, actualizando `estado_sii`, `glosa_sii`, `estado_receptor`
# y `fecha_consulta_sii` en la BD del tenant. Re-procesa solo DTEs en
# estados intermedios ("enviado", "pendiente").

POLLING_INTERVALO_MIN = int(os.getenv("POLLING_INTERVALO_MIN", "30"))


def _ejecutar_polling_empresa(registro: EmpresaRegistro) -> dict:
    """Ejecuta polling de DTEs pendientes para una empresa.

    Importa lazy para evitar dependencias circulares en tests.
    """
    from crumbpos.core.sii_client.polling import poll_all
    from crumbpos.api.services.emision_dte import ServicioEmisionDTE, EmisorConfig

    db = get_empresa_db_session(registro.rut, registro.ambiente_activo)
    try:
        empresa = db.query(Empresa).filter(Empresa.rut == registro.rut).first()
        if not empresa:
            return {"ok": False, "error": "Empresa no encontrada en BD tenant"}
        if not empresa.fecha_resolucion or not empresa.cert_rut_firmante:
            return {"ok": False, "error": "Empresa sin certificado configurado"}

        # Resolver certificado y obtener tokens.
        cert_path = None
        tmp_file = None
        if empresa.cert_data:
            pfx_bytes = base64.b64decode(empresa.cert_data)
            fd, tmp_pfx = tempfile.mkstemp(suffix=".pfx")
            os.write(fd, pfx_bytes)
            os.close(fd)
            cert_path = tmp_pfx
            tmp_file = tmp_pfx
        elif empresa.cert_path and Path(empresa.cert_path).exists():
            cert_path = empresa.cert_path
        if not cert_path:
            return {"ok": False, "error": "Certificado .pfx no encontrado"}

        try:
            config = EmisorConfig(
                rut=empresa.rut,
                razon_social=empresa.razon_social,
                giro=empresa.giro or "",
                acteco=empresa.acteco or 0,
                direccion=empresa.direccion or "",
                comuna=empresa.comuna or "",
                ciudad=empresa.ciudad or "",
                fecha_resolucion=empresa.fecha_resolucion,
                numero_resolucion=empresa.numero_resolucion or 0,
                cert_path=cert_path,
                cert_password=empresa.cert_password,
                rut_firmante=empresa.cert_rut_firmante,
                ambiente=empresa.ambiente_sii,
            )
            servicio = ServicioEmisionDTE(config)
            token_soap = servicio._obtener_token()
            # Token boleta solo si hay boletas pendientes (lazy).
            token_boleta = None
            try:
                from crumbpos.db.models import DteEmitido
                hay_boleta_pendiente = (
                    db.query(DteEmitido)
                    .filter(
                        DteEmitido.tipo_dte.in_([39, 41]),
                        DteEmitido.estado_sii.in_(["enviado", "pendiente"]),
                    )
                    .first()
                ) is not None
                if hay_boleta_pendiente:
                    token_boleta = servicio._obtener_token_boleta()
            except Exception as exc:
                logger.warning("Polling: no se pudo obtener token boleta: %s", exc)

            resultado = poll_all(db, empresa, token_soap, token_boleta=token_boleta)
            db.commit()
            return {"ok": True, **resultado}
        finally:
            if tmp_file and os.path.exists(tmp_file):
                os.unlink(tmp_file)
    except Exception as exc:
        logger.error("Polling error para %s: %s", registro.rut, exc, exc_info=True)
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def ejecutar_polling_todas_empresas() -> list[dict]:
    """Polling de DTEs pendientes para todas las empresas activas."""
    resultados = []
    for reg in listar_empresas():
        if not reg.activa:
            continue
        logger.info("Polling: procesando %s (%s)...", reg.rut, reg.razon_social)
        resultado = _ejecutar_polling_empresa(reg)
        resultado["empresa_rut"] = reg.rut
        resultados.append(resultado)
        if resultado.get("ok"):
            logger.info(
                "  -> %s: %d consultados, %d actualizados",
                reg.rut,
                resultado.get("total_consultados", 0),
                resultado.get("total_actualizados", 0),
            )
        else:
            logger.warning("  -> %s: error — %s", reg.rut, resultado.get("error"))
    return resultados


async def _loop_polling_sii():
    """Loop que dispara polling cada POLLING_INTERVALO_MIN minutos.

    El polling es idempotente: solo actualiza DTEs en estados intermedios
    (enviado/pendiente). Si el SII está caído, retorna error por empresa
    pero sigue procesando las demás y reintenta al siguiente ciclo.
    """
    logger.info(
        "Scheduler polling SII iniciado — ejecución cada %d minutos",
        POLLING_INTERVALO_MIN,
    )
    # Esperar 60s antes del primer polling para no competir con el arranque.
    await asyncio.sleep(60)

    while True:
        try:
            logger.info("=== Polling SII (intervalo %d min) ===", POLLING_INTERVALO_MIN)
            resultados = ejecutar_polling_todas_empresas()
            ok = sum(1 for r in resultados if r.get("ok"))
            err = sum(1 for r in resultados if not r.get("ok"))
            logger.info("=== Polling SII completado: %d OK, %d errores ===", ok, err)
        except asyncio.CancelledError:
            logger.info("Scheduler polling SII detenido")
            break
        except Exception as exc:
            logger.error("Error en loop polling: %s", exc, exc_info=True)
        # Esperar hasta el próximo ciclo
        try:
            await asyncio.sleep(POLLING_INTERVALO_MIN * 60)
        except asyncio.CancelledError:
            logger.info("Scheduler polling SII detenido")
            break


def iniciar_scheduler():
    """Inicia el scheduler RCOF + IECV mensual + polling SII como tasks asyncio.

    Llamar desde el lifespan de FastAPI.
    """
    global _rcof_task, _iecv_task, _polling_task, _backfill_task, _iecv_catch_up_task
    _rcof_task = asyncio.create_task(_loop_rcof_diario())
    _iecv_task = asyncio.create_task(_loop_iecv_mensual())
    _polling_task = asyncio.create_task(_loop_polling_sii())
    # A6: backfill de RCOFs fallidos de los últimos 7 días (espera 30s al boot)
    _backfill_task = asyncio.create_task(_backfill_rcof_al_boot())
    # A7: catch-up IECV si el server estuvo caído el día 1 (espera 35s al boot)
    _iecv_catch_up_task = asyncio.create_task(_iecv_catch_up_al_boot())
    return _rcof_task


def detener_scheduler():
    """Detiene el scheduler RCOF, el recordatorio IECV mensual y el polling SII."""
    global _rcof_task, _iecv_task, _polling_task, _backfill_task, _iecv_catch_up_task
    if _rcof_task and not _rcof_task.done():
        _rcof_task.cancel()
        _rcof_task = None
    if _iecv_task and not _iecv_task.done():
        _iecv_task.cancel()
        _iecv_task = None
    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        _polling_task = None
    if _backfill_task and not _backfill_task.done():
        _backfill_task.cancel()
        _backfill_task = None
    if _iecv_catch_up_task and not _iecv_catch_up_task.done():
        _iecv_catch_up_task.cancel()
        _iecv_catch_up_task = None
