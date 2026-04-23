"""Reinicio de certificación — preserva CAFs (folio_actual intacto).

Durante la certificación puede ocurrir que los N° de Atención de un set
se quemen (el SII bloquea el combo FolioNotificacion + Periodo +
TipoLibro=ESPECIAL una vez recibido) y la única salida sea solicitar un
set nuevo al SII. En ese caso el software debe poder arrancar el wizard
de cero **sin perder el avance de folios** de los CAFs ya cargados: el
SII no permite retroceder folios y cada CAF nuevo demora.

Este módulo borra los datos del wizard en curso y los DTEs emitidos
durante la cert, pero **preserva** los ``CafFolio`` con su ``folio_actual``
intacto. La próxima emisión continúa desde el siguiente folio disponible.

**Regla R4**: este módulo SOLO abre ``certificacion.db``. Nunca abre la
base de datos productiva. Tests de invariante (``TestAislamientoR4``)
verifican esto automáticamente.

Lo que se borra:
  - ``CertificacionRun`` (cascada: ``CertificacionCaso``, ``CertificacionLibro``)
  - ``DteEmitido`` (folios ficticios ya enviados al SII)

Lo que se preserva:
  - ``CafFolio`` (incluyendo ``folio_actual``) — INVARIANTE CRÍTICA
  - ``Empresa``, ``Sucursal``, ``Usuario``, certificado .pfx

Diferencia con ``cleanup.py``:
  - ``cleanup``: post-producción. Requiere ``etapa='produccion'``. Borra CAFs.
  - ``reiniciar``: durante certificación. Requiere ``etapa!='produccion'``.
    Preserva CAFs.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from crumbpos.db.models import (
    CertificacionCaso,
    CertificacionLibro,
    CertificacionRun,
    DteEmitido,
)
from crumbpos.db.multi_tenant import (
    EmpresaEliminacionLog,
    EmpresaRegistro,
    get_empresa_db_session,
    get_master_session,
)

logger = logging.getLogger(__name__)


def reiniciar_certificacion(
    rut: str,
    user_id: str,
    user_email: str | None = None,
) -> dict[str, Any]:
    """Reinicia la certificación de una empresa preservando CAFs.

    Precondiciones:
      - La empresa debe existir en master.db.
      - La etapa NO debe ser ``produccion`` (si ya pasó a producción,
        usar ``cleanup.limpiar_certificacion`` en su lugar).

    Lo que hace:
      1. Valida que la empresa esté en certificación (no producción).
      2. Abre ``certificacion.db`` de la empresa.
      3. Borra runs → casos + libros (cascade).
      4. Borra ``DteEmitido`` (folios ficticios).
      5. **NO toca** ``CafFolio`` (folio_actual intacto).
      6. Registra evento ``cert_reiniciada`` en el log de auditoría
         (master.db) con el conteo de borrados.

    Args:
        rut: RUT de la empresa (ej: "77829149-5").
        user_id: ID del super admin que ejecuta el reinicio.
        user_email: Email opcional para el log.

    Returns:
        Dict con conteo de registros eliminados y timestamp.

    Raises:
        ValueError: si la empresa no existe o está en producción.
    """
    # ── Validar precondiciones en master.db ──────────────────────
    registro = _leer_registro(rut)

    if registro.etapa == "produccion":
        raise ValueError(
            f"La empresa {rut} ya está en producción "
            f"(etapa actual: {registro.etapa}). "
            "Usa cleanup.limpiar_certificacion para archivar los datos "
            "de la certificación; reiniciar solo aplica durante el proceso."
        )

    # ── Reiniciar en certificacion.db ────────────────────────────
    session = get_empresa_db_session(rut, "certificacion")
    stats: dict[str, int] = {}
    try:
        n_runs = session.query(CertificacionRun).count()
        n_casos = session.query(CertificacionCaso).count()
        n_libros = session.query(CertificacionLibro).count()
        n_dtes = session.query(DteEmitido).count()

        # Borrar en orden: hijos primero para evitar FK issues.
        # CafFolio se preserva intacto — INVARIANTE DEL MÓDULO.
        session.query(CertificacionCaso).delete()
        session.query(CertificacionLibro).delete()
        session.query(CertificacionRun).delete()
        session.query(DteEmitido).delete()

        session.commit()

        stats = {
            "runs": n_runs,
            "casos": n_casos,
            "libros": n_libros,
            "dtes": n_dtes,
        }

        logger.info(
            "Certificación reiniciada: rut=%s, runs=%d, casos=%d, "
            "libros=%d, dtes=%d (CAFs preservados con folio_actual intacto)",
            rut, n_runs, n_casos, n_libros, n_dtes,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # ── Log de auditoría en master.db ────────────────────────────
    now = datetime.utcnow()
    _log_evento(
        rut=rut,
        evento="cert_reiniciada",
        user_id=user_id,
        user_email=user_email,
        detalle=stats,
    )

    return {
        "rut": rut,
        "reiniciada_at": now.isoformat(),
        **stats,
    }


# ══════════════════════════════════════════════════════════════════
# Helpers internos
# ══════════════════════════════════════════════════════════════════


def _leer_registro(rut: str) -> EmpresaRegistro:
    master = get_master_session()
    try:
        reg = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if reg is None:
            raise ValueError(f"Empresa {rut} no existe en master.db")
        master.expunge(reg)
        return reg
    finally:
        master.close()


def _log_evento(
    rut: str,
    evento: str,
    user_id: str,
    user_email: str | None,
    detalle: dict[str, Any] | None = None,
) -> None:
    master = get_master_session()
    try:
        master.add(EmpresaEliminacionLog(
            id=str(uuid.uuid4()),
            empresa_rut=rut,
            evento=evento,
            user_id=user_id,
            user_email=user_email,
            timestamp=datetime.utcnow(),
            detalle_json=json.dumps(detalle or {}, default=str),
        ))
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
