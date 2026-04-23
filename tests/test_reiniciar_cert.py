"""Tests para crumbpos/certificacion/reiniciar.py.

Reinicia una certificación en curso preservando los CAFs (con
``folio_actual`` intacto). Se usa cuando la empresa quemó sus N° de
Atención en una certificación fallida y el SII le entrega un set nuevo:
hay que empezar de cero el wizard pero sin perder los folios ya
consumidos (el SII no permite retroceder folios y volver a pedir CAFs
por cada reinicio cuesta días).

Diferencia clave con ``cleanup.py``:
  - ``cleanup``: post-producción. Borra TODO incluyendo CAFs.
  - ``reiniciar``: durante certificación. Borra runs/casos/libros/DTEs
    pero **preserva CafFolio con folio_actual intacto**.

Invariante crítica que estos tests defienden:
  Un reinicio NUNCA debe alterar ``CafFolio.folio_actual``. Si esto se
  rompe, el próximo DTE emitido reusaría folios ya enviados al SII y
  generaría un rechazo FAU (folio ya usado).
"""
from __future__ import annotations

import ast
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

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
def master_session(master_engine):
    Session = sessionmaker(bind=master_engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        master_engine.dispose()


RUT = "77829149-5"
USER_ID = "admin-001"


@pytest.fixture
def empresa_cert(cert_session):
    e = Empresa(
        id="emp-1",
        rut=RUT,
        razon_social="GRUPO TRESTRES SPA",
        giro="Publicidad",
        acteco=731001,
        direccion="Los Militares 5620",
        comuna="Las Condes",
        ciudad="Santiago",
        ambiente_sii="certificacion",
        fecha_resolucion="2026-04-21",
        numero_resolucion=0,
    )
    s = Sucursal(
        id="suc-1",
        empresa_id="emp-1",
        nombre="Casa Matriz",
        direccion="Los Militares 5620",
        comuna="Las Condes",
        ciudad="Santiago",
    )
    cert_session.add(e)
    cert_session.add(s)
    cert_session.commit()
    return e


@pytest.fixture
def datos_cert_en_curso(cert_session, empresa_cert):
    """Run en curso + casos + libros + DTEs emitidos + CAFs con folio avanzado."""
    run = CertificacionRun(
        id="run-1",
        rut_empresa=RUT,
        estado="en_progreso",
        screen_actual=3,
    )
    cert_session.add(run)
    cert_session.flush()

    for i in range(2):
        cert_session.add(CertificacionCaso(
            run_id=run.id,
            set_nombre="BASICO",
            numero_caso=f"caso-{i}",
            numero_atencion=4788484,
            tipo_dte=33,
            estado="emitido",
        ))

    cert_session.add(CertificacionLibro(
        run_id=run.id,
        tipo_libro="ventas",
        estado="rechazado",
        estado_sii="LNC",
    ))

    for i in range(2):
        cert_session.add(DteEmitido(
            empresa_id=empresa_cert.id,
            tipo_dte=33,
            folio=i + 1,
            fecha_emision=date(2026, 4, 22),
            receptor_rut="66666666-6",
            receptor_razon="Receptor Cert",
            monto_neto=10000,
            monto_exento=0,
            iva=1900,
            monto_total=11900,
            estado_sii="pendiente",
        ))

    # CAFs con folio_actual ya avanzado (simula folios consumidos)
    cert_session.add(CafFolio(
        empresa_id=empresa_cert.id,
        tipo_dte=33,
        rango_desde=1,
        rango_hasta=100,
        folio_actual=5,  # ← ya se consumieron 4 folios (folio 5 es el siguiente)
        caf_xml_raw="<CAF/>",
    ))
    cert_session.add(CafFolio(
        empresa_id=empresa_cert.id,
        tipo_dte=52,
        rango_desde=1,
        rango_hasta=50,
        folio_actual=3,  # ← ya se consumieron 2 guías
        caf_xml_raw="<CAF/>",
    ))

    cert_session.commit()
    return run


@pytest.fixture
def registro_en_proceso(master_session):
    reg = EmpresaRegistro(
        rut=RUT,
        razon_social="GRUPO TRESTRES SPA",
        etapa="proceso_certificacion",
        ambiente_activo="certificacion",
    )
    master_session.add(reg)
    master_session.commit()
    return reg


@pytest.fixture
def registro_produccion(master_session):
    reg = EmpresaRegistro(
        rut=RUT,
        razon_social="GRUPO TRESTRES SPA",
        etapa="produccion",
        ambiente_activo="produccion",
    )
    master_session.add(reg)
    master_session.commit()
    return reg


def _patch_sessions(cert_engine, master_engine):
    CertSession = sessionmaker(bind=cert_engine, future=True)
    MasterSession = sessionmaker(bind=master_engine, future=True)
    p1 = patch(
        "crumbpos.certificacion.reiniciar.get_empresa_db_session",
        side_effect=lambda rut, amb: CertSession(),
    )
    p2 = patch(
        "crumbpos.certificacion.reiniciar.get_master_session",
        side_effect=lambda: MasterSession(),
    )
    return p1, p2


# ══════════════════════════════════════════════════════════════════
# Invariante crítica: CAFs preservados
# ══════════════════════════════════════════════════════════════════


class TestPreservacionDeCAFs:
    """INVARIANTE: reiniciar NUNCA debe modificar CafFolio.folio_actual.

    Si esto se rompe, reemisiones re-usarían folios → rechazo FAU del SII.
    """

    def test_folio_actual_no_cambia(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, datos_cert_en_curso, registro_en_proceso,
    ):
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            reiniciar_certificacion(RUT, USER_ID)

        s = sessionmaker(bind=cert_engine, future=True)()
        try:
            caf_t33 = s.query(CafFolio).filter(CafFolio.tipo_dte == 33).first()
            caf_t52 = s.query(CafFolio).filter(CafFolio.tipo_dte == 52).first()
            assert caf_t33 is not None, "CAF T33 debe preservarse"
            assert caf_t52 is not None, "CAF T52 debe preservarse"
            assert caf_t33.folio_actual == 5, (
                "folio_actual de T33 debe quedar intacto — si cambia, "
                "el próximo DTE emitido chocará con un folio ya enviado al SII."
            )
            assert caf_t52.folio_actual == 3, (
                "folio_actual de T52 debe quedar intacto."
            )
        finally:
            s.close()

    def test_rango_y_xml_caf_no_cambian(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, datos_cert_en_curso, registro_en_proceso,
    ):
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            reiniciar_certificacion(RUT, USER_ID)

        s = sessionmaker(bind=cert_engine, future=True)()
        try:
            caf = s.query(CafFolio).filter(CafFolio.tipo_dte == 33).first()
            assert caf.rango_desde == 1
            assert caf.rango_hasta == 100
            assert caf.caf_xml_raw == "<CAF/>" or caf.caf_xml_raw == b"<CAF/>"
        finally:
            s.close()

    def test_conteo_cafs_no_cambia(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, datos_cert_en_curso, registro_en_proceso,
    ):
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            reiniciar_certificacion(RUT, USER_ID)

        s = sessionmaker(bind=cert_engine, future=True)()
        try:
            assert s.query(CafFolio).count() == 2
        finally:
            s.close()


# ══════════════════════════════════════════════════════════════════
# Datos de certificación borrados
# ══════════════════════════════════════════════════════════════════


class TestDatosCertBorrados:

    def test_borra_runs_casos_libros(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, datos_cert_en_curso, registro_en_proceso,
    ):
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            result = reiniciar_certificacion(RUT, USER_ID)

        assert result["runs"] == 1
        assert result["casos"] == 2
        assert result["libros"] == 1

        s = sessionmaker(bind=cert_engine, future=True)()
        try:
            assert s.query(CertificacionRun).count() == 0
            assert s.query(CertificacionCaso).count() == 0
            assert s.query(CertificacionLibro).count() == 0
        finally:
            s.close()

    def test_borra_dtes_emitidos(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, datos_cert_en_curso, registro_en_proceso,
    ):
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            result = reiniciar_certificacion(RUT, USER_ID)

        assert result["dtes"] == 2
        s = sessionmaker(bind=cert_engine, future=True)()
        try:
            assert s.query(DteEmitido).count() == 0
        finally:
            s.close()


# ══════════════════════════════════════════════════════════════════
# Preservación de Empresa/Sucursal/cert
# ══════════════════════════════════════════════════════════════════


class TestPreservacionEmpresa:

    def test_preserva_empresa(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, datos_cert_en_curso, registro_en_proceso,
    ):
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            reiniciar_certificacion(RUT, USER_ID)

        s = sessionmaker(bind=cert_engine, future=True)()
        try:
            emp = s.query(Empresa).filter(Empresa.rut == RUT).first()
            assert emp is not None
            assert emp.razon_social == "GRUPO TRESTRES SPA"
            assert emp.fecha_resolucion == "2026-04-21"
        finally:
            s.close()

    def test_preserva_sucursal(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, datos_cert_en_curso, registro_en_proceso,
    ):
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            reiniciar_certificacion(RUT, USER_ID)

        s = sessionmaker(bind=cert_engine, future=True)()
        try:
            assert s.query(Sucursal).count() == 1
        finally:
            s.close()


# ══════════════════════════════════════════════════════════════════
# Precondiciones
# ══════════════════════════════════════════════════════════════════


class TestPrecondiciones:

    def test_rechaza_si_etapa_produccion(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, datos_cert_en_curso, registro_produccion,
    ):
        """No se puede reiniciar cert si la empresa ya está en producción."""
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            with pytest.raises(ValueError, match="producci"):
                reiniciar_certificacion(RUT, USER_ID)

        # Y los datos deben seguir intactos (rollback implícito)
        s = sessionmaker(bind=cert_engine, future=True)()
        try:
            assert s.query(CertificacionRun).count() == 1
            assert s.query(CafFolio).count() == 2
        finally:
            s.close()

    def test_rechaza_si_empresa_no_existe(
        self, cert_engine, master_engine,
        cert_session, master_session,
    ):
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            with pytest.raises(ValueError, match="no existe"):
                reiniciar_certificacion("99999999-9", USER_ID)


# ══════════════════════════════════════════════════════════════════
# Idempotencia y logging
# ══════════════════════════════════════════════════════════════════


class TestIdempotenciaYLog:

    def test_sin_datos_a_borrar_no_falla(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, registro_en_proceso,
    ):
        """Si no hay run/casos/libros/dtes, reiniciar no debe fallar."""
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            result = reiniciar_certificacion(RUT, USER_ID)

        assert result["runs"] == 0
        assert result["casos"] == 0
        assert result["libros"] == 0
        assert result["dtes"] == 0

    def test_registra_evento_en_log_auditoria(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, datos_cert_en_curso, registro_en_proceso,
    ):
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            reiniciar_certificacion(RUT, USER_ID, user_email="matias@trestres.cl")

        s = sessionmaker(bind=master_engine, future=True)()
        try:
            logs = s.query(EmpresaEliminacionLog).filter(
                EmpresaEliminacionLog.empresa_rut == RUT,
            ).all()
            assert len(logs) == 1
            assert logs[0].evento == "cert_reiniciada"
            assert logs[0].user_id == USER_ID
            assert logs[0].user_email == "matias@trestres.cl"
        finally:
            s.close()

    def test_no_marca_cert_archivada(
        self, cert_engine, master_engine,
        cert_session, master_session,
        empresa_cert, datos_cert_en_curso, registro_en_proceso,
    ):
        """Reiniciar NO es archivar: cert_archivada_at debe quedar en None."""
        from crumbpos.certificacion.reiniciar import reiniciar_certificacion

        p1, p2 = _patch_sessions(cert_engine, master_engine)
        with p1, p2:
            reiniciar_certificacion(RUT, USER_ID)

        s = sessionmaker(bind=master_engine, future=True)()
        try:
            reg = s.query(EmpresaRegistro).filter(
                EmpresaRegistro.rut == RUT,
            ).first()
            assert reg.cert_archivada_at is None
        finally:
            s.close()


# ══════════════════════════════════════════════════════════════════
# Aislamiento: no tocar producción (regla R4)
# ══════════════════════════════════════════════════════════════════


class TestAislamientoR4:
    """reiniciar.py solo opera sobre certificacion.db. Nunca debe abrir
    la base de datos del ambiente productivo."""

    def test_codigo_no_menciona_produccion_db(self):
        path = Path(__file__).parent.parent / "crumbpos" / "certificacion" / "reiniciar.py"
        code = path.read_text()
        # No debe aparecer la cadena "produccion.db" ni abrir 'produccion' como ambiente
        assert "produccion.db" not in code
        # get_empresa_db_session se llama solo con el ambiente "certificacion"
        tree = ast.parse(code)
        llamadas_producción = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "get_empresa_db_session"
            ):
                # Buscar segundo argumento literal == "produccion"
                if len(node.args) >= 2:
                    arg = node.args[1]
                    if isinstance(arg, ast.Constant) and arg.value == "produccion":
                        llamadas_producción.append(node.lineno)
        assert not llamadas_producción, (
            f"reiniciar.py abre producción en líneas {llamadas_producción}; viola R4."
        )
