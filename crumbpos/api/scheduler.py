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
)
from crumbpos.db.models import Empresa, DteEmitido
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

            logger.info("=== Ejecutando RCOF diario ===")
            resultados = ejecutar_rcof_todas_empresas()

            ok = sum(1 for r in resultados if r.get("ok"))
            err = sum(1 for r in resultados if not r.get("ok"))
            logger.info(
                "=== RCOF diario completado: %d OK, %d errores ===",
                ok, err,
            )

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
    """
    hoy = date.today()
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


# ══════════════════════════════════════════════════════════════
# Control de tasks
# ══════════════════════════════════════════════════════════════

# Handle del task para poder cancelarlo en shutdown
_rcof_task: asyncio.Task | None = None
_iecv_task: asyncio.Task | None = None
_polling_task: asyncio.Task | None = None


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
    global _rcof_task, _iecv_task, _polling_task
    _rcof_task = asyncio.create_task(_loop_rcof_diario())
    _iecv_task = asyncio.create_task(_loop_iecv_mensual())
    _polling_task = asyncio.create_task(_loop_polling_sii())
    return _rcof_task


def detener_scheduler():
    """Detiene el scheduler RCOF, el recordatorio IECV mensual y el polling SII."""
    global _rcof_task, _iecv_task, _polling_task
    if _rcof_task and not _rcof_task.done():
        _rcof_task.cancel()
        _rcof_task = None
    if _iecv_task and not _iecv_task.done():
        _iecv_task.cancel()
        _iecv_task = None
    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        _polling_task = None
