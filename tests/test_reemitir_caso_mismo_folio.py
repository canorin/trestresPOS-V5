"""Tests para re-emisión de un caso de certificación reutilizando el folio.

Escenario operativo — 2026-04-22, set BASICO de 77829149-5:

Los 8 casos del set BASICO fueron emitidos (XML firmado, folio consumido
del CAF) pero el sobre fue rechazado por el SII con STATUS=7 (esquema
inválido). Formalmente el SII no recepcionó nada, los folios no están
quemados fiscalmente. Después de corregir el bug del core (GiroRecep >
maxLength), necesitamos regenerar los XMLs con el MISMO folio —
descartar el XML viejo no debe quemar el folio en el CAF.

Reglas:
- ``emitir_factura(folio_override=X)`` reutiliza folio X sin avanzar el
  contador del CAF. Falla si el CAF no contiene ese folio.
- El router ``/casos/{rut}/{caso_id}/emitir`` detecta ``estado='emitido'``
  y pasa ``folio_override=caso.folio`` para regenerar.
- Si el caso tiene ``avance_declarado_at`` o ``aprobado_at`` seteados,
  la re-emisión se rechaza con 409 — ese DTE ya fue declarado al SII o
  aprobado, reemitirlo sería fraude.
"""
from __future__ import annotations

from datetime import datetime
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
        giro="SERVICIOS DE PUBLICIDAD PRESTADOS POR EMPRESAS",
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
    numero: str = "4788482-1",
    folio: int = 53,
    tipo_dte: int = 33,
) -> tuple[CertificacionCaso, DteEmitido]:
    """Crea un caso que simula 'emitido con sobre rechazado'."""
    from datetime import date as _date

    dte = DteEmitido(
        empresa_id=empresa_id,
        tipo_dte=tipo_dte,
        folio=folio,
        fecha_emision=_date(2026, 4, 22),
        receptor_rut="77829149-5",
        receptor_razon="GRUPO TRESTRES SPA",
        monto_neto=1000,
        iva=190,
        monto_total=1190,
        xml_firmado="XML_VIEJO_CON_GIRO_MALO",
        estado_sii="pendiente",
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
        emitido_at=datetime.utcnow(),
        error_mensaje="Rechazo SII sobre 'BASICO' [status=ERROR]: 7",
    )
    session.add(caso)
    session.flush()
    return caso, dte


# ══════════════════════════════════════════════════════════════════
# Servicio: emitir_factura(folio_override=X)
# ══════════════════════════════════════════════════════════════════


class TestEmitirFacturaConFolioOverride:
    """La firma pública del servicio acepta folio_override y lo usa sin
    avanzar el contador del CAF."""

    def test_servicio_acepta_folio_override(self):
        """El método ``emitir_factura`` debe tener el parámetro ``folio_override``.
        Si no existe, hay que agregarlo al servicio."""
        import inspect
        from crumbpos.api.services.emision_dte import ServicioEmisionDTE

        sig = inspect.signature(ServicioEmisionDTE.emitir_factura)
        assert "folio_override" in sig.parameters, (
            "ServicioEmisionDTE.emitir_factura debe aceptar folio_override "
            "para permitir regeneración con mismo folio tras rechazo de sobre."
        )
        # Default debe ser None (no-op cuando no se pasa)
        assert sig.parameters["folio_override"].default is None

    def test_folio_override_no_avanza_contador_caf(self):
        """Si se pasa folio_override, no se llama a siguiente_folio."""
        from crumbpos.api.services.emision_dte import ServicioEmisionDTE

        # Mock el servicio para aislar la lógica de asignación de folio
        servicio = ServicioEmisionDTE.__new__(ServicioEmisionDTE)
        servicio._caf_manager_db = MagicMock()
        servicio._caf_manager_db.siguiente_folio = MagicMock()
        servicio._caf_manager_db.obtener_caf = MagicMock(return_value=None)
        servicio._caf_manager = None
        servicio._cargar_firma = MagicMock()

        # Mock config mínimo
        servicio.config = MagicMock()
        servicio.config.rut = "77829149-5"

        req = MagicMock()
        req.tipo_dte = 33
        req.items = [{"nombre": "X", "cantidad": 1, "precio_unitario": 1000}]
        req.referencias = None
        req.descuentos_globales = None
        req.caso_set = None
        req.oc_numero = None
        req.receptor_rut = "77829149-5"
        req.fma_pago = None
        req.ind_traslado = None
        req.tipo_despacho = None
        req.fecha_vencimiento = None
        req.indicador_servicio = None

        # Para esta prueba sólo verificamos que el flujo lee obtener_caf en
        # vez de siguiente_folio cuando hay folio_override. El primer return
        # (CAF None) hará que falle con 'No hay CAF disponible' — es ok, sólo
        # nos interesa la mecánica de folio.
        with patch.object(servicio, "_validar_request", return_value=None):
            servicio.emitir_factura(req, enviar_sii=False, folio_override=53)

        # siguiente_folio NO debe haberse llamado
        servicio._caf_manager_db.siguiente_folio.assert_not_called()
        # obtener_caf SÍ — con el folio override
        servicio._caf_manager_db.obtener_caf.assert_called_once_with(33, 53)


# ══════════════════════════════════════════════════════════════════
# Router: re-emisión desde estado='emitido'
# ══════════════════════════════════════════════════════════════════


class TestRouterReemitirCasoEmitido:
    def test_reemite_si_caso_en_emitido_sin_aprobacion(self, session, run, empresa):
        """Caso en 'emitido' con sobre rechazado → se puede re-emitir."""
        caso, _dte_viejo = _mk_caso_emitido(
            run.id, empresa.id, session,
            numero="4788482-1", folio=53,
        )

        # Mock el servicio para no cargar CAFs reales
        from crumbpos.api.routers import certificacion as cert_router

        resultado_mock = MagicMock()
        resultado_mock.ok = True
        resultado_mock.folio = 53
        resultado_mock.xml_firmado = b"XML_NUEVO_CON_GIRO_TRUNCADO"
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
                          return_value=session):
            cert_router.emitir_caso("77829149-5", caso.id)

        # emitir_factura debe haberse llamado con folio_override=53
        call_args = servicio_mock.emitir_factura.call_args
        assert call_args is not None
        kwargs = call_args.kwargs
        assert kwargs.get("folio_override") == 53, (
            "Re-emisión debe pasar folio_override=53 para reutilizar el folio."
        )

    def test_reemite_bloqueado_si_avance_declarado(self, session, run, empresa):
        caso, _dte = _mk_caso_emitido(
            run.id, empresa.id, session,
            numero="4788482-1", folio=53,
        )
        caso.avance_declarado_at = datetime.utcnow()
        session.commit()

        from crumbpos.api.routers import certificacion as cert_router

        with patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             pytest.raises(HTTPException) as exc:
            cert_router.emitir_caso("77829149-5", caso.id)

        assert exc.value.status_code == 409
        assert "declarado" in exc.value.detail.lower() or \
               "avance" in exc.value.detail.lower()

    def test_reemite_bloqueado_si_aprobado(self, session, run, empresa):
        caso, _dte = _mk_caso_emitido(
            run.id, empresa.id, session,
            numero="4788482-1", folio=53,
        )
        caso.aprobado_at = datetime.utcnow()
        session.commit()

        from crumbpos.api.routers import certificacion as cert_router

        with patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             pytest.raises(HTTPException) as exc:
            cert_router.emitir_caso("77829149-5", caso.id)

        assert exc.value.status_code == 409
        assert "aprobado" in exc.value.detail.lower()

    def test_reemision_actualiza_xml_y_preserva_folio(self, session, run, empresa):
        """Después de re-emitir, el DteEmitido tiene xml nuevo, folio viejo."""
        caso, dte_viejo = _mk_caso_emitido(
            run.id, empresa.id, session,
            numero="4788482-1", folio=53,
        )
        xml_viejo = dte_viejo.xml_firmado
        caso_id = caso.id
        dte_id_viejo = dte_viejo.id

        from crumbpos.api.routers import certificacion as cert_router

        resultado_mock = MagicMock()
        resultado_mock.ok = True
        resultado_mock.folio = 53
        resultado_mock.xml_firmado = b"XML_NUEVO_BYTES"
        resultado_mock.ted_xml = "<TED>nuevo</TED>"
        resultado_mock.monto_neto = 1000
        resultado_mock.monto_exento = None
        resultado_mock.iva = 190
        resultado_mock.monto_total = 1190

        servicio_mock = MagicMock()
        servicio_mock.emitir_factura = MagicMock(return_value=resultado_mock)

        # El router llama ``session.close()`` en el finally. Sustituimos
        # por no-op para poder inspeccionar el estado post-commit.
        with patch.object(cert_router, "_get_servicio_for_certificacion",
                          return_value=(servicio_mock, empresa)), \
             patch.object(cert_router, "get_empresa_registro",
                          return_value=MagicMock()), \
             patch.object(cert_router, "get_empresa_db_session",
                          return_value=session), \
             patch.object(session, "close", lambda: None):
            cert_router.emitir_caso("77829149-5", caso_id)

        # Re-queremos los rows frescos tras el commit
        caso_fresco = session.query(CertificacionCaso).filter(
            CertificacionCaso.id == caso_id,
        ).one()
        assert caso_fresco.folio == 53, "El folio debe conservarse"
        assert caso_fresco.estado == "emitido"
        assert caso_fresco.error_mensaje is None

        # El DteEmitido asociado al caso debe tener el XML nuevo.
        # Debe ser el MISMO id (update in-place, no row nuevo).
        dte_tras = session.get(DteEmitido, caso_fresco.dte_emitido_id)
        assert dte_tras is not None
        assert dte_tras.id == dte_id_viejo, (
            "Re-emisión debería actualizar el DteEmitido existente, no crear otro."
        )
        assert dte_tras.folio == 53
        import base64
        assert base64.b64decode(dte_tras.xml_firmado) == b"XML_NUEVO_BYTES"
        assert dte_tras.xml_firmado != xml_viejo


# ══════════════════════════════════════════════════════════════════
# Emisión inicial con folio PRE-ASIGNADO (set de simulación)
#
# El set de simulación reserva los 16 folios en /simulacion/.../preview
# (llama a siguiente_folio una vez por tipo × cantidad) y los persiste
# en ``caso.folio`` antes de emitir. Cuando el usuario hace click en
# "Emitir" por primera vez, el router DEBE respetar ese folio y NO
# consumir otro — si no, se queman folios al doble (bug detectado
# 2026-04-23 en pantalla 5 del wizard certificación).
# ══════════════════════════════════════════════════════════════════


class TestRouterEmisionInicialConFolioPreAsignado:
    def test_emision_inicial_con_folio_preasignado_respeta_folio(
        self, session, run, empresa,
    ):
        """Caso 'pendiente' con folio pre-asignado → emit usa ese folio."""
        # Caso estilo simulación: estado pendiente, folio ya seteado.
        caso = CertificacionCaso(
            run_id=run.id,
            set_nombre="SIMULACION",
            numero_caso="SIM-02",
            numero_atencion=1002,
            tipo_dte=33,
            datos={
                "items": [
                    {"nombre": "Producto test", "cantidad": 1,
                     "precio_unitario": 1000},
                ],
                "slot": 2,
            },
            estado="pendiente",
            folio=102,  # pre-reservado por /simulacion/.../preview
        )
        session.add(caso)
        session.flush()
        caso_id = caso.id

        from crumbpos.api.routers import certificacion as cert_router

        resultado_mock = MagicMock()
        resultado_mock.ok = True
        resultado_mock.folio = 102
        resultado_mock.xml_firmado = b"XML_SIM02"
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

        # El router debe haber pasado folio_override=102, NO None.
        call_args = servicio_mock.emitir_factura.call_args
        assert call_args is not None
        kwargs = call_args.kwargs
        assert kwargs.get("folio_override") == 102, (
            "Emisión inicial con folio pre-asignado (set simulación) debe "
            "pasar folio_override=caso.folio para NO consumir otro folio "
            "del CAF. Si pasa None, se queman folios al doble."
        )

    def test_emision_inicial_sin_folio_no_usa_override(
        self, session, run, empresa,
    ):
        """Caso 'pendiente' SIN folio pre-asignado (set de pruebas estándar)
        → folio_override=None, el servicio asigna uno con siguiente_folio."""
        caso = CertificacionCaso(
            run_id=run.id,
            set_nombre="BASICO",
            numero_caso="4788482-3",
            numero_atencion=4788482,
            tipo_dte=33,
            datos={
                "items": [
                    {"nombre": "Producto", "cantidad": 1,
                     "precio_unitario": 1000},
                ],
            },
            estado="pendiente",
            # folio NO seteado — set de pruebas no pre-reserva.
        )
        session.add(caso)
        session.flush()
        caso_id = caso.id

        from crumbpos.api.routers import certificacion as cert_router

        resultado_mock = MagicMock()
        resultado_mock.ok = True
        resultado_mock.folio = 55  # asignado por siguiente_folio
        resultado_mock.xml_firmado = b"XML"
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

        call_args = servicio_mock.emitir_factura.call_args
        kwargs = call_args.kwargs
        assert kwargs.get("folio_override") is None, (
            "Set de pruebas estándar (sin folio pre-asignado) debe pasar "
            "folio_override=None para que el servicio asigne con "
            "siguiente_folio."
        )
