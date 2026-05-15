"""Tests Fase 5 — Declarar avance y marcar aprobación (sets y libros).

Cubre:
- ``envio_sobre_cert.declarar_avance`` y ``marcar_aprobado`` (sets/sobres)
- ``envio_libro_cert.declarar_avance_libro`` y ``marcar_aprobado_libro``

Valida las precondiciones (trackid obligatorio, avance antes de aprobación),
los campos persistidos (timestamps, estado) y los mensajes de error.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.api.services.envio_libro_cert import (
    declarar_avance_libro,
    marcar_aprobado_libro,
)
from crumbpos.api.services.envio_sobre_cert import (
    declarar_avance,
    marcar_aprobado,
)
from crumbpos.db.models import (
    Base,
    CertificacionCaso,
    CertificacionLibro,
    CertificacionRun,
    Empresa,
)


# ══════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def empresa(session):
    e = Empresa(
        id="emp-1",
        rut="77051056-2",
        razon_social="Test SPA",
        giro="Consultoría",
        acteco=741000,
        direccion="Los Conquistadores 1700",
        comuna="Providencia",
        ciudad="Santiago",
        ambiente_sii="certificacion",
        fecha_resolucion="2014-08-22",
        numero_resolucion=80,
    )
    session.add(e)
    session.commit()
    return e


@pytest.fixture
def run(session, empresa):
    r = CertificacionRun(
        id="run-1",
        rut_empresa=empresa.rut,
        estado="emitiendo",
        screen_actual=3,
    )
    session.add(r)
    session.commit()
    return r


@pytest.fixture
def run_otra(session, empresa):
    """Run distinta para probar aislamiento."""
    r = CertificacionRun(
        id="run-otro",
        rut_empresa=empresa.rut,
        estado="emitiendo",
        screen_actual=3,
    )
    session.add(r)
    session.commit()
    return r


def _make_caso(session, run, set_nombre, numero_caso, trackid=None):
    """Crea un CertificacionCaso minimalista."""
    c = CertificacionCaso(
        run_id=run.id,
        set_nombre=set_nombre,
        numero_caso=numero_caso,
        numero_atencion=1234,
        tipo_dte=33,
        estado="emitido" if trackid else "pendiente",
        trackid=trackid,
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _make_libro(session, run, tipo_libro, trackid=None):
    """Crea un CertificacionLibro minimalista."""
    lib = CertificacionLibro(
        run_id=run.id,
        tipo_libro=tipo_libro,
        trackid=trackid,
    )
    session.add(lib)
    session.commit()
    session.refresh(lib)
    return lib


# ══════════════════════════════════════════════════════════════════
# Tests: Declarar avance — SETS
# ══════════════════════════════════════════════════════════════════


class TestDeclararAvanceSet:

    def test_ok_registra_fecha(self, session, run):
        """Avance se registra con timestamp en todos los casos del set."""
        c1 = _make_caso(session, run, "BASICO", "caso-1", trackid="T001")
        c2 = _make_caso(session, run, "BASICO", "caso-2", trackid="T002")

        result = declarar_avance(session, run, "BASICO")

        assert result["set_nombre"] == "BASICO"
        assert result["casos_actualizados"] == 2
        assert result["avance_declarado_at"]

        session.refresh(c1)
        session.refresh(c2)
        assert c1.avance_declarado_at is not None
        assert c2.avance_declarado_at is not None

    def test_falla_sin_casos(self, session, run):
        """Error si no hay casos para el set indicado."""
        with pytest.raises(ValueError, match="No hay casos"):
            declarar_avance(session, run, "INEXISTENTE")

    def test_falla_sin_trackid(self, session, run):
        """Error si algún caso no tiene trackid (no fue enviado)."""
        _make_caso(session, run, "BASICO", "caso-1", trackid="T001")
        _make_caso(session, run, "BASICO", "caso-2", trackid=None)

        with pytest.raises(ValueError, match="sin trackid"):
            declarar_avance(session, run, "BASICO")

    def test_no_afecta_otro_set(self, session, run):
        """Declarar avance de un set no toca los casos de otro set."""
        _make_caso(session, run, "BASICO", "caso-1", trackid="T001")
        c_guias = _make_caso(session, run, "GUIAS", "caso-g1", trackid="T010")

        declarar_avance(session, run, "BASICO")

        session.refresh(c_guias)
        assert c_guias.avance_declarado_at is None

    def test_idempotente(self, session, run):
        """Llamar avance dos veces sobreescribe la fecha sin error."""
        _make_caso(session, run, "BASICO", "caso-1", trackid="T001")

        r1 = declarar_avance(session, run, "BASICO")
        r2 = declarar_avance(session, run, "BASICO")
        # No lanza error — actualiza la fecha
        assert r2["casos_actualizados"] == 1


# ══════════════════════════════════════════════════════════════════
# Tests: Marcar aprobado — SETS
# ══════════════════════════════════════════════════════════════════


class TestMarcarAprobadoSet:

    def test_ok_marca_estado(self, session, run):
        """Aprobación cambia estado a 'aprobado' y setea aprobado_at."""
        c = _make_caso(session, run, "BASICO", "caso-1", trackid="T001")
        declarar_avance(session, run, "BASICO")

        result = marcar_aprobado(session, run, "BASICO")

        assert result["set_nombre"] == "BASICO"
        assert result["casos_actualizados"] == 1
        assert result["aprobado_at"]

        session.refresh(c)
        assert c.estado == "aprobado"
        assert c.aprobado_at is not None

    def test_falla_sin_avance(self, session, run):
        """Error si no se declaró avance antes de aprobar."""
        _make_caso(session, run, "BASICO", "caso-1", trackid="T001")

        with pytest.raises(ValueError, match="no tiene avance declarado"):
            marcar_aprobado(session, run, "BASICO")

    def test_falla_sin_casos(self, session, run):
        """Error si no hay casos para el set."""
        with pytest.raises(ValueError, match="No hay casos"):
            marcar_aprobado(session, run, "INEXISTENTE")

    def test_multiples_casos_todos_aprobados(self, session, run):
        """Todos los casos del set quedan aprobados."""
        c1 = _make_caso(session, run, "BASICO", "caso-1", trackid="T001")
        c2 = _make_caso(session, run, "BASICO", "caso-2", trackid="T002")
        c3 = _make_caso(session, run, "BASICO", "caso-3", trackid="T003")
        declarar_avance(session, run, "BASICO")

        result = marcar_aprobado(session, run, "BASICO")

        assert result["casos_actualizados"] == 3
        for c in [c1, c2, c3]:
            session.refresh(c)
            assert c.estado == "aprobado"
            assert c.aprobado_at is not None


# ══════════════════════════════════════════════════════════════════
# Tests: Declarar avance — LIBROS
# ══════════════════════════════════════════════════════════════════


class TestDeclararAvanceLibro:

    def test_ok_registra_fecha(self, session, run):
        """Avance se registra con timestamp en el libro."""
        lib = _make_libro(session, run, "ventas", trackid="TL-001")

        result = declarar_avance_libro(session, run, lib.id)

        assert result["libro_id"] == lib.id
        assert result["tipo_libro"] == "ventas"
        assert result["avance_declarado_at"]

        session.refresh(lib)
        assert lib.avance_declarado_at is not None

    def test_falla_sin_trackid(self, session, run):
        """Error si el libro no fue enviado (sin trackid)."""
        lib = _make_libro(session, run, "compras", trackid=None)

        with pytest.raises(ValueError, match="no tiene trackid"):
            declarar_avance_libro(session, run, lib.id)

    def test_falla_libro_no_encontrado(self, session, run):
        """Error si el libro_id no existe."""
        with pytest.raises(ValueError, match="no encontrado"):
            declarar_avance_libro(session, run, "inexistente-id")

    def test_falla_run_mismatch(self, session, run, run_otra):
        """Error si el libro pertenece a otra run."""
        lib = _make_libro(session, run_otra, "ventas", trackid="TL-001")

        with pytest.raises(ValueError, match="no pertenece"):
            declarar_avance_libro(session, run, lib.id)

    def test_idempotente(self, session, run):
        """Llamar avance dos veces sobreescribe la fecha sin error."""
        lib = _make_libro(session, run, "guias", trackid="TL-010")

        r1 = declarar_avance_libro(session, run, lib.id)
        r2 = declarar_avance_libro(session, run, lib.id)
        assert r2["avance_declarado_at"]


# ══════════════════════════════════════════════════════════════════
# Tests: Marcar aprobado — LIBROS
# ══════════════════════════════════════════════════════════════════


class TestMarcarAprobadoLibro:

    def test_ok_marca_estado(self, session, run):
        """Aprobación cambia estado a 'aprobado' y setea aprobado_at."""
        lib = _make_libro(session, run, "ventas", trackid="TL-001")
        declarar_avance_libro(session, run, lib.id)

        result = marcar_aprobado_libro(session, run, lib.id)

        assert result["libro_id"] == lib.id
        assert result["tipo_libro"] == "ventas"
        assert result["aprobado_at"]

        session.refresh(lib)
        assert lib.estado == "aprobado"
        assert lib.aprobado_at is not None

    def test_falla_sin_avance(self, session, run):
        """Error si no se declaró avance antes."""
        lib = _make_libro(session, run, "compras", trackid="TL-002")

        with pytest.raises(ValueError, match="no tiene avance declarado"):
            marcar_aprobado_libro(session, run, lib.id)

    def test_falla_libro_no_encontrado(self, session, run):
        """Error si el libro_id no existe."""
        with pytest.raises(ValueError, match="no encontrado"):
            marcar_aprobado_libro(session, run, "nope-id")

    def test_falla_run_mismatch(self, session, run, run_otra):
        """Error si el libro pertenece a otra run."""
        lib = _make_libro(session, run_otra, "ventas", trackid="TL-001")
        lib.avance_declarado_at = datetime.now(timezone.utc)
        session.commit()

        with pytest.raises(ValueError, match="no pertenece"):
            marcar_aprobado_libro(session, run, lib.id)

    def test_flujo_completo_avance_luego_aprobado(self, session, run):
        """Flujo completo: crear → enviar (simular trackid) → declarar avance → aprobar."""
        lib = _make_libro(session, run, "guias", trackid="TL-G01")

        # Paso 1: declarar avance
        r1 = declarar_avance_libro(session, run, lib.id)
        session.refresh(lib)
        assert lib.avance_declarado_at is not None
        assert lib.estado != "aprobado"

        # Paso 2: marcar aprobado
        r2 = marcar_aprobado_libro(session, run, lib.id)
        session.refresh(lib)
        assert lib.estado == "aprobado"
        assert lib.aprobado_at is not None
