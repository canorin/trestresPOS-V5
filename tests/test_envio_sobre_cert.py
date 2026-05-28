"""Tests para crumbpos/api/services/envio_sobre_cert.py (Fase 2.d).

Cubre el servicio que arma / envía / consulta el sobre EnvioDTE multi-DTE
del set de certificación. El objetivo es capturar regresiones en:

- La extracción byte-exact del ``<DTE>`` firmado (si esto se rompe, la
  firma interna deja de verificar y el SII tira DTE-3-505).
- La estructura de la Caratula multi-DTE (``SubTotDTE`` por tipo, ordenados
  ascendente por TpoDTE).
- El orden topológico NC antes que ND (R20) dentro del sobre.
- La persistencia de trackid/estado_sii en los casos del set cuando el
  envío es exitoso, y la persistencia de error_mensaje cuando el SII
  rechaza el sobre sin devolver trackid.
- La consulta de estado vía trackid: que lea el trackid existente, llame
  al SII (mock) y escriba el estado_sii y glosa devueltos.

No tocamos el SII real — todas las llamadas a ``enviar_dte`` y
``consultar_estado_envio`` se mockean con ``monkeypatch``, y la firma del
envelope se mockea reemplazando ``servicio._firma`` con un objeto que
devuelve XML pre-cocinado. Los tests corren sobre SQLite in-memory
(::memory::) con el schema de ``crumbpos.db.models.Base``, así que no
requieren ningún archivo ni cert PFX.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import date
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.api.services.emision_dte import EmisorConfig

from crumbpos.api.services import envio_sobre_cert
from crumbpos.api.services.envio_sobre_cert import (
    _cargar_casos_emitidos,
    _construir_caratula_multi,
    _extraer_dte_interno,
    _parsear_estado_sii,
    _parsear_glosa_sii,
    _sha256_hex,
    armar_sobre,
    consultar_estado,
    enviar_sobre,
)
from crumbpos.db.models import (
    Base,
    CertificacionCaso,
    CertificacionRun,
    DteEmitido,
    Empresa,
)


# ══════════════════════════════════════════════════════════════════
# Fixtures de infraestructura
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def session():
    """Sesión SQLAlchemy sobre SQLite in-memory con el schema completo."""
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
        razon_social="Fixture SPA",
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
        estado="iniciado",
        screen_actual=3,
    )
    session.add(r)
    session.commit()
    return r


@pytest.fixture
def servicio_fake():
    """ServicioEmisionDTE simulado — config mínima para caratula/firma.

    _cargar_firma() es un no-op; _firma expone firmar/verificar_firma_xml
    como MagicMocks controlables por test. _obtener_token devuelve un
    string fijo. La config usa ``EmisorConfig`` real (dataclass del core)
    en vez de ``SimpleNamespace``: si el contrato del dataclass cambia,
    este fixture falla al importar con un ``TypeError`` claro en vez de
    un ``AttributeError`` enterrado en runtime.
    """
    servicio = MagicMock()
    servicio.config = EmisorConfig(
        rut="77051056-2",
        razon_social="Fixture SPA",
        giro="SERVICIOS DE PRUEBA",
        acteco=620200,
        direccion="DIRECCION FIXTURE 123",
        comuna="SANTIAGO",
        ciudad="SANTIAGO",
        fecha_resolucion="2014-08-22",
        numero_resolucion=80,
        cert_path="/tmp/fixture.pfx",
        ambiente="certificacion",
    )
    servicio._cargar_firma = MagicMock()

    def fake_firmar(xml, ref_id, type):
        # Devuelve el XML tal cual + un nodo Signature de marker. El
        # test de armar_sobre verifica que el servicio llama type='env'.
        return xml.replace(
            "</EnvioDTE>",
            "<Signature>FAKE-ENVELOPE-SIG</Signature></EnvioDTE>",
        )

    servicio._firma = MagicMock()
    servicio._firma.firmar = MagicMock(side_effect=fake_firmar)
    servicio._firma.verificar_firma_xml = MagicMock(return_value=(0, "ok"))
    servicio._obtener_token = MagicMock(return_value="tok-fake")
    return servicio


# ══════════════════════════════════════════════════════════════════
# Helpers para construir casos y DTEs fake
# ══════════════════════════════════════════════════════════════════


def _dte_xml_fake(tipo: int, folio: int, ref: tuple[int, int] | None = None) -> str:
    """XML sintético con forma EnvioDTE individual → DTE interior.

    El regex _DTE_INNER_RE captura ``<DTE ...>...</DTE>`` non-greedy, así
    que alcanzamos con este shape minimal. No hay firmas reales — es
    solo para probar extracción byte-exact y armado del sobre multi-DTE.
    """
    ref_xml = ""
    if ref is not None:
        tpo_ref, folio_ref = ref
        ref_xml = (
            f"<Referencia>"
            f"<NroLinRef>1</NroLinRef>"
            f"<TpoDocRef>{tpo_ref}</TpoDocRef>"
            f"<FolioRef>{folio_ref}</FolioRef>"
            f"<CodRef>1</CodRef>"
            f"<RazonRef>Anula documento</RazonRef>"
            f"</Referencia>"
        )
    return (
        f'<?xml version="1.0" encoding="ISO-8859-1"?>'
        f'<EnvioDTE xmlns="http://www.sii.cl/SiiDte" version="1.0">'
        f'<SetDTE ID="SetDoc">'
        f"<Caratula version=\"1.0\"><RutEmisor>77051056-2</RutEmisor>"
        f"<RutEnvia>77051056-2</RutEnvia><RutReceptor>60803000-K</RutReceptor>"
        f"<FchResol>2014-08-22</FchResol><NroResol>80</NroResol>"
        f"<TmstFirmaEnv>2026-04-09T10:00:00</TmstFirmaEnv>"
        f"<SubTotDTE><TpoDTE>{tipo}</TpoDTE><NroDTE>1</NroDTE></SubTotDTE>"
        f"</Caratula>"
        f'<DTE version="1.0">'
        f'<Documento ID="F{folio}T{tipo}">'
        f"<Encabezado>"
        f"<IdDoc><TipoDTE>{tipo}</TipoDTE><Folio>{folio}</Folio>"
        f"<FchEmis>2026-04-09</FchEmis></IdDoc>"
        f"<Emisor><RUTEmisor>77051056-2</RUTEmisor></Emisor>"
        f"<Receptor><RUTRecep>66666666-6</RUTRecep></Receptor>"
        f"<Totales><MntTotal>11900</MntTotal></Totales>"
        f"</Encabezado>"
        f"<Detalle><NroLinDet>1</NroLinDet><NmbItem>Test</NmbItem>"
        f"<QtyItem>1</QtyItem><PrcItem>10000</PrcItem>"
        f"<MontoItem>10000</MontoItem></Detalle>"
        f"{ref_xml}"
        f"<TED version=\"1.0\">FAKE-TED</TED>"
        f"<TmstFirma>2026-04-09T10:00:00</TmstFirma>"
        f"</Documento>"
        f"<Signature>FAKE-DOC-SIG-{tipo}-{folio}</Signature>"
        f"</DTE>"
        f"</SetDTE>"
        f"<Signature>FAKE-ENVOLVING-SIG</Signature>"
        f"</EnvioDTE>"
    )


def _make_caso_con_dte(
    session,
    run,
    empresa,
    *,
    tipo: int,
    folio: int,
    numero_caso: str,
    numero_atencion: int = 1,
    set_nombre: str = "BASICO",
    estado: str = "emitido",
    ref: tuple[int, int] | None = None,
) -> tuple[CertificacionCaso, DteEmitido]:
    """Crea un par DteEmitido + CertificacionCaso persistidos en la BD."""
    xml_raw = _dte_xml_fake(tipo, folio, ref=ref)
    xml_b64 = base64.b64encode(xml_raw.encode("ISO-8859-1")).decode("ascii")
    dte = DteEmitido(
        id=f"dte-{tipo}-{folio}",
        empresa_id=empresa.id,
        tipo_dte=tipo,
        folio=folio,
        fecha_emision=date(2026, 4, 9),
        monto_total=11900,
        xml_firmado=xml_b64,
        estado_sii="pendiente",
    )
    session.add(dte)
    caso = CertificacionCaso(
        id=f"caso-{tipo}-{folio}",
        run_id=run.id,
        set_nombre=set_nombre,
        numero_caso=numero_caso,
        numero_atencion=numero_atencion,
        tipo_dte=tipo,
        datos={"items": []},
        estado=estado,
        folio=folio if estado == "emitido" else None,
        dte_emitido_id=dte.id if estado == "emitido" else None,
    )
    session.add(caso)
    session.commit()
    return caso, dte


# ══════════════════════════════════════════════════════════════════
# Helpers puros
# ══════════════════════════════════════════════════════════════════


class TestExtraerDteInterno:
    """Byte-exact extraction del `<DTE>` firmado desde un EnvioDTE b64."""

    def test_extrae_dte_con_atributos(self):
        xml = _dte_xml_fake(33, 127)
        b64 = base64.b64encode(xml.encode("ISO-8859-1")).decode("ascii")
        out = _extraer_dte_interno(b64)
        assert out.startswith('<DTE version="1.0">')
        assert out.endswith("</DTE>")
        assert "<TipoDTE>33</TipoDTE>" in out
        assert "<Folio>127</Folio>" in out

    def test_preserva_bytes_interiores(self):
        xml = _dte_xml_fake(56, 99, ref=(33, 42))
        b64 = base64.b64encode(xml.encode("ISO-8859-1")).decode("ascii")
        out = _extraer_dte_interno(b64)
        # Los dos marcadores de firma interior deben sobrevivir intactos.
        assert "FAKE-DOC-SIG-56-99" in out
        # Y la Referencia también debe ir dentro del DTE.
        assert "<TpoDocRef>33</TpoDocRef>" in out
        assert "<FolioRef>42</FolioRef>" in out

    def test_falla_si_no_hay_dte(self):
        xml = '<?xml version="1.0"?><EnvioDTE><SetDTE></SetDTE></EnvioDTE>'
        b64 = base64.b64encode(xml.encode("ISO-8859-1")).decode("ascii")
        with pytest.raises(ValueError, match="no contiene un elemento <DTE>"):
            _extraer_dte_interno(b64)


class TestConstruirCaratulaMulti:
    """La Caratula multi-DTE debe agregar SubTotDTE ordenados por tipo."""

    def test_subtotales_ordenados_ascendente(self, servicio_fake):
        # Pasamos deliberadamente en orden no-ascendente.
        xml = _construir_caratula_multi(
            servicio_fake, {61: 2, 33: 4, 34: 1},
        )
        # Los SubTotDTE deben aparecer en orden 33, 34, 61.
        pos_33 = xml.index("<TpoDTE>33</TpoDTE>")
        pos_34 = xml.index("<TpoDTE>34</TpoDTE>")
        pos_61 = xml.index("<TpoDTE>61</TpoDTE>")
        assert pos_33 < pos_34 < pos_61

    def test_contiene_datos_emisor(self, servicio_fake):
        xml = _construir_caratula_multi(servicio_fake, {33: 1})
        assert "<RutEmisor>77051056-2</RutEmisor>" in xml
        assert "<RutEnvia>77051056-2</RutEnvia>" in xml
        assert "<RutReceptor>60803000-K</RutReceptor>" in xml
        assert "<FchResol>2014-08-22</FchResol>" in xml
        assert "<NroResol>80</NroResol>" in xml

    def test_rut_firmante_override(self, servicio_fake):
        servicio_fake.config.rut_firmante = "11111111-1"
        xml = _construir_caratula_multi(servicio_fake, {33: 1})
        assert "<RutEnvia>11111111-1</RutEnvia>" in xml

    def test_un_subtotdte_por_tipo(self, servicio_fake):
        xml = _construir_caratula_multi(servicio_fake, {33: 4, 34: 2})
        assert xml.count("<SubTotDTE>") == 2
        assert "<NroDTE>4</NroDTE>" in xml
        assert "<NroDTE>2</NroDTE>" in xml


class TestParsersRespuestaSii:
    def test_estado_extrae_codigo(self):
        raw = (
            "<SII:RESPUESTA><SII:RESP_HDR>"
            "<ESTADO>EPR</ESTADO><GLOSA>OK</GLOSA>"
            "</SII:RESP_HDR></SII:RESPUESTA>"
        )
        assert _parsear_estado_sii(raw) == "EPR"

    def test_estado_vacio_devuelve_none(self):
        assert _parsear_estado_sii("") is None
        assert _parsear_estado_sii("<SII:RESPUESTA></SII:RESPUESTA>") is None

    def test_glosa_err_tiene_prioridad(self):
        raw = (
            "<X><GLOSA_ERR>Schema error</GLOSA_ERR>"
            "<GLOSA_ESTADO>Rechazado</GLOSA_ESTADO>"
            "<GLOSA>info</GLOSA></X>"
        )
        assert _parsear_glosa_sii(raw) == "Schema error"

    def test_glosa_estado_fallback(self):
        raw = "<X><GLOSA_ESTADO>En revision</GLOSA_ESTADO></X>"
        assert _parsear_glosa_sii(raw) == "En revision"

    def test_glosa_fallback(self):
        assert _parsear_glosa_sii("<X><GLOSA>simple</GLOSA></X>") == "simple"

    def test_glosa_sin_match(self):
        assert _parsear_glosa_sii("<X></X>") is None
        assert _parsear_glosa_sii("") is None


class TestSha256Hex:
    def test_hex_digest_matches_stdlib(self):
        data = b"hola mundo"
        assert _sha256_hex(data) == hashlib.sha256(data).hexdigest()


# ══════════════════════════════════════════════════════════════════
# _cargar_casos_emitidos
# ══════════════════════════════════════════════════════════════════


class TestCargarCasosEmitidos:
    def test_ok_todos_emitidos(self, session, run, empresa):
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=2, numero_caso="4768464-2",
        )
        casos = _cargar_casos_emitidos(session, run, "BASICO")
        assert len(casos) == 2
        assert [c.numero_caso for c in casos] == ["4768464-1", "4768464-2"]

    def test_set_vacio_falla(self, session, run):
        with pytest.raises(ValueError, match="No hay casos para el set"):
            _cargar_casos_emitidos(session, run, "BASICO")

    def test_caso_pendiente_bloquea(self, session, run, empresa):
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=2, numero_caso="4768464-2",
            estado="pendiente",
        )
        with pytest.raises(ValueError, match="casos sin emitir"):
            _cargar_casos_emitidos(session, run, "BASICO")

    def test_caso_emitido_sin_dte_id_falla(self, session, run, empresa):
        # Forzamos el escenario: caso marcado emitido pero sin dte_emitido_id.
        caso = CertificacionCaso(
            id="caso-huerfano",
            run_id=run.id,
            set_nombre="BASICO",
            numero_caso="4768464-X",
            numero_atencion=1,
            tipo_dte=33,
            datos={"items": []},
            estado="emitido",
            folio=5,
            dte_emitido_id=None,
        )
        session.add(caso)
        session.commit()
        with pytest.raises(ValueError, match="sin dte_emitido_id"):
            _cargar_casos_emitidos(session, run, "BASICO")


# ══════════════════════════════════════════════════════════════════
# armar_sobre — orquestación completa con firma mockeada
# ══════════════════════════════════════════════════════════════════


class TestArmarSobre:
    def test_estructura_multi_dte(self, session, run, empresa, servicio_fake):
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=2, numero_caso="4768464-2",
        )
        _make_caso_con_dte(
            session, run, empresa,
            tipo=34, folio=1, numero_caso="4768464-3",
        )
        resultado = armar_sobre(session, run, "BASICO", servicio_fake, empresa)

        # Estructura del resultado.
        assert set(resultado.keys()) == {
            "xml_bytes", "sha256", "resumen_por_tipo", "folios",
            "casos_ids", "url_sii", "es_boleta",
        }
        assert isinstance(resultado["xml_bytes"], bytes)
        assert resultado["sha256"] == hashlib.sha256(resultado["xml_bytes"]).hexdigest()
        assert resultado["resumen_por_tipo"] == {33: 2, 34: 1}
        assert len(resultado["casos_ids"]) == 3

        # El XML debe empezar con la declaración ISO-8859-1.
        xml_str = resultado["xml_bytes"].decode("ISO-8859-1")
        assert xml_str.startswith('<?xml version="1.0" encoding="ISO-8859-1"?>')
        # SetDTE con ID correcto + caratula multi-DTE con ambos SubTotDTE.
        assert '<SetDTE ID="SetDoc">' in xml_str
        assert "<TpoDTE>33</TpoDTE>" in xml_str
        assert "<NroDTE>2</NroDTE>" in xml_str
        assert "<TpoDTE>34</TpoDTE>" in xml_str
        assert "<NroDTE>1</NroDTE>" in xml_str
        # Tres DTEs en el sobre (uno por caso).
        assert xml_str.count("<DTE version=\"1.0\">") == 3
        # La firma del envelope fue llamada con type='env'.
        args, kwargs = servicio_fake._firma.firmar.call_args
        assert kwargs.get("type") == "env" or "env" in args
        # El sobre pasó por la verificación pre-envío.
        servicio_fake._firma.verificar_firma_xml.assert_called_once()

    def test_orden_nc_antes_nd_r20(
        self, session, run, empresa, servicio_fake,
    ):
        """R20: NC (T61) antes que ND (T56) cuando apuntan al mismo doc."""
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=10, numero_caso="4768464-1",
        )
        _make_caso_con_dte(
            session, run, empresa,
            tipo=56, folio=20, numero_caso="4768464-2",
            ref=(33, 10),
        )
        _make_caso_con_dte(
            session, run, empresa,
            tipo=61, folio=30, numero_caso="4768464-3",
            ref=(33, 10),
        )
        resultado = armar_sobre(session, run, "BASICO", servicio_fake, empresa)
        xml_str = resultado["xml_bytes"].decode("ISO-8859-1")

        # Posiciones de cada folio en el XML final.
        pos_f10 = xml_str.index("<Folio>10</Folio>")
        pos_f30 = xml_str.index("<Folio>30</Folio>")
        pos_f20 = xml_str.index("<Folio>20</Folio>")

        # F33=10 primero (todos dependen de él), luego NC F61=30,
        # luego ND F56=20 — esta regla mantiene sano CodRef=3.
        assert pos_f10 < pos_f30 < pos_f20

        # Y los folios en el resultado siguen el mismo orden.
        folios = [(f["tipo"], f["folio"]) for f in resultado["folios"]]
        assert folios == [(33, 10), (61, 30), (56, 20)]

    def test_orden_referencias_cruzadas_simulacion(
        self, session, run, empresa, servicio_fake,
    ):
        """Grafo flexible del set de simulación: refs T61↔T56 cruzadas.

        Setup:
        - T33 F100 (factura raíz)
        - T61 F200 → T33 F100 (NC anula factura)
        - T56 F300 → T61 F200 (ND anula NC — cruzado)
        - T61 F400 → T56 F300 (NC anula ND — segundo cruce)

        Invariante que ``ordenar_por_dependencias`` debe respetar:
        para cada par (A, B) donde A referencia a B, B aparece antes
        que A en el sobre, independiente del tipo de doc.

        Este test cubre la corrección del grafo flexible del 2026-04-23:
        antes T61 solo refería facturas y T56 solo refería T61. Ahora
        pueden cruzarse libremente y el sobre debe seguir ordenado.
        """
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=100, numero_caso="SIM-001",
            set_nombre="SIMULACION",
        )
        _make_caso_con_dte(
            session, run, empresa,
            tipo=61, folio=200, numero_caso="SIM-002",
            set_nombre="SIMULACION",
            ref=(33, 100),
        )
        _make_caso_con_dte(
            session, run, empresa,
            tipo=56, folio=300, numero_caso="SIM-003",
            set_nombre="SIMULACION",
            ref=(61, 200),  # ND referenciando NC — cross-ref
        )
        _make_caso_con_dte(
            session, run, empresa,
            tipo=61, folio=400, numero_caso="SIM-004",
            set_nombre="SIMULACION",
            ref=(56, 300),  # NC referenciando ND — segundo cross-ref
        )
        resultado = armar_sobre(
            session, run, "SIMULACION", servicio_fake, empresa,
        )
        xml_str = resultado["xml_bytes"].decode("ISO-8859-1")

        pos_f100 = xml_str.index("<Folio>100</Folio>")
        pos_f200 = xml_str.index("<Folio>200</Folio>")
        pos_f300 = xml_str.index("<Folio>300</Folio>")
        pos_f400 = xml_str.index("<Folio>400</Folio>")

        # Cadena: F100 → F200 → F300 → F400.
        # Cada nodo debe preceder al que lo referencia.
        assert pos_f100 < pos_f200, "T33 F100 debe preceder a T61 F200"
        assert pos_f200 < pos_f300, "T61 F200 debe preceder a T56 F300"
        assert pos_f300 < pos_f400, "T56 F300 debe preceder a T61 F400"

        folios = [(f["tipo"], f["folio"]) for f in resultado["folios"]]
        assert folios == [(33, 100), (61, 200), (56, 300), (61, 400)]

    def test_caso_pendiente_hace_fallar_armar(
        self, session, run, empresa, servicio_fake,
    ):
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=2, numero_caso="4768464-2",
            estado="pendiente",
        )
        with pytest.raises(ValueError, match="sin emitir"):
            armar_sobre(session, run, "BASICO", servicio_fake, empresa)

    def test_verificacion_firma_falla_aborta(
        self, session, run, empresa, servicio_fake,
    ):
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        servicio_fake._firma.verificar_firma_xml.return_value = (
            2, "DTE-3-505: Firma no valida",
        )
        with pytest.raises(RuntimeError, match="Firma del sobre inv"):
            armar_sobre(session, run, "BASICO", servicio_fake, empresa)


# ══════════════════════════════════════════════════════════════════
# enviar_sobre — persistencia del trackid / rechazo
# ══════════════════════════════════════════════════════════════════


class TestEnviarSobre:
    def test_ok_persiste_trackid_y_estado_sii(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        c1, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        c2, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=2, numero_caso="4768464-2",
        )

        def fake_enviar_dte(**kwargs):
            return {
                "track_id": "TRACK-12345",
                "status": "0",
                "glosa": "Envío aceptado",
                "raw": "<HDR>ok</HDR>",
            }
        monkeypatch.setattr(
            envio_sobre_cert, "enviar_dte", fake_enviar_dte,
        )

        resultado = enviar_sobre(
            session, run, "BASICO", servicio_fake, empresa,
        )

        assert resultado["ok"] is True
        assert resultado["trackid"] == "TRACK-12345"
        assert resultado["casos_actualizados"] == 2
        # R8: estado_sii='enviado', pero estado del caso sigue 'emitido'.
        session.refresh(c1)
        session.refresh(c2)
        assert c1.trackid == "TRACK-12345"
        assert c2.trackid == "TRACK-12345"
        assert c1.estado_sii == "enviado"
        assert c2.estado_sii == "enviado"
        assert c1.estado == "emitido"
        assert c2.estado == "emitido"
        assert c1.error_mensaje is None
        assert c2.error_mensaje is None

    def test_rechazo_guarda_error_mensaje_sin_trackid(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        c1, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )

        def fake_enviar_dte(**kwargs):
            return {
                "track_id": None,
                "status": "5",
                "glosa": "Firma no valida",
                "raw": "<HDR>error</HDR>",
            }
        monkeypatch.setattr(
            envio_sobre_cert, "enviar_dte", fake_enviar_dte,
        )

        resultado = enviar_sobre(
            session, run, "BASICO", servicio_fake, empresa,
        )

        assert resultado["ok"] is False
        assert resultado["trackid"] is None
        session.refresh(c1)
        assert c1.trackid is None
        assert c1.estado == "emitido"  # no cambia el estado del caso
        assert c1.error_mensaje is not None
        assert "Firma no valida" in c1.error_mensaje

    def test_caso_pendiente_propaga_valueerror(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
            estado="pendiente",
        )
        # enviar_dte no debería ser llamado — el fallo ocurre antes.
        monkeypatch.setattr(
            envio_sobre_cert, "enviar_dte",
            lambda **kw: pytest.fail("enviar_dte no debería llamarse"),
        )
        with pytest.raises(ValueError, match="sin emitir"):
            enviar_sobre(session, run, "BASICO", servicio_fake, empresa)


# ══════════════════════════════════════════════════════════════════
# consultar_estado — lectura de trackid + persistencia de estado_sii
# ══════════════════════════════════════════════════════════════════


class TestConsultarEstado:
    def test_lee_trackid_y_persiste_estado(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        c1, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        c2, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=2, numero_caso="4768464-2",
        )
        # Pre-condición: ambos casos tienen el mismo trackid persistido.
        c1.trackid = "TRACK-9"
        c1.estado_sii = "enviado"
        c2.trackid = "TRACK-9"
        c2.estado_sii = "enviado"
        session.commit()

        def fake_consultar(**kwargs):
            assert kwargs["track_id"] == "TRACK-9"
            return {"raw": "<X><ESTADO>EPR</ESTADO></X>"}
        monkeypatch.setattr(
            envio_sobre_cert, "consultar_estado_envio", fake_consultar,
        )

        resultado = consultar_estado(
            session, run, "BASICO", servicio_fake, empresa,
        )
        assert resultado["trackid"] == "TRACK-9"
        assert resultado["estado_sii"] == "EPR"
        assert resultado["casos_actualizados"] == 2

        session.refresh(c1)
        session.refresh(c2)
        assert c1.estado_sii == "EPR"
        assert c2.estado_sii == "EPR"

    def test_rechazo_guarda_glosa_en_error_mensaje(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        c1, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        c1.trackid = "TRACK-RCH"
        c1.estado_sii = "enviado"
        session.commit()

        def fake_consultar(**kwargs):
            return {
                "raw": (
                    "<X><ESTADO>RCH</ESTADO>"
                    "<GLOSA_ERR>Schema invalido</GLOSA_ERR></X>"
                ),
            }
        monkeypatch.setattr(
            envio_sobre_cert, "consultar_estado_envio", fake_consultar,
        )

        resultado = consultar_estado(
            session, run, "BASICO", servicio_fake, empresa,
        )
        assert resultado["estado_sii"] == "RCH"
        assert "Schema invalido" in (resultado["glosa"] or "")

        session.refresh(c1)
        assert c1.estado_sii == "RCH"
        assert c1.error_mensaje == "Schema invalido"

    def test_estado_ok_limpia_error_previo(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        c1, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        c1.trackid = "TRACK-R"
        c1.estado_sii = "enviado"
        c1.error_mensaje = "error anterior"
        session.commit()

        monkeypatch.setattr(
            envio_sobre_cert, "consultar_estado_envio",
            lambda **kw: {"raw": "<X><ESTADO>EPR</ESTADO></X>"},
        )
        consultar_estado(
            session, run, "BASICO", servicio_fake, empresa,
        )
        session.refresh(c1)
        assert c1.error_mensaje is None

    def test_sin_trackid_falla(
        self, session, run, empresa, servicio_fake,
    ):
        _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        with pytest.raises(ValueError, match="tiene trackid"):
            consultar_estado(
                session, run, "BASICO", servicio_fake, empresa,
            )

    def test_trackids_distintos_falla(
        self, session, run, empresa, servicio_fake,
    ):
        c1, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=1, numero_caso="4768464-1",
        )
        c2, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=33, folio=2, numero_caso="4768464-2",
        )
        c1.trackid = "TRACK-A"
        c2.trackid = "TRACK-B"
        session.commit()
        with pytest.raises(ValueError, match="trackids distintos"):
            consultar_estado(
                session, run, "BASICO", servicio_fake, empresa,
            )

    def test_sin_casos_falla(self, session, run, empresa, servicio_fake):
        with pytest.raises(ValueError, match="No hay casos"):
            consultar_estado(
                session, run, "BASICO", servicio_fake, empresa,
            )

    def test_boleta_epr_auto_declara_avance(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        """EPR para boletas (T39) debe auto-stampar avance_declarado_at.

        El portal SII no tiene opción "Declarar Avance" para boletas, por
        lo que el wizard no puede pedirle al usuario que lo haga.  Al
        consultar estado y recibir EPR, el servicio auto-declara el avance
        para que marcar_aprobado pueda proceder sin bloqueo.
        """
        c1, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=39, folio=1, numero_caso="9999-1", set_nombre="BOLETAS",
        )
        c2, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=39, folio=2, numero_caso="9999-2", set_nombre="BOLETAS",
        )
        c1.trackid = "TRACK-BOL"
        c1.estado_sii = "enviado"
        c2.trackid = "TRACK-BOL"
        c2.estado_sii = "enviado"
        session.commit()

        servicio_fake._obtener_token_boleta = MagicMock(return_value="tok-bol")
        monkeypatch.setattr(
            envio_sobre_cert,
            "consultar_estado_boleta",
            lambda **kw: {"estado": "EPR", "glosa": None},
        )

        resultado = consultar_estado(
            session, run, "BOLETAS", servicio_fake, empresa,
        )
        assert resultado["estado_sii"] == "EPR"

        session.refresh(c1)
        session.refresh(c2)
        assert c1.avance_declarado_at is not None, (
            "avance_declarado_at debe auto-setearse para boletas con EPR"
        )
        assert c2.avance_declarado_at is not None

    def test_boleta_rechazo_no_auto_declara_avance(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        """Rechazo (RCH) para boletas NO debe auto-declarar avance."""
        c1, _ = _make_caso_con_dte(
            session, run, empresa,
            tipo=39, folio=1, numero_caso="9998-1", set_nombre="BOLETAS",
        )
        c1.trackid = "TRACK-BOL-R"
        c1.estado_sii = "enviado"
        session.commit()

        servicio_fake._obtener_token_boleta = MagicMock(return_value="tok-bol")
        monkeypatch.setattr(
            envio_sobre_cert,
            "consultar_estado_boleta",
            lambda **kw: {"estado": "RCH", "glosa": "Rechazado"},
        )

        consultar_estado(session, run, "BOLETAS", servicio_fake, empresa)

        session.refresh(c1)
        assert c1.avance_declarado_at is None, (
            "avance_declarado_at NO debe setearse si el SII rechaza las boletas"
        )
