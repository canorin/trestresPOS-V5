"""Limpieza post-certificación — production-safe por construcción.

Después de que una empresa pasa a etapa ``produccion`` (Fase 6.a),
los datos del wizard de certificación (runs, casos, libros, DTEs de
prueba) ya no se necesitan. Este módulo los elimina de ``certificacion.db``
preservando los registros operativos (Empresa, Sucursal, Usuario, CAFs).

**Regla R4**: este módulo SOLO abre ``certificacion.db``. No puede
abrir la base de datos del ambiente productivo, no puede importar
routers de ese ambiente, no puede leer ni escribir datos de él.
El guardian hook y los tests de invariantes verifican esto automáticamente.

Lo que se borra:
  - ``CertificacionRun`` (cascada: ``CertificacionCaso``, ``CertificacionLibro``)
  - ``DteEmitido`` emitidos durante la certificación (folios ficticios)
  - ``CafFolio`` del ambiente cert (ya consumidos, no reutilizables)

Lo que se preserva:
  - ``Empresa`` (config necesaria para routing y producción)
  - ``Sucursal`` (ídem)
  - ``Usuario`` (ídem)

Lo que se marca en master.db:
  - ``EmpresaRegistro.cert_archivada_at`` con timestamp del archivado
  - ``EmpresaEliminacionLog`` con evento ``cert_archivada``
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from crumbpos.db.models import (
    CafFolio,
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


def limpiar_certificacion(
    rut: str,
    user_id: str,
    user_email: str | None = None,
) -> dict[str, Any]:
    """Limpia los datos de certificación de una empresa.

    Precondiciones:
      - La empresa debe existir en master.db.
      - La etapa debe ser ``produccion`` (ya pasó la certificación).

    Lo que hace:
      1. Abre ``certificacion.db`` de la empresa.
      2. Cuenta y borra runs → casos + libros (cascade).
      3. Borra ``DteEmitido`` (DTEs de prueba, folios ficticios).
      4. Borra ``CafFolio`` (CAFs consumidos durante cert).
      5. Marca ``cert_archivada_at`` en ``EmpresaRegistro`` (master.db).
      6. Agrega evento ``cert_archivada`` al log de auditoría.

    Args:
        rut: RUT de la empresa (ej: "77051056-2").
        user_id: ID del super admin que ejecuta la limpieza.
        user_email: Email opcional para el log.

    Returns:
        Dict con conteo de registros eliminados.

    Raises:
        ValueError: si la empresa no existe o no está en producción.
    """
    # ── Validar precondiciones en master.db ──────────────────────
    registro = _leer_registro(rut)

    if registro.etapa != "produccion":
        raise ValueError(
            f"La empresa {rut} no está en producción "
            f"(etapa actual: {registro.etapa}). "
            "Solo se puede limpiar la certificación después de pasar a producción."
        )

    if registro.cert_archivada_at is not None:
        raise ValueError(
            f"La certificación de {rut} ya fue archivada el "
            f"{registro.cert_archivada_at.isoformat()}."
        )

    # ── Limpiar en certificacion.db ──────────────────────────────
    session = get_empresa_db_session(rut, "certificacion")
    stats: dict[str, int] = {}
    try:
        # Contar antes de borrar (para el resumen)
        n_runs = session.query(CertificacionRun).count()
        n_casos = session.query(CertificacionCaso).count()
        n_libros = session.query(CertificacionLibro).count()
        n_dtes = session.query(DteEmitido).count()
        n_cafs = session.query(CafFolio).count()

        # Borrar en orden: hijos primero para evitar FK issues
        # (aunque SQLite no fuerza FK por defecto, es buena práctica)
        session.query(CertificacionCaso).delete()
        session.query(CertificacionLibro).delete()
        session.query(CertificacionRun).delete()
        session.query(DteEmitido).delete()
        session.query(CafFolio).delete()

        session.commit()

        stats = {
            "runs": n_runs,
            "casos": n_casos,
            "libros": n_libros,
            "dtes": n_dtes,
            "cafs": n_cafs,
        }

        logger.info(
            "Certificación limpiada: rut=%s, runs=%d, casos=%d, "
            "libros=%d, dtes=%d, cafs=%d",
            rut, n_runs, n_casos, n_libros, n_dtes, n_cafs,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # ── Marcar en master.db ──────────────────────────────────────
    now = datetime.now(timezone.utc)
    _marcar_archivada(rut, now)
    _log_evento(
        rut=rut,
        evento="cert_archivada",
        user_id=user_id,
        user_email=user_email,
        detalle=stats,
    )

    return {
        "rut": rut,
        "cert_archivada_at": now.isoformat(),
        **stats,
    }


# ══════════════════════════════════════════════════════════════════
# Helpers internos
# ══════════════════════════════════════════════════════════════════


def _leer_registro(rut: str) -> EmpresaRegistro:
    """Lee EmpresaRegistro desde master.db."""
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


def _marcar_archivada(rut: str, ts: datetime) -> None:
    """Setea cert_archivada_at en EmpresaRegistro."""
    master = get_master_session()
    try:
        reg = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if reg:
            reg.cert_archivada_at = ts
            master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()


def _log_evento(
    rut: str,
    evento: str,
    user_id: str,
    user_email: str | None,
    detalle: dict[str, Any] | None = None,
) -> None:
    """Append-only al log de auditoría en master.db."""
    master = get_master_session()
    try:
        master.add(EmpresaEliminacionLog(
            id=str(uuid.uuid4()),
            empresa_rut=rut,
            evento=evento,
            user_id=user_id,
            user_email=user_email,
            timestamp=datetime.now(timezone.utc),
            detalle_json=json.dumps(detalle or {}, default=str),
        ))
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
