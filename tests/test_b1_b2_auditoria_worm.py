"""Tests para B1/B2 — AuditoriaEvento append-only + WORM en dte_emitido.

B1:
- registrar_evento crea fila con hash_row correcto
- Segundo evento encadena hash_prev = hash_row del anterior
- UPDATE en auditoria_evento → error de trigger
- DELETE en auditoria_evento → error de trigger

B2:
- DELETE en dte_emitido reciente → error de trigger WORM
- DELETE en dte_emitido > 6 años → permitido
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from crumbpos.db.models import (
    Base, Empresa, AuditoriaEvento, DteEmitido,
    registrar_evento, _hash_evento,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _engine_con_triggers():
    """Crea engine in-memory con tablas + triggers B1/B2 instalados."""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)

    # Instalar triggers manualmente (normalmente los instala _migrate_empresa_schema)
    with eng.connect() as conn:
        # B2: WORM dte_emitido
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_dte_emitido_worm "
            "BEFORE DELETE ON dte_emitido "
            "BEGIN "
            "  SELECT CASE "
            "    WHEN julianday('now') - julianday(OLD.fecha_emision) < 2191.5 "
            "    THEN RAISE(ABORT, 'B2-WORM') "
            "  END; "
            "END"
        ))
        # B1: auditoria append-only
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_auditoria_no_update "
            "BEFORE UPDATE ON auditoria_evento "
            "BEGIN "
            "  SELECT RAISE(ABORT, 'B1: append-only UPDATE'); "
            "END"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_auditoria_no_delete "
            "BEFORE DELETE ON auditoria_evento "
            "BEGIN "
            "  SELECT RAISE(ABORT, 'B1: append-only DELETE'); "
            "END"
        ))
        conn.commit()
    return eng


def _empresa(session: Session) -> Empresa:
    emp = Empresa(
        id=str(uuid.uuid4()),
        rut="76354771-K",
        razon_social="Test SA",
        giro="Comercio",
        direccion="Calle 1",
        comuna="Santiago",
        ciudad="Santiago",
    )
    session.add(emp)
    session.flush()
    return emp


def _dte(session: Session, empresa_id: str, folio: int = 1,
         fecha_emision: date | None = None) -> DteEmitido:
    dte = DteEmitido(
        empresa_id=empresa_id,
        tipo_dte=33,
        folio=folio,
        fecha_emision=fecha_emision or date.today(),
        monto_total=100000,
        estado_sii="pendiente",
    )
    session.add(dte)
    session.flush()
    return dte


# ──────────────────────────────────────────────────────────────────────────────
# B1 — AuditoriaEvento
# ──────────────────────────────────────────────────────────────────────────────

def test_registrar_evento_crea_fila():
    """registrar_evento crea una fila AuditoriaEvento en la DB."""
    eng = _engine_con_triggers()
    with Session(eng) as s, s.begin():
        emp = _empresa(s)
        evt = registrar_evento(s, emp.id, "TEST_EVENTO", {"key": "val"})
        assert evt.id is not None
        assert evt.tipo == "TEST_EVENTO"
        assert evt.hash_row is not None
        assert len(evt.hash_row) == 64  # SHA-256 hexdigest


def test_primer_evento_hash_prev_es_none():
    """El primer evento de una empresa no tiene hash_prev (génesis)."""
    eng = _engine_con_triggers()
    with Session(eng) as s, s.begin():
        emp = _empresa(s)
        evt = registrar_evento(s, emp.id, "PRIMER", {})
        hash_prev_capturado = evt.hash_prev  # capturar dentro de la sesión
    assert hash_prev_capturado is None


def test_segundo_evento_encadena_hash():
    """El segundo evento tiene hash_prev == hash_row del anterior."""
    eng = _engine_con_triggers()
    with Session(eng) as s, s.begin():
        emp = _empresa(s)
        e1 = registrar_evento(s, emp.id, "E1", {"n": 1})
        e2 = registrar_evento(s, emp.id, "E2", {"n": 2})
        h1 = e1.hash_row
        hp2 = e2.hash_prev
    assert hp2 == h1


def test_hash_row_es_determinista():
    """_hash_evento produce el mismo resultado para las mismas entradas."""
    h1 = _hash_evento("prev_hash", "id-1", "TIPO", {"x": 1}, "2025-01-01T12:00:00")
    h2 = _hash_evento("prev_hash", "id-1", "TIPO", {"x": 1}, "2025-01-01T12:00:00")
    assert h1 == h2
    assert len(h1) == 64


def test_hash_cambia_si_payload_cambia():
    """Modificar el payload produce un hash distinto (detecta manipulación)."""
    h1 = _hash_evento(None, "id-1", "T", {"x": 1}, "2025-01-01T00:00:00")
    h2 = _hash_evento(None, "id-1", "T", {"x": 2}, "2025-01-01T00:00:00")
    assert h1 != h2


def test_trigger_bloquea_update_en_auditoria():
    """UPDATE sobre auditoria_evento debe fallar con error de trigger B1."""
    from sqlalchemy.exc import OperationalError, IntegrityError
    eng = _engine_con_triggers()
    with Session(eng) as s, s.begin():
        emp = _empresa(s)
        evt = registrar_evento(s, emp.id, "INMUTABLE", {})
        evt_id = evt.id

    with Session(eng) as s:
        with pytest.raises((OperationalError, IntegrityError)):
            s.execute(
                text("UPDATE auditoria_evento SET tipo = 'MODIFICADO' WHERE id = :id"),
                {"id": evt_id},
            )
            s.commit()


def test_trigger_bloquea_delete_en_auditoria():
    """DELETE sobre auditoria_evento debe fallar con error de trigger B1."""
    from sqlalchemy.exc import OperationalError, IntegrityError
    eng = _engine_con_triggers()
    with Session(eng) as s, s.begin():
        emp = _empresa(s)
        evt = registrar_evento(s, emp.id, "INMUTABLE", {})
        evt_id = evt.id

    with Session(eng) as s:
        with pytest.raises((OperationalError, IntegrityError)):
            s.execute(
                text("DELETE FROM auditoria_evento WHERE id = :id"),
                {"id": evt_id},
            )
            s.commit()


# ──────────────────────────────────────────────────────────────────────────────
# B2 — WORM en dte_emitido
# ──────────────────────────────────────────────────────────────────────────────

def test_worm_bloquea_delete_de_dte_reciente():
    """DELETE de un DTE de hoy debe fallar con error WORM B2."""
    from sqlalchemy.exc import OperationalError, IntegrityError
    eng = _engine_con_triggers()
    with Session(eng) as s, s.begin():
        emp = _empresa(s)
        dte = _dte(s, emp.id, fecha_emision=date.today())
        dte_id = dte.id

    with Session(eng) as s:
        with pytest.raises((OperationalError, IntegrityError)):
            s.execute(
                text("DELETE FROM dte_emitido WHERE id = :id"),
                {"id": dte_id},
            )
            s.commit()


def test_worm_permite_delete_de_dte_antiguo():
    """DELETE de un DTE > 6 años debe poder ejecutarse (fuera de retención)."""
    eng = _engine_con_triggers()
    with Session(eng) as s, s.begin():
        emp = _empresa(s)
        # Fecha hace 7 años — fuera del periodo de retención
        fecha_vieja = (datetime.now() - timedelta(days=7 * 366)).date()
        dte = _dte(s, emp.id, fecha_emision=fecha_vieja)
        dte_id = dte.id

    with Session(eng) as s, s.begin():
        # No debe lanzar excepción
        s.execute(
            text("DELETE FROM dte_emitido WHERE id = :id"),
            {"id": dte_id},
        )
    # Verificar que se eliminó
    with Session(eng) as s:
        count = s.execute(
            text("SELECT COUNT(*) FROM dte_emitido WHERE id = :id"),
            {"id": dte_id},
        ).scalar()
    assert count == 0


def test_worm_bloquea_dte_de_hace_5_anios():
    """DELETE de un DTE de hace 5 años también debe ser bloqueado (< 6 años)."""
    from sqlalchemy.exc import OperationalError, IntegrityError
    eng = _engine_con_triggers()
    with Session(eng) as s, s.begin():
        emp = _empresa(s)
        fecha_5_anios = (datetime.now() - timedelta(days=5 * 365)).date()
        dte = _dte(s, emp.id, fecha_emision=fecha_5_anios)
        dte_id = dte.id

    with Session(eng) as s:
        with pytest.raises((OperationalError, IntegrityError)):
            s.execute(
                text("DELETE FROM dte_emitido WHERE id = :id"),
                {"id": dte_id},
            )
            s.commit()
