"""Tareas programadas — RCOF diario a las 23:00.

Se ejecuta como background task dentro del proceso FastAPI (asyncio).
Itera todas las empresas activas y genera+envia el RCOF del dia
para cada una que tenga boletas emitidas.

Multi-tenant: accede a cada BD de empresa de forma independiente,
sin requerir contexto HTTP ni autenticacion.
"""
import asyncio
import logging
import os
import base64
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

from crumbpos.db.multi_tenant import (
    listar_empresas,
    get_empresa_db_session,
    EmpresaRegistro,
)
from crumbpos.db.models import Empresa
from crumbpos.api.services.rcof_service import ServicioRCOF
from crumbpos.config import settings

logger = logging.getLogger(__name__)

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
        fecha: Fecha del RCOF (default: hoy)

    Returns:
        Lista de resultados por empresa.
    """
    if fecha is None:
        fecha = date.today()

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
    """Calcula segundos hasta la proxima ocurrencia de hora:minuto."""
    ahora = datetime.now()
    objetivo = datetime.combine(ahora.date(), time(hora, minuto))

    if ahora >= objetivo:
        # Ya paso hoy, programar para manana
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
            proxima = datetime.now() + timedelta(seconds=espera)
            logger.info(
                "Proxima ejecucion RCOF: %s (en %.0f min)",
                proxima.strftime("%Y-%m-%d %H:%M"),
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


# Handle del task para poder cancelarlo en shutdown
_rcof_task: asyncio.Task | None = None


def iniciar_scheduler():
    """Inicia el scheduler RCOF como background task asyncio.

    Llamar desde el lifespan de FastAPI.
    """
    global _rcof_task
    _rcof_task = asyncio.create_task(_loop_rcof_diario())
    return _rcof_task


def detener_scheduler():
    """Detiene el scheduler RCOF."""
    global _rcof_task
    if _rcof_task and not _rcof_task.done():
        _rcof_task.cancel()
        _rcof_task = None
