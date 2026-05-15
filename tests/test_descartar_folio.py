"""Tests para el flujo de descarte + re-emisión con folio nuevo.

Contexto operativo — 2026-04-22, set BASICO de 77829149-5.

Cuando el SII acepta un sobre (EPR) pero rechaza un DTE individual con
DTE-3-100 o DTE-3-101, ese folio queda quemado. El usuario necesita
descartarlo y re-emitir el caso con un folio nuevo del CAF.

El wizard expone esto como un flujo de DOS pasos separados (en vez de
un endpoint atómico que mezclaba las dos acciones y dejaba el caso en
un estado "EMITIDO" confuso para el usuario):

1. ``POST /casos/{rut}/{caso_id}/descartar-folio``
   - Marca el DteEmitido viejo como ``descartado``.
   - Resetea el caso: ``folio=None``, ``dte_emitido_id=None``,
     ``estado='pendiente'``, limpia ``trackid`` / ``estado_sii`` /
     ``error_mensaje``.
   - NO asigna folio nuevo ni emite XML. El usuario ve el caso volver
     visiblemente al estado PENDIENTE.

2. ``POST /casos/{rut}/{caso_id}/emitir`` (endpoint normal de emisión)
   - El caso ahora está en ``pendiente``, así que emite como primera vez.
   - Asigna el próximo folio disponible del CAF.

Reglas de gating del descarte:

- Solo aplica si el caso está en ``emitido`` y tiene ``dte_emitido_id``.
- Bloqueado si ``avance_declarado_at`` o ``aprobado_at`` están seteados:
  un caso declarado al SII no puede descartarse (ya quedó en la historia
  del SII bajo ese folio).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.db.models import (
    Base,
    CertificacionCaso,
    CertificacionRun,
    DteEmitido,
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
        rut="77829149-5",
        razon_social="GRUPO TRESTRES SPA",
        giro="SERVICIOS DE PUBLICIDAD",
        direccion="AV PROVIDENCIA 123",
        comuna="PROVIDENCIA",
        ciudad="SANTIAGO",
        cert_rut_firmante="11111111-1",
    )
    session.add(e)
    session.flush()
    return e


@pytest.fixture
def run(session):
    r = CertificacionRun(
        rut_empresa="77829149-5",
        estado="set_cargado",
        screen_actual=3,
    )
    session.add(r)
    session.flush()
    return r


def _mk_caso_emitido(
    run_id: str,
    empresa_id: str,
    session,
    *,
    numero: str = "4788482-5",
    folio: int = 32,
    tipo_dte: int = 61,
) -> tuple[CertificacionCaso, DteEmitido]:
    """Caso emitido con DteEmitido asociado — listo para descartar."""
    from datetime import date as _date, timezone

    dte = DteEmitido(
        empresa_id=empresa_id,
        tipo_dte=tipo_dte,
        folio=folio,
        fecha_emision=_date(2026, 4, 22),
        receptor_rut="60803000-K",
        receptor_razon="SII",
        monto_neto=1000,
        iva=190,
        monto_total=1190,
        xml_firmado="XML_CON_FOLIO_ORIGINAL",
        estado_sii="EPR",
    )
    session.add(dte)
    session.flush()

    caso = CertificacionCaso(
        run_id=run_id,
        set_nombre="BASICO",
        numero_caso=numero,
        numero_atencion=4788482,
        tipo_dte=tipo_dte,
        datos={"items": [{"nombre": "X", "cantidad": 1, "precio_unitario": 1000}]},
        estado="emitido",
        folio=folio,
        dte_emitido_id=dte.id,
        emitido_at=datetime.now(timezone.utc),
        trackid="0247737678",
        estado_sii="EPR",
        error_mensaje=None,
    )
    session.add(caso)
    session.flush()
    return caso, dte


# ══════════════════════════════════════════════════════════════════
# Endpoint POST /descartar-folio — descartar SIN re-emitir
# ══════════════════════════════════════════════════════════════════


class TestEndpointDescartarFolio:
    """El endpoint existe, descarta el folio y resetea el caso a pendiente."""

    def test_endpoint_existe_y_firma(self):
        """El router debe exponer ``descartar_folio_caso`` como función."""
        from crumbpos.api.routers import certificacion as cert_router

        assert hasattr(cert_router, "descartar_folio_caso"), (
            "El router debe tener una función descartar_folio_caso para "
            "descartar un folio sin re-emitir."
        )
        import inspect
        sig = inspect.signature(cert_router.descartar_folio_caso)
        params = list(sig.parameters.keys())
        # Debe aceptar rut y caso_id (como el resto de endpoints)
        assert "rut" in params
        assert "caso_id" in params

    def test_descartar_resetea_caso_a_pendiente(self, session, run, empresa):
        """Después de descartar, el caso vuelve a 'pendiente' sin folio."""
        caso, _dte = _mk_caso_emitido(
            run.id, empresa.id, session, numero="4788482-6", folio=32,
        )
        caso_id = caso.id

        from crumbpos.api.routers import certificacion as cert_router

        with patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             patch.object(session, "close", lambda: None):
            cert_router.descartar_folio_caso("77829149-5", caso_id)

        caso_fresco = session.query(CertificacionCaso).filter(
            CertificacionCaso.id == caso_id,
        ).one()
        assert caso_fresco.estado == "pendiente"
        assert caso_fresco.folio is None
        assert caso_fresco.dte_emitido_id is None
        assert caso_fresco.trackid is None
        assert caso_fresco.estado_sii is None
        assert caso_fresco.error_mensaje is None

    def test_descartar_marca_dte_viejo_como_descartado(
        self, session, run, empresa,
    ):
        """El DteEmitido viejo se preserva (no se borra) con estado 'descartado'."""
        caso, dte_viejo = _mk_caso_emitido(
            run.id, empresa.id, session, numero="4788482-6", folio=32,
        )
        dte_viejo_id = dte_viejo.id

        from crumbpos.api.routers import certificacion as cert_router

        with patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             patch.object(session, "close", lambda: None):
            cert_router.descartar_folio_caso("77829149-5", caso.id)

        dte_persistido = session.query(DteEmitido).filter(
            DteEmitido.id == dte_viejo_id,
        ).one()
        assert dte_persistido.estado_sii == "descartado", (
            "El DTE descartado debe quedar marcado como 'descartado' "
            "(VARCHAR(15)-safe para PostgreSQL)."
        )
        # El folio y el XML se preservan — historial auditable
        assert dte_persistido.folio == 32
        assert dte_persistido.xml_firmado == "XML_CON_FOLIO_ORIGINAL"

    def test_descartar_bloqueado_si_avance_declarado(
        self, session, run, empresa,
    ):
        """No se puede descartar un caso con avance ya declarado al SII."""
        caso, _dte = _mk_caso_emitido(run.id, empresa.id, session)
        caso.avance_declarado_at = datetime.now(timezone.utc)
        session.flush()

        from crumbpos.api.routers import certificacion as cert_router

        with patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             patch.object(session, "close", lambda: None):
            with pytest.raises(HTTPException) as exc:
                cert_router.descartar_folio_caso("77829149-5", caso.id)
        assert exc.value.status_code == 409
        assert "avance" in exc.value.detail.lower()

    def test_descartar_bloqueado_si_aprobado(self, session, run, empresa):
        """No se puede descartar un caso ya aprobado."""
        caso, _dte = _mk_caso_emitido(run.id, empresa.id, session)
        caso.aprobado_at = datetime.now(timezone.utc)
        session.flush()

        from crumbpos.api.routers import certificacion as cert_router

        with patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             patch.object(session, "close", lambda: None):
            with pytest.raises(HTTPException) as exc:
                cert_router.descartar_folio_caso("77829149-5", caso.id)
        assert exc.value.status_code == 409
        assert "aprobado" in exc.value.detail.lower()

    def test_descartar_bloqueado_si_caso_pendiente(
        self, session, run, empresa,
    ):
        """Un caso pendiente no tiene folio para descartar — error 422."""
        caso = CertificacionCaso(
            run_id=run.id,
            set_nombre="BASICO",
            numero_caso="4788482-9",
            numero_atencion=4788482,
            tipo_dte=61,
            datos={"items": [{"nombre": "X", "cantidad": 1, "precio_unitario": 1000}]},
            estado="pendiente",
        )
        session.add(caso)
        session.flush()

        from crumbpos.api.routers import certificacion as cert_router

        with patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             patch.object(session, "close", lambda: None):
            with pytest.raises(HTTPException) as exc:
                cert_router.descartar_folio_caso("77829149-5", caso.id)
        # El caso ya está pendiente — no hay nada que descartar
        assert exc.value.status_code in (409, 422)

    def test_descartar_caso_inexistente_da_404(self, session, run, empresa):
        from crumbpos.api.routers import certificacion as cert_router

        with patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             patch.object(session, "close", lambda: None):
            with pytest.raises(HTTPException) as exc:
                cert_router.descartar_folio_caso(
                    "77829149-5", "00000000-0000-0000-0000-000000000000",
                )
        assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════
# Endpoint POST /emitir — ya NO acepta forzar_nuevo_folio
# ══════════════════════════════════════════════════════════════════


class TestEmitirSinForzarNuevoFolio:
    """Tras el refactor, el endpoint /emitir queda limpio y hace una sola cosa."""

    def test_emitir_ya_no_acepta_forzar_nuevo_folio(self):
        """``forzar_nuevo_folio`` fue eliminado — ese flujo ahora usa el
        endpoint de 2 pasos descartar-folio → emitir."""
        import inspect
        from crumbpos.api.routers import certificacion as cert_router

        sig = inspect.signature(cert_router.emitir_caso)
        assert "forzar_nuevo_folio" not in sig.parameters, (
            "forzar_nuevo_folio debe eliminarse — quedó reemplazado por "
            "el flujo de 2 pasos POST /descartar-folio + POST /emitir."
        )


# ══════════════════════════════════════════════════════════════════
# Flujo e2e — descartar luego emitir
# ══════════════════════════════════════════════════════════════════


class TestFlujoDescartarLuegoEmitir:
    """Tras descartar, emitir asigna un folio nuevo del CAF normalmente."""

    def test_descartar_luego_emitir_asigna_folio_nuevo(
        self, session, run, empresa,
    ):
        """El flujo completo: descartar → caso pendiente → emitir → folio nuevo."""
        caso, _dte_viejo = _mk_caso_emitido(
            run.id, empresa.id, session, numero="4788482-6", folio=32,
        )
        caso_id = caso.id

        from crumbpos.api.routers import certificacion as cert_router

        # Paso 1: descartar
        with patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             patch.object(session, "close", lambda: None):
            cert_router.descartar_folio_caso("77829149-5", caso_id)

        caso_tras_descarte = session.query(CertificacionCaso).filter(
            CertificacionCaso.id == caso_id,
        ).one()
        assert caso_tras_descarte.estado == "pendiente"
        assert caso_tras_descarte.folio is None

        # Paso 2: emitir — asigna folio nuevo (mockeamos el servicio)
        resultado_mock = MagicMock()
        resultado_mock.ok = True
        resultado_mock.folio = 35  # próximo del CAF
        resultado_mock.xml_firmado = b"XML_FOLIO_35"
        resultado_mock.ted_xml = "<TED/>"
        resultado_mock.monto_neto = 1000
        resultado_mock.monto_exento = None
        resultado_mock.iva = 190
        resultado_mock.monto_total = 1190

        servicio_mock = MagicMock()
        servicio_mock.emitir_factura = MagicMock(return_value=resultado_mock)

        with patch.object(cert_router, "_get_servicio_for_certificacion",
                          return_value=(servicio_mock, empresa)), \
             patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             patch.object(session, "close", lambda: None):
            cert_router.emitir_caso("77829149-5", caso_id)

        # emitir_factura se llama SIN folio_override (caso en pendiente)
        call_args = servicio_mock.emitir_factura.call_args
        assert call_args is not None
        kwargs = call_args.kwargs
        assert kwargs.get("folio_override") is None

        caso_final = session.query(CertificacionCaso).filter(
            CertificacionCaso.id == caso_id,
        ).one()
        assert caso_final.folio == 35
        assert caso_final.estado == "emitido"
