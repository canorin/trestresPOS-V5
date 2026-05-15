"""Utilidades de manejo de errores — D3.

No exponer detalles internos de excepciones al cliente.
Loguear el traceback completo server-side y devolver solo un error_id.
"""
from __future__ import annotations

import logging
import secrets
import traceback

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def raise_safe_500(exc: Exception, contexto: str, *, log: logging.Logger | None = None) -> None:
    """Loguea `exc` con traceback completo y lanza HTTPException(500) con error_id.

    Args:
        exc: La excepción capturada.
        contexto: Texto breve para el log (ej. ``"crear usuario"``).
        log: Logger opcional; usa el del módulo si es None.

    Raises:
        HTTPException: siempre, con status_code=500 y detail con el error_id.
    """
    _log = log or logger
    error_id = secrets.token_hex(8)
    _log.error(
        "Error interno [error_id=%s] al %s: %s\n%s",
        error_id, contexto, exc, traceback.format_exc(),
    )
    raise HTTPException(
        status_code=500,
        detail=f"Error interno del servidor (error_id={error_id}). Consulte los logs.",
    )
