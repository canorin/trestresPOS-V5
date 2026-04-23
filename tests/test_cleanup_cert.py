"""Tests para crumbpos/certificacion/cleanup.py (Fase 6.b).

Cubre:
- Limpieza exitosa: runs/casos/libros/dtes/cafs borrados.
- Preservación: Empresa/Sucursal/Usuario intactos.
- Precondiciones: etapa debe ser 'produccion', no debe estar ya archivada.
- Marcado en master.db: cert_archivada_at + log de auditoría.
- Aislamiento R4: cleanup.py no menciona 'produccion.db'.

Usa SQLite in-memory con monkey-patch de get_empresa_db_session y
get_master_session para evitar dependencia del filesystem.
"""
from __future__ import annotations

import ast
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.db.models import (
    Base,
    CafFolio,
    CertificacionCaso,
    CertificacionLibro,
    CertificacionRun,
    DteEmitido,
    Empresa,
    Sucursal,
)
from crumbpos.db.multi_tenant import (
    BaseMaster,
    EmpresaEliminacionLog,
    EmpresaRegistro,
)


# ══════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def cert_engine():
    """Engine + tables para certificacion.db (in-memory)."""
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
    """Engine + tables para master.db (in-memory)."""
    engine = create_engine("sqlite:///:memory:", future=True)
    BaseMaster.metadata.create_all(engine)
    return engine


@pytest.fixture
def master_session(master_engine):
    Session = sessionmaker(bind=master_engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        master_engine.dispose()


RUT = "77051056-2"
USER_ID = "admin-001"


@pytest.fixture
def empresa_cert(cert_session):
    """Crea Empresa + Sucursal en certificacion.db."""
    e = Empresa(
        id="emp-1",
        rut=RUT,
        razon_social="Test SPA",
        giro="Consultoría",
        acteco=741000,
        direccion="Test 123",
        comuna="Providencia",
        ciudad="Santiago",
        ambiente_sii="certificacion",
        fecha_resolucion="2014-08-22",
        numero_resolucion=80,
    )
    s = Sucursal(
        id="suc-1",
        empresa_id="emp-1",
        nombre="Casa Matriz",
        direccion="Test 123",
        comuna="Providencia",
        ciudad="Santiago",
    )
    cert_session.add(e)
    cert_session.add(s)
    cert_session.commit()
    return e


@pytest.fixture
def run_con_datos(cert_session, empresa_cert):
    """Crea una run completa con casos, libros, DTEs y CAFs."""
    run = CertificacionRun(
        id="run-1",
        rut_empresa=RUT,
        estado="completado",
        screen_actual=4,
    )
    cert_session.add(run)
    cert_session.flush()

    # Casos
    for i in range(3):
        cert_session.add(CertificacionCaso(
            run_id=run.id,
            set_nombre="BASICO",
            numero_caso=f"caso-{i}",
            numero_atencion=1234,
            tipo_dte=33,
            estado="aprobado",
        ))

    # Libros
    cert_session.add(CertificacionLibro(
        run_id=run.id,
        tipo_libro="ventas",
        estado="aprobado",
    ))

    # DTEs emitidos
    for i in range(3):
        cert_session.add(DteEmitido(
            empresa_id=empresa_cert.id,
            tipo_dte=33,
            folio=i + 1,
            fecha_emision=date(2026, 4, 9),
            receptor_rut="66666666-6",
            receptor_razon="Receptor",
            monto_neto=10000,
            monto_exento=0,
            iva=1900,
            monto_total=11900,
            estado_sii="pendiente",
        ))

    # CAFs
    cert_session.add(CafFolio(
        empresa_id=empresa_cert.id,
        tipo_dte=33,
        rango_desde=1,
        rango_hasta=100,
        folio_actual=4,
        caf_xml_raw="<CAF/>",
    ))

    cert_session.commit()
    return run


@pytest.fixture
def registro_master(master_session):
    """Crea EmpresaRegistro en master.db en estado producción."""
    reg = EmpresaRegistro(
        rut=RUT,
        razon_social="Test SPA",
        etapa="produccion",
        ambiente_activo="produccion",
    )
    master_session.add(reg)
    master_session.commit()
    return reg


# ══════════════════════════════════════════════════════════════════
# Helper: monkey-patch de sessions
# ══════════════════════════════════════════════════════════════════

def _patch_sessions(cert_engine, master_engine):
    """Devuelve context managers para patchear las funciones de session."""
    CertSession = sessionmaker(bind=cert_engine, future=True)
    MasterSession = sessionmaker(bind=master_engine, future=True)

    p1 = patch(
        "crumbpos.certificacion.cleanup.get_empresa_db_session",
        side_effect=lambda rut, amb: CertSession(),
    )
    p2 = patch(
        "crumbpos.certificacion.cleanup.get_master_session",
        side_effect=lambda: MasterSession(),
    )
    return p1, p2


# ══════════════════════════════════════════════════════════════════
# Tests: limpieza exitosa
# ══════════════════════════════════════════════════════════════════


class TestLimpiezaExitosa:

    def test_borra_runs_casos_libros(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, run_con_datos, registro_master,
    ):
        """Después de cleanup, no quedan runs/casos/libros."""
        from crumbpos.certificacion.cleanup import limpiar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            result = limpiar_certificacion(RUT, USER_ID)

        assert result["runs"] == 1
        assert result["casos"] == 3
        assert result["libros"] == 1

        # Verificar en DB
        s = sessionmaker(bind=cert_engine, future=True)()
        assert s.query(CertificacionRun).count() == 0
        assert s.query(CertificacionCaso).count() == 0
        assert s.query(CertificacionLibro).count() == 0
        s.close()

    def test_borra_dtes_y_cafs(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, run_con_datos, registro_master,
    ):
        """Después de cleanup, no quedan DTEs ni CAFs."""
        from crumbpos.certificacion.cleanup import limpiar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            result = limpiar_certificacion(RUT, USER_ID)

        assert result["dtes"] == 3
        assert result["cafs"] == 1

        s = sessionmaker(bind=cert_engine, future=True)()
        assert s.query(DteEmitido).count() == 0
        assert s.query(CafFolio).count() == 0
        s.close()

    def test_preserva_empresa_y_sucursal(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, run_con_datos, registro_master,
    ):
        """Empresa y Sucursal siguen intactos después de cleanup."""
        from crumbpos.certificacion.cleanup import limpiar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            limpiar_certificacion(RUT, USER_ID)

        s = sessionmaker(bind=cert_engine, future=True)()
        assert s.query(Empresa).count() == 1
        emp = s.query(Empresa).first()
        assert emp.rut == RUT
        assert s.query(Sucursal).count() == 1
        s.close()

    def test_marca_cert_archivada_at(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, run_con_datos, registro_master,
    ):
        """cert_archivada_at se setea en EmpresaRegistro."""
        from crumbpos.certificacion.cleanup import limpiar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            result = limpiar_certificacion(RUT, USER_ID)

        assert "cert_archivada_at" in result

        s = sessionmaker(bind=master_engine, future=True)()
        reg = s.query(EmpresaRegistro).filter_by(rut=RUT).first()
        assert reg.cert_archivada_at is not None
        s.close()

    def test_log_evento_registrado(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, run_con_datos, registro_master,
    ):
        """Se registra evento cert_archivada en el log."""
        from crumbpos.certificacion.cleanup import limpiar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            limpiar_certificacion(RUT, USER_ID, user_email="admin@test.cl")

        s = sessionmaker(bind=master_engine, future=True)()
        logs = s.query(EmpresaEliminacionLog).filter_by(empresa_rut=RUT).all()
        assert len(logs) == 1
        assert logs[0].evento == "cert_archivada"
        assert logs[0].user_id == USER_ID
        assert logs[0].user_email == "admin@test.cl"
        s.close()


# ══════════════════════════════════════════════════════════════════
# Tests: precondiciones
# ══════════════════════════════════════════════════════════════════


class TestPrecondiciones:

    def test_falla_si_no_esta_en_produccion(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, run_con_datos,
    ):
        """Error si la empresa no está en producción."""
        # Crear registro en etapa certificación
        reg = EmpresaRegistro(
            rut=RUT,
            razon_social="Test SPA",
            etapa="proceso_certificacion",
        )
        master_session.add(reg)
        master_session.commit()

        from crumbpos.certificacion.cleanup import limpiar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            with pytest.raises(ValueError, match="no está en producción"):
                limpiar_certificacion(RUT, USER_ID)

    def test_falla_si_ya_archivada(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, run_con_datos,
    ):
        """Error si la certificación ya fue archivada."""
        reg = EmpresaRegistro(
            rut=RUT,
            razon_social="Test SPA",
            etapa="produccion",
            cert_archivada_at=datetime.utcnow(),
        )
        master_session.add(reg)
        master_session.commit()

        from crumbpos.certificacion.cleanup import limpiar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            with pytest.raises(ValueError, match="ya fue archivada"):
                limpiar_certificacion(RUT, USER_ID)

    def test_falla_empresa_no_existe(
        self, cert_engine, master_engine,
        cert_session, master_session,
    ):
        """Error si la empresa no existe en master.db."""
        from crumbpos.certificacion.cleanup import limpiar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            with pytest.raises(ValueError, match="no existe"):
                limpiar_certificacion("99999999-9", USER_ID)


# ══════════════════════════════════════════════════════════════════
# Tests: sin datos que limpiar
# ══════════════════════════════════════════════════════════════════


class TestSinDatos:

    def test_cleanup_sin_runs_no_falla(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, registro_master,
    ):
        """Si no hay runs, cleanup ejecuta OK con conteo 0."""
        from crumbpos.certificacion.cleanup import limpiar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            result = limpiar_certificacion(RUT, USER_ID)

        assert result["runs"] == 0
        assert result["casos"] == 0
        assert result["cert_archivada_at"]


# ══════════════════════════════════════════════════════════════════
# Test: R4 — aislamiento estático
# ══════════════════════════════════════════════════════════════════


class TestAislamientoR4:

    def test_no_menciona_produccion_db(self):
        """cleanup.py no contiene string 'produccion.db'."""
        src = Path(__file__).resolve().parent.parent / "crumbpos" / "certificacion" / "cleanup.py"
        content = src.read_text()
        assert "produccion.db" not in content

    def test_no_importa_routers_produccion(self):
        """cleanup.py no importa módulos de routers de producción."""
        src = Path(__file__).resolve().parent.parent / "crumbpos" / "certificacion" / "cleanup.py"
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "routers" not in node.module or "certificacion" in node.module, (
                    f"cleanup.py importa router de producción: {node.module}"
                )
