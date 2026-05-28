"""Configuración centralizada de logging para CrumbPOS.

NUEVO 2026-05-28 — Antes no existía configuración de logging centralizada.
Historial: los logs iban solo a consola/stderr. En producción (servidor sin terminal
           visible) no quedaba registro de errores. Cuando el SII rechazaba un DTE
           o el wizard fallaba, no había forma de saber qué pasó después de que
           la sesión de terminal se cerraba.
Causa raíz: ningún handler de archivo configurado. Librerías externas (sqlalchemy,
            httpx) ahogaban los logs propios en nivel DEBUG.
Solución: logging centralizado con RotatingFileHandler a data/logs/crumbpos.log
          (10 MB, 5 backups). Llamar una vez desde lifespan de FastAPI.

Llamar ``configurar_logging()`` una vez al iniciar la aplicación (desde lifespan
de FastAPI). Produce dos canales simultáneos:

  - Consola (stderr): útil en desarrollo y para ver el servidor en tiempo real.
  - Archivo rotativo data/logs/crumbpos.log: persistente, para diagnóstico
    posterior en producción o certificación.

Formato de cada línea:
    2026-05-28 10:23:45 | INFO     | crumbpos.api.services.emision_dte | mensaje

Uso desde cualquier módulo del proyecto::

    import logging
    logger = logging.getLogger(__name__)
    logger.info("DTE emitido rut=%s tipo=%s folio=%s", rut, tipo, folio)
    logger.error("Firma DTE falló rut=%s tipo=%s folio=%s: %s", rut, tipo, folio, exc)
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Librerías externas que generan mucho ruido en nivel DEBUG.
# Se fijan en WARNING para que los logs propios sean legibles.
_LIBRERIAS_SILENCIAR = [
    "uvicorn.access",
    "uvicorn.error",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "httpx",
    "httpcore",
    "multipart",
    "passlib",
    "python_multipart",
]


def configurar_logging(
    nivel: str | None = None,
    log_dir: Path | None = None,
) -> None:
    """Configura logging global: consola + archivo rotativo.

    Es idempotente: llamadas repetidas limpian los handlers anteriores y
    reconfiguran desde cero. Seguro llamar más de una vez (ej. en tests).

    Args:
        nivel: Nivel de logging ('DEBUG' / 'INFO' / 'WARNING' / 'ERROR').
               Por defecto INFO en producción (CRUMBPOS_ENV=production),
               DEBUG en cualquier otro entorno.
        log_dir: Directorio donde escribir crumbpos.log.
                 Por defecto: data/logs/ relativo al directorio de trabajo.
    """
    if nivel is None:
        env = os.getenv("CRUMBPOS_ENV", "development").lower()
        nivel = "INFO" if env == "production" else "DEBUG"

    numeric_level = getattr(logging, nivel.upper(), logging.INFO)
    formatter = logging.Formatter(fmt=_FMT, datefmt=_DATE_FMT)

    # ── Handler consola ───────────────────────────────────────────────────────
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(numeric_level)

    # ── Handler archivo rotativo ──────────────────────────────────────────────
    if log_dir is None:
        log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "crumbpos.log"

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB por archivo
        backupCount=5,               # 5 archivos de respaldo → 60 MB máximo total
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(numeric_level)

    # ── Root logger ───────────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.handlers.clear()  # garantiza idempotencia
    root.addHandler(console)
    root.addHandler(file_handler)

    # ── Silenciar librerías externas verbosas ─────────────────────────────────
    for nombre in _LIBRERIAS_SILENCIAR:
        logging.getLogger(nombre).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging configurado — nivel=%s  archivo=%s", nivel, log_file.resolve()
    )
