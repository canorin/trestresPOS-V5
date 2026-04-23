"""Tests para la auto-hidratación de la fila ``Empresa`` en BD per-empresa.

Cubre dos capas complementarias del fix observado en 2026-04-21 con
77829149-5, donde un reset previo dejó la empresa registrada en master.db
pero con la fila ``Empresa`` ausente de ``certificacion.db``. Resultado:
cualquier endpoint pasado por ``get_tenant`` tiraba 500 "Empresa no
inicializada en BD certificacion" — bloqueando upload de CAFs, emisión de
DTE y consultas de folios.

Capas testeadas:

1. **`_hidratar_empresa_desde_datos_setup`** (router certificacion) —
   cuando PATCH /api/certificacion/runs/... llega con ``datos_setup``,
   el handler upsertea la fila ``Empresa`` con los valores del
   formulario de screen 1 del wizard. Insert si falta, update selectivo
   si ya existe (no sobreescribe con strings vacíos).

2. **`_ensure_empresa_row_seeded`** (multi_tenant) — defensa en
   profundidad: si ``get_empresa_engine`` detecta que el registro existe
   en master.db pero no hay fila ``Empresa`` en la BD de la empresa,
   inserta un stub mínimo con RUT + razón social del registro + campos
   vacíos. Esto garantiza que incluso si alguien salta el flujo del
   wizard (tests, scripts, APIs directas), la infraestructura funciona.

Los tests corren sobre SQLite in-memory y monkey-patchean las sessions
factory del módulo ``multi_tenant`` para no tocar filesystem.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.db.models import Base, Empresa
from crumbpos.db.multi_tenant import BaseMaster, EmpresaRegistro


# ══════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def cert_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def cert_session(cert_engine):
    Session = sessionmaker(bind=cert_engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        cert_engine.dispose()


@pytest.fixture
def master_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    BaseMaster.metadata.create_all(engine)
    return engine


@pytest.fixture
def master_with_registro(master_engine):
    """Inserta un EmpresaRegistro en master.db y deja la session abierta."""
    Session = sessionmaker(bind=master_engine, future=True)
    s = Session()
    reg = EmpresaRegistro(
        rut="77829149-5",
        razon_social="GRUPO TRESTRES SPA",
        ambiente_activo="certificacion",
        etapa="pendiente_certificacion",
        plan="full_free",
    )
    s.add(reg)
    s.commit()
    s.close()
    return master_engine


# ══════════════════════════════════════════════════════════════════
# Tests: _hidratar_empresa_desde_datos_setup
# ══════════════════════════════════════════════════════════════════


class TestHidratarDatosSetup:
    """PATCH /runs con datos_setup hidrata la fila Empresa."""

    def test_inserta_empresa_si_no_existe(self, cert_session):
        from crumbpos.api.routers.certificacion import (
            _hidratar_empresa_desde_datos_setup,
        )

        assert cert_session.query(Empresa).count() == 0

        datos = {
            "rut": "77829149-5",
            "razon_social": "GRUPO TRESTRES SPA",
            "giro": "SERVICIOS DE PUBLICIDAD",
            "acteco": 731001,
            "firmante": "17586255-2",
            "direccion": "CALLE FALSA 123",
            "comuna": "PROVIDENCIA",
            "ciudad": "SANTIAGO",
            "fecha_resolucion": "2026-04-01",
            "numero_resolucion": 0,
        }
        _hidratar_empresa_desde_datos_setup(cert_session, "77829149-5", datos)
        cert_session.commit()

        emp = cert_session.query(Empresa).filter_by(rut="77829149-5").one()
        assert emp.razon_social == "GRUPO TRESTRES SPA"
        assert emp.giro == "SERVICIOS DE PUBLICIDAD"
        assert emp.acteco == 731001
        assert emp.cert_rut_firmante == "17586255-2"
        assert emp.direccion == "CALLE FALSA 123"
        assert emp.comuna == "PROVIDENCIA"
        assert emp.ciudad == "SANTIAGO"
        assert emp.fecha_resolucion == "2026-04-01"
        assert emp.ambiente_sii == "certificacion"

    def test_actualiza_empresa_existente_selectivamente(self, cert_session):
        """Update: sobreescribe solo campos no-vacíos, preserva el resto."""
        from crumbpos.api.routers.certificacion import (
            _hidratar_empresa_desde_datos_setup,
        )

        # Fila pre-existente con todos los campos.
        cert_session.add(Empresa(
            id="emp-pre",
            rut="77829149-5",
            razon_social="GRUPO TRESTRES SPA",
            giro="GIRO ORIGINAL",
            direccion="DIRECCION ORIGINAL",
            comuna="COMUNA ORIGINAL",
            ciudad="CIUDAD ORIGINAL",
            acteco=100000,
            ambiente_sii="certificacion",
        ))
        cert_session.commit()

        # Patch parcial — solo giro y dirección.
        datos = {
            "giro": "GIRO NUEVO",
            "direccion": "DIRECCION NUEVA",
            "comuna": "",  # vacío: no debe sobreescribir
            "ciudad": "",  # vacío: no debe sobreescribir
        }
        _hidratar_empresa_desde_datos_setup(cert_session, "77829149-5", datos)
        cert_session.commit()

        emp = cert_session.query(Empresa).filter_by(rut="77829149-5").one()
        assert emp.giro == "GIRO NUEVO"
        assert emp.direccion == "DIRECCION NUEVA"
        assert emp.comuna == "COMUNA ORIGINAL"  # preservado
        assert emp.ciudad == "CIUDAD ORIGINAL"  # preservado
        assert emp.acteco == 100000  # preservado

    def test_acteco_no_entero_se_descarta(self, cert_session):
        """Si ACTECO viene como string no-numérico, se ignora (no crashea)."""
        from crumbpos.api.routers.certificacion import (
            _hidratar_empresa_desde_datos_setup,
        )

        datos = {
            "rut": "77829149-5",
            "razon_social": "GRUPO TRESTRES SPA",
            "giro": "G",
            "direccion": "D",
            "comuna": "C",
            "ciudad": "S",
            "acteco": "abc",  # inválido, no entero
        }
        _hidratar_empresa_desde_datos_setup(cert_session, "77829149-5", datos)
        cert_session.commit()

        emp = cert_session.query(Empresa).filter_by(rut="77829149-5").one()
        assert emp.acteco is None

    def test_firmante_vacio_no_rompe(self, cert_session):
        from crumbpos.api.routers.certificacion import (
            _hidratar_empresa_desde_datos_setup,
        )

        datos = {
            "rut": "77829149-5",
            "razon_social": "GRUPO TRESTRES SPA",
            "giro": "G",
            "direccion": "D",
            "comuna": "C",
            "ciudad": "S",
            "firmante": "",
        }
        _hidratar_empresa_desde_datos_setup(cert_session, "77829149-5", datos)
        cert_session.commit()

        emp = cert_session.query(Empresa).filter_by(rut="77829149-5").one()
        assert emp.cert_rut_firmante is None

    def test_insert_con_campos_obligatorios_vacios_funciona(self, cert_session):
        """Insert con strings vacíos en NOT NULL: SQLite lo acepta (no NULL)."""
        from crumbpos.api.routers.certificacion import (
            _hidratar_empresa_desde_datos_setup,
        )

        datos = {"rut": "77829149-5", "razon_social": "X"}
        _hidratar_empresa_desde_datos_setup(cert_session, "77829149-5", datos)
        cert_session.commit()

        emp = cert_session.query(Empresa).filter_by(rut="77829149-5").one()
        assert emp.razon_social == "X"
        assert emp.giro == ""
        assert emp.direccion == ""


# ══════════════════════════════════════════════════════════════════
# Tests: _ensure_empresa_row_seeded (self-heal en get_empresa_engine)
# ══════════════════════════════════════════════════════════════════


class TestSelfHealSeeded:
    """``_ensure_empresa_row_seeded`` crea stub si master tiene registro pero falta Empresa."""

    def test_siembra_stub_si_falta_fila_pero_hay_registro(
        self, cert_engine, master_with_registro, monkeypatch,
    ):
        """Caso 77829149-5: registro en master, fila vacía en cert → stub."""
        from crumbpos.db import multi_tenant as mt
        from crumbpos.db.multi_tenant import _ensure_empresa_row_seeded

        # monkey-patch master session factory para que apunte a nuestro
        # engine in-memory con el registro.
        MasterSessionFactory = sessionmaker(
            bind=master_with_registro, future=True,
        )
        monkeypatch.setattr(mt, "_master_engine", master_with_registro)
        monkeypatch.setattr(mt, "_MasterSessionFactory", MasterSessionFactory)
        monkeypatch.setattr(mt, "_ensure_master", lambda: None)

        # Pre-condición: la BD cert está vacía.
        Session = sessionmaker(bind=cert_engine, future=True)
        with Session() as s:
            assert s.query(Empresa).count() == 0

        _ensure_empresa_row_seeded(cert_engine, "77829149-5", "certificacion")

        # Post: hay una fila stub con RUT + razón social del master.
        with Session() as s:
            emp = s.query(Empresa).filter_by(rut="77829149-5").one()
            assert emp.razon_social == "GRUPO TRESTRES SPA"
            assert emp.giro == ""
            assert emp.direccion == ""
            assert emp.ambiente_sii == "certificacion"

    def test_no_toca_fila_si_ya_existe(
        self, cert_engine, master_with_registro, monkeypatch,
    ):
        """Si la fila ya existe, no debe tocarla (idempotente)."""
        from crumbpos.db import multi_tenant as mt
        from crumbpos.db.multi_tenant import _ensure_empresa_row_seeded

        MasterSessionFactory = sessionmaker(
            bind=master_with_registro, future=True,
        )
        monkeypatch.setattr(mt, "_master_engine", master_with_registro)
        monkeypatch.setattr(mt, "_MasterSessionFactory", MasterSessionFactory)
        monkeypatch.setattr(mt, "_ensure_master", lambda: None)

        # Pre-existente con datos.
        Session = sessionmaker(bind=cert_engine, future=True)
        with Session() as s:
            s.add(Empresa(
                id="emp-existing",
                rut="77829149-5",
                razon_social="NOMBRE ORIGINAL",
                giro="GIRO ORIGINAL",
                direccion="DIR",
                comuna="C",
                ciudad="S",
                ambiente_sii="certificacion",
            ))
            s.commit()

        _ensure_empresa_row_seeded(cert_engine, "77829149-5", "certificacion")

        with Session() as s:
            emp = s.query(Empresa).filter_by(rut="77829149-5").one()
            # Sin cambios.
            assert emp.razon_social == "NOMBRE ORIGINAL"
            assert emp.giro == "GIRO ORIGINAL"
            assert s.query(Empresa).count() == 1

    def test_no_siembra_si_no_hay_registro_en_master(
        self, cert_engine, master_engine, monkeypatch,
    ):
        """Master vacío → no siembra nada (no sabemos razón social)."""
        from crumbpos.db import multi_tenant as mt
        from crumbpos.db.multi_tenant import _ensure_empresa_row_seeded

        MasterSessionFactory = sessionmaker(bind=master_engine, future=True)
        monkeypatch.setattr(mt, "_master_engine", master_engine)
        monkeypatch.setattr(mt, "_MasterSessionFactory", MasterSessionFactory)
        monkeypatch.setattr(mt, "_ensure_master", lambda: None)

        _ensure_empresa_row_seeded(cert_engine, "99999999-9", "certificacion")

        Session = sessionmaker(bind=cert_engine, future=True)
        with Session() as s:
            assert s.query(Empresa).count() == 0
