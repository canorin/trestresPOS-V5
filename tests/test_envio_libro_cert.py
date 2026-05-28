"""Tests para crumbpos/api/services/envio_libro_cert.py (Fase 3.d).

Cubre el servicio que genera / envía / consulta libros de certificación
(ventas, compras, guías). El objetivo es capturar regresiones en:

- El enriquecimiento de entradas de compras: CompraLibro (parser) → dict
  listo para generar_libro_compras. Cubre IVANoRec, IVARetTotal, OtrosImp.
- La generación del libro: llama al generador correcto según tipo_libro,
  firma con type="libro", persiste XML en CertificacionLibro.xml_libro.
- El envío al SII: genera si falta XML, envía, persiste trackid/estado_sii.
- La consulta de estado: lee trackid, llama al SII (mock), escribe
  estado_sii y error_mensaje.

No tocamos el SII real — ``enviar_dte`` y ``consultar_estado_envio`` se
mockean. La firma se mockea con un ``MagicMock`` que devuelve el XML sin
cambios. Los tests corren sobre SQLite in-memory.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.api.services import envio_libro_cert
from crumbpos.api.services.emision_dte import EmisorConfig
from crumbpos.api.services.envio_libro_cert import (
    _derivar_periodo,
    _parsear_estado_sii,
    _parsear_glosa_sii,
    _sha256_hex,
    _validar_xml_libro_ventas_sin_lbr2,
    consultar_estado_libro,
    enriquecer_entradas_compras,
    enviar_libro,
    generar_libro,
    reiniciar_envio_libro,
)
from crumbpos.db.models import (
    Base,
    CertificacionCaso,
    CertificacionLibro,
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
        estado="emitiendo",
        screen_actual=3,
    )
    session.add(r)
    session.commit()
    return r


@pytest.fixture
def servicio_fake():
    """ServicioEmisionDTE simulado con firma mock.

    La ``config`` se construye con ``EmisorConfig`` real (no
    ``SimpleNamespace``) para que el contrato del dataclass del core
    esté vigente: si mañana se agrega un campo obligatorio a
    ``EmisorConfig``, este fixture falla al importar con un ``TypeError``
    claro, en vez de dejarlo explotar como ``AttributeError`` en runtime
    cuando el servicio productivo intenta leer el campo.
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
        return xml + "<Signature>FAKE-LIBRO-SIG</Signature>"

    servicio._firma = MagicMock()
    servicio._firma.firmar = MagicMock(side_effect=fake_firmar)
    servicio._obtener_token = MagicMock(return_value="tok-fake")
    return servicio


def _make_dtes(session, empresa, tipos_folios):
    """Crea DteEmitido para la lista de (tipo, folio)."""
    for tipo, folio in tipos_folios:
        xml_fake = f"<DTE><T{tipo}>F{folio}</T{tipo}></DTE>"
        session.add(DteEmitido(
            empresa_id=empresa.id,
            tipo_dte=tipo,
            folio=folio,
            fecha_emision=date(2026, 4, 9),
            receptor_rut="77051056-2",
            receptor_razon="Fixture SPA",
            monto_neto=10000,
            monto_exento=0,
            iva=1900,
            monto_total=11900,
            xml_firmado=base64.b64encode(xml_fake.encode()).decode(),
            estado_sii="pendiente",
        ))
    session.commit()


def _make_libro(session, run, tipo_libro, datos=None, numero_atencion=None):
    """Crea un CertificacionLibro."""
    lib = CertificacionLibro(
        run_id=run.id,
        tipo_libro=tipo_libro,
        datos=datos,
        numero_atencion=numero_atencion,
    )
    session.add(lib)
    session.commit()
    session.refresh(lib)
    return lib


# ══════════════════════════════════════════════════════════════════
# Tests: helpers internos
# ══════════════════════════════════════════════════════════════════


class TestParsers:
    def test_estado_sii_extraccion(self):
        xml = "<RESP><ESTADO>LOK</ESTADO></RESP>"
        assert _parsear_estado_sii(xml) == "LOK"

    def test_estado_sii_vacio(self):
        assert _parsear_estado_sii("") is None
        assert _parsear_estado_sii(None) is None

    def test_glosa_prioridad(self):
        xml = "<R><GLOSA_ERR>Error</GLOSA_ERR><GLOSA>OK</GLOSA></R>"
        assert _parsear_glosa_sii(xml) == "Error"

    def test_glosa_fallback(self):
        xml = "<R><GLOSA>Texto</GLOSA></R>"
        assert _parsear_glosa_sii(xml) == "Texto"

    def test_sha256(self):
        data = b"test data"
        assert _sha256_hex(data) == hashlib.sha256(data).hexdigest()


class TestDerivarPeriodo:
    def test_deriva_de_dte(self, session, empresa):
        _make_dtes(session, empresa, [(33, 100)])
        assert _derivar_periodo(session, empresa) == "2026-04"

    def test_fallback_sin_dtes(self, session, empresa):
        periodo = _derivar_periodo(session, empresa)
        # Sin DTEs, usa mes actual
        assert len(periodo) == 7  # YYYY-MM


# ══════════════════════════════════════════════════════════════════
# Tests: enriquecer_entradas_compras
# ══════════════════════════════════════════════════════════════════


class TestEnriquecerCompras:
    def test_entrada_basica(self, empresa):
        raw = [{
            "tipo_doc": 33,
            "folio": 100,
            "observaciones": "",
            "monto_exento": 0,
            "monto_afecto": 10000,
        }]
        result = enriquecer_entradas_compras(raw, empresa, "2026-04-09")
        assert len(result) == 1
        e = result[0]
        assert e["TpoDoc"] == 33
        assert e["NroDoc"] == 100
        assert e["RUTDoc"] == "77051056-2"
        assert e["MntNeto"] == 10000
        assert e["MntIVA"] == 1900
        assert e["MntTotal"] == 11900

    def test_entrega_gratuita(self, empresa):
        """CodIVANoRec=4 para entrega gratuita.

        MntIVA NO se emite (va en IVANoRec), pero MntTotal SÍ incluye el IVA
        no recuperable — el SII valida MntTotal = MntExe + MntNeto + IVA total
        de la operación. Reparar este cálculo evita el reparo ``LBR-2 -
        Reparo en Calculo de [MntTotal]`` (visto en cert 77829149-5 con
        T:33-F:67 el 2026-04-22).
        """
        raw = [{
            "tipo_doc": 33,
            "folio": 101,
            "observaciones": "ENTREGA GRATUITA DEL PROVEEDOR",
            "monto_exento": 0,
            "monto_afecto": 5000,
        }]
        result = enriquecer_entradas_compras(raw, empresa, "2026-04-09")
        e = result[0]
        assert e["IVANoRec"]["CodIVANoRec"] == 4
        assert e["IVANoRec"]["MntIVANoRec"] == 950
        assert "MntIVA" not in e
        assert e["MntTotal"] == 5950, (
            "MntTotal debe ser MntNeto + IVA aunque el IVA vaya en IVANoRec — "
            "el SII levanta LBR-2 si MntTotal no cuadra con los componentes."
        )

    def test_iva_retenido_total(self, empresa):
        """IVARetTotal + OtrosImp CodImp=15, MntTotal=MntNeto."""
        raw = [{
            "tipo_doc": 46,
            "folio": 200,
            "observaciones": "IVA RETENIDO TOTAL",
            "monto_exento": 0,
            "monto_afecto": 10000,
        }]
        result = enriquecer_entradas_compras(raw, empresa, "2026-04-09")
        e = result[0]
        assert e["IVARetTotal"] == 1900
        assert e["OtrosImp"]["CodImp"] == 15
        assert e["OtrosImp"]["MntImp"] == 1900
        assert e["MntTotal"] == 10000  # regla 25

    def test_factor_proporcionalidad(self, empresa):
        """IVAUsoComun para factor proporcionalidad.

        Igual que entrega gratuita: MntIVA no se emite (va en IVAUsoComun),
        pero MntTotal incluye el IVA — la factura real del proveedor es
        MntNeto + IVA. Ver bug T:30-F:781 en cert 77829149-5 (2026-04-22).
        """
        raw = [{
            "tipo_doc": 33,
            "folio": 300,
            "observaciones": "FACTOR PROPORCIONALIDAD",
            "monto_exento": 0,
            "monto_afecto": 8000,
        }]
        result = enriquecer_entradas_compras(raw, empresa, "2026-04-09")
        e = result[0]
        assert e["IVAUsoComun"] == 1520
        assert "MntIVA" not in e
        assert e["MntTotal"] == 9520, (
            "MntTotal debe incluir el IVA aunque se reporte en IVAUsoComun"
        )

    def test_iva_uso_comun_observacion_literal(self, empresa):
        """La observación 'IVA USO COMUN' (sin 'FACTOR PROPORCIONALIDAD')
        también debe disparar el branch de IVAUsoComun con MntTotal correcto.
        Caso real del set cert 77829149-5 entrada T:30-F:781."""
        raw = [{
            "tipo_doc": 30,
            "folio": 781,
            "observaciones": "FACTURA CON IVA USO COMUN",
            "monto_exento": 0,
            "monto_afecto": 29627,
        }]
        result = enriquecer_entradas_compras(raw, empresa, "2026-04-09")
        e = result[0]
        assert e["IVAUsoComun"] == 5629
        assert "MntIVA" not in e
        assert e["MntTotal"] == 35256  # 29627 + 5629

    def test_passthrough_ya_enriquecida(self, empresa):
        """Si la entrada ya tiene TpoDoc+RUTDoc, pasa directo."""
        raw = [{
            "TpoDoc": 33,
            "NroDoc": 100,
            "RUTDoc": "99999999-9",
            "RznSoc": "Otro",
            "FchDoc": "2026-04-09",
            "MntNeto": 5000,
            "MntIVA": 950,
            "MntTotal": 5950,
        }]
        result = enriquecer_entradas_compras(raw, empresa, "2026-04-09")
        assert result[0]["RUTDoc"] == "99999999-9"  # no sobrescribe

    def test_exento_puro(self, empresa):
        """Entrada solo con monto exento, sin afecto."""
        raw = [{
            "tipo_doc": 34,
            "folio": 50,
            "observaciones": "",
            "monto_exento": 7000,
            "monto_afecto": 0,
        }]
        result = enriquecer_entradas_compras(raw, empresa, "2026-04-09")
        e = result[0]
        assert e["MntExe"] == 7000
        assert "MntNeto" not in e
        assert "MntIVA" not in e
        assert e["MntTotal"] == 7000


# ══════════════════════════════════════════════════════════════════
# Tests: generar_libro
# ══════════════════════════════════════════════════════════════════


class TestGenerarLibro:
    def test_generar_ventas(self, session, run, empresa, servicio_fake):
        _make_dtes(session, empresa, [(33, 100), (33, 101), (61, 50)])
        libro = _make_libro(session, run, "ventas", numero_atencion=4761386)

        resultado = generar_libro(session, run, libro.id, servicio_fake, empresa)

        assert resultado["tipo_libro"] == "ventas"
        assert resultado["sha256"]
        assert resultado["tamano_bytes"] > 0
        # Firma llamada con type="libro"
        servicio_fake._firma.firmar.assert_called_once()
        call_args = servicio_fake._firma.firmar.call_args
        assert call_args.kwargs.get("type") == "libro" or call_args[0][2] == "libro"
        # XML persisted en DB
        session.refresh(libro)
        assert libro.xml_libro is not None
        assert libro.estado == "generando"

    def test_generar_guias(self, session, run, empresa, servicio_fake):
        _make_dtes(session, empresa, [(52, 200), (52, 201)])
        libro = _make_libro(session, run, "guias", numero_atencion=4761389)

        resultado = generar_libro(session, run, libro.id, servicio_fake, empresa)

        assert resultado["tipo_libro"] == "guias"
        session.refresh(libro)
        assert libro.xml_libro is not None

    def test_generar_compras(self, session, run, empresa, servicio_fake):
        # Necesita al menos un DTE para derivar periodo/fecha
        _make_dtes(session, empresa, [(33, 100)])
        entradas = [
            {"tipo_doc": 33, "folio": 100, "observaciones": "",
             "monto_exento": 0, "monto_afecto": 10000},
        ]
        libro = _make_libro(session, run, "compras",
                           datos={"entradas": entradas},
                           numero_atencion=4761387)

        resultado = generar_libro(session, run, libro.id, servicio_fake, empresa)

        assert resultado["tipo_libro"] == "compras"
        session.refresh(libro)
        assert libro.xml_libro is not None

    def test_generar_sin_dtes_falla(self, session, run, empresa, servicio_fake):
        libro = _make_libro(session, run, "ventas")

        with pytest.raises(ValueError, match="No hay DTEs"):
            generar_libro(session, run, libro.id, servicio_fake, empresa)

    def test_generar_compras_sin_entradas_falla(self, session, run, empresa, servicio_fake):
        libro = _make_libro(session, run, "compras", datos={})

        with pytest.raises(ValueError, match="sin entradas"):
            generar_libro(session, run, libro.id, servicio_fake, empresa)

    def test_generar_libro_no_encontrado(self, session, run, empresa, servicio_fake):
        with pytest.raises(ValueError, match="no encontrado"):
            generar_libro(session, run, "inexistente", servicio_fake, empresa)

    def test_generar_libro_run_mismatch(self, session, run, empresa, servicio_fake):
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas")
        run_otra = CertificacionRun(
            id="run-2", rut_empresa=empresa.rut, estado="emitiendo",
        )
        session.add(run_otra)
        session.commit()

        with pytest.raises(ValueError, match="no pertenece"):
            generar_libro(session, run_otra, libro.id, servicio_fake, empresa)

    def test_generar_idempotente(self, session, run, empresa, servicio_fake):
        """Llamar generar dos veces regenera el XML."""
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=123)

        r1 = generar_libro(session, run, libro.id, servicio_fake, empresa)
        r2 = generar_libro(session, run, libro.id, servicio_fake, empresa)

        assert r1["sha256"] == r2["sha256"]
        assert servicio_fake._firma.firmar.call_count == 2


# ══════════════════════════════════════════════════════════════════
# Tests: enviar_libro
# ══════════════════════════════════════════════════════════════════


class TestEnviarLibro:
    def test_envio_ok(self, session, run, empresa, servicio_fake, monkeypatch):
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=4761386)

        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {"track_id": "TRACK-123", "status": "OK", "raw": ""},
        )

        resultado = enviar_libro(session, run, libro.id, servicio_fake, empresa)

        assert resultado["ok"] is True
        assert resultado["trackid"] == "TRACK-123"
        session.refresh(libro)
        assert libro.trackid == "TRACK-123"
        assert libro.estado == "enviado"
        assert libro.estado_sii == "enviado"
        assert libro.enviado_at is not None
        assert libro.error_mensaje is None

    def test_envio_rechazo(self, session, run, empresa, servicio_fake, monkeypatch):
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=123)

        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {"track_id": None, "status": "RCH", "glosa": "Schema error", "raw": ""},
        )

        resultado = enviar_libro(session, run, libro.id, servicio_fake, empresa)

        assert resultado["ok"] is False
        assert resultado["trackid"] is None
        session.refresh(libro)
        assert libro.trackid is None
        assert libro.error_mensaje is not None
        assert "Schema error" in libro.error_mensaje

    def test_envio_rechazado_resetea_estado_a_pendiente(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        """Bug 77829149-5 2026-04-23: libro guías quedó con ``estado='generando'``
        tras rechazo del SII → UI pegada en "GENERANDO", usuario no puede
        re-intentar sin reiniciar. El flujo correcto: rechazo debe resetear
        ``estado='pendiente'`` (preservando ``error_mensaje``) para que la
        UI muestre el error y el usuario pueda apretar "Enviar al SII" de
        nuevo sin tener que reiniciar manualmente."""
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=123)

        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {
                "track_id": None, "status": "ERROR", "glosa": "",
                "raw": "<html>respuesta rara sin GLOSA</html>",
            },
        )

        enviar_libro(session, run, libro.id, servicio_fake, empresa)

        session.refresh(libro)
        assert libro.estado == "pendiente", (
            f"Tras rechazo del SII, estado debe volver a 'pendiente' "
            f"para desbloquear la UI. Actual: {libro.estado!r}"
        )
        assert libro.error_mensaje is not None
        assert libro.trackid is None

    def test_envio_rechazado_loggea_raw_para_diagnostico(
        self, session, run, empresa, servicio_fake, monkeypatch, caplog,
    ):
        """Cuando el SII rechaza sin glosa parseable (ej. HTML 500 o
        respuesta no estándar), el raw completo debe quedar en el log
        del server — sin eso no podemos diagnosticar rechazos como el
        'Rechazo SII libro guias: ERROR' que dejó el parser vacío."""
        import logging

        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=123)

        raw_sii = "<html><body>Error interno SII</body></html>"
        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {
                "track_id": None, "status": "ERROR", "glosa": "",
                "raw": raw_sii,
            },
        )

        with caplog.at_level(logging.WARNING, logger="crumbpos.api.services.envio_libro_cert"):
            enviar_libro(session, run, libro.id, servicio_fake, empresa)

        assert any(
            raw_sii in record.getMessage() or "Error interno SII" in record.getMessage()
            for record in caplog.records
        ), (
            "El raw response del SII debe loggearse en WARNING para que "
            "podamos diagnosticar rechazos sin glosa parseable."
        )

    def test_envio_reutiliza_xml(self, session, run, empresa, servicio_fake, monkeypatch):
        """Si el libro ya tiene XML generado, no lo regenera."""
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=123)

        # Generar primero
        generar_libro(session, run, libro.id, servicio_fake, empresa)
        call_count_after_gen = servicio_fake._firma.firmar.call_count

        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {"track_id": "TRACK-456", "status": "OK", "raw": ""},
        )

        enviar_libro(session, run, libro.id, servicio_fake, empresa)

        # firmar no se llamó de nuevo (reutilizó XML existente)
        assert servicio_fake._firma.firmar.call_count == call_count_after_gen

    def test_envio_genera_si_falta_xml(self, session, run, empresa, servicio_fake, monkeypatch):
        """Si no hay XML generado, lo genera antes de enviar."""
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=123)

        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {"track_id": "TRACK-789", "status": "OK", "raw": ""},
        )

        resultado = enviar_libro(session, run, libro.id, servicio_fake, empresa)

        assert resultado["ok"] is True
        session.refresh(libro)
        assert libro.xml_libro is not None


# ══════════════════════════════════════════════════════════════════
# Tests: consultar_estado_libro
# ══════════════════════════════════════════════════════════════════


class TestConsultarEstadoLibro:
    def _libro_con_trackid(self, session, run):
        lib = CertificacionLibro(
            run_id=run.id,
            tipo_libro="ventas",
            estado="enviado",
            trackid="TRACK-100",
            estado_sii="enviado",
        )
        session.add(lib)
        session.commit()
        session.refresh(lib)
        return lib

    def test_consulta_lok(self, session, run, empresa, servicio_fake, monkeypatch):
        libro = self._libro_con_trackid(session, run)

        monkeypatch.setattr(
            envio_libro_cert, "consultar_estado_envio",
            lambda **kw: {"raw": "<RESP><ESTADO>LOK</ESTADO><GLOSA>Aceptado Cuadrado</GLOSA></RESP>"},
        )

        resultado = consultar_estado_libro(session, run, libro.id, servicio_fake, empresa)

        assert resultado["estado_sii"] == "LOK"
        assert resultado["glosa"] == "Aceptado Cuadrado"
        session.refresh(libro)
        assert libro.estado_sii == "LOK"
        assert libro.error_mensaje is None

    def test_consulta_rechazo(self, session, run, empresa, servicio_fake, monkeypatch):
        libro = self._libro_con_trackid(session, run)

        monkeypatch.setattr(
            envio_libro_cert, "consultar_estado_envio",
            lambda **kw: {"raw": "<RESP><ESTADO>SRH</ESTADO><GLOSA_ERR>No Informa IVA</GLOSA_ERR></RESP>"},
        )

        resultado = consultar_estado_libro(session, run, libro.id, servicio_fake, empresa)

        assert resultado["estado_sii"] == "SRH"
        session.refresh(libro)
        assert libro.estado_sii == "SRH"
        assert "No Informa IVA" in libro.error_mensaje

    def test_consulta_limpia_error_previo(self, session, run, empresa, servicio_fake, monkeypatch):
        libro = self._libro_con_trackid(session, run)
        libro.error_mensaje = "error previo"
        session.commit()

        monkeypatch.setattr(
            envio_libro_cert, "consultar_estado_envio",
            lambda **kw: {"raw": "<RESP><ESTADO>EPR</ESTADO></RESP>"},
        )

        consultar_estado_libro(session, run, libro.id, servicio_fake, empresa)

        session.refresh(libro)
        assert libro.error_mensaje is None

    def test_consulta_sin_trackid_falla(self, session, run, empresa, servicio_fake):
        libro = _make_libro(session, run, "ventas")

        with pytest.raises(ValueError, match="no tiene trackid"):
            consultar_estado_libro(session, run, libro.id, servicio_fake, empresa)

    def test_consulta_libro_no_encontrado(self, session, run, empresa, servicio_fake):
        with pytest.raises(ValueError, match="no encontrado"):
            consultar_estado_libro(session, run, "xxx", servicio_fake, empresa)

    def test_consulta_lnc_es_rechazo(self, session, run, empresa, servicio_fake, monkeypatch):
        """LNC (Libro No Cargado) se trata como rechazo."""
        libro = self._libro_con_trackid(session, run)

        monkeypatch.setattr(
            envio_libro_cert, "consultar_estado_envio",
            lambda **kw: {"raw": "<RESP><ESTADO>LNC</ESTADO><GLOSA_ERR>Libro ya cerrado</GLOSA_ERR></RESP>"},
        )

        consultar_estado_libro(session, run, libro.id, servicio_fake, empresa)

        session.refresh(libro)
        assert libro.estado_sii == "LNC"
        assert "ya cerrado" in libro.error_mensaje


# ══════════════════════════════════════════════════════════════════
# Tests: reiniciar_envio_libro
# ══════════════════════════════════════════════════════════════════


class TestReiniciarEnvioLibro:
    """Reiniciar libro ya enviado para poder re-generarlo con otro
    ``numero_atencion`` o tras un fix del core (ver bug cert 77829149-5
    2026-04-22: libro compras enviado sin N° Atención + reparos de
    MntTotal). Es el equivalente para libros del botón
    ``descartar-folio`` para DTEs.

    Reglas:
      - Solo reinicia libros con trackid (si nunca se envió, no hay
        nada que reiniciar).
      - Bloquea si ``avance_declarado_at`` o ``aprobado_at`` ya están
        seteados — rebobinar eso sería fraude.
      - Resetea trackid, estado_sii, error_mensaje, estado='pendiente',
        enviado_at=None. Mantiene ``xml_libro`` y ``datos`` intactos
        (el próximo ``generar_libro`` los sobreescribirá).
    """

    def _libro_enviado(self, session, run, **kwargs):
        lib = _make_libro(session, run, "compras", datos={"entradas": []})
        lib.trackid = "247769358"
        lib.estado = "enviado"
        lib.estado_sii = "LOK"
        lib.error_mensaje = "LBR - 2 - Reparo en Calculo de [MntTotal]"
        lib.enviado_at = datetime.now(timezone.utc)
        for k, v in kwargs.items():
            setattr(lib, k, v)
        session.commit()
        return lib

    def test_reinicia_libro_enviado(self, session, run):
        libro = self._libro_enviado(session, run)

        resultado = reiniciar_envio_libro(session, run, libro.id)

        session.refresh(libro)
        assert libro.trackid is None
        assert libro.estado_sii is None
        assert libro.error_mensaje is None
        assert libro.estado == "pendiente"
        assert libro.enviado_at is None
        assert resultado["ok"] is True
        assert resultado["libro_id"] == libro.id
        assert resultado["estado"] == "pendiente"

    def test_preserva_xml_libro_y_datos(self, session, run):
        """El XML previo y las entradas parseadas NO se borran — el
        próximo ``generar_libro`` los sobreescribe idempotentemente."""
        libro = self._libro_enviado(session, run)
        libro.xml_libro = "<xml>previo</xml>"
        session.commit()

        reiniciar_envio_libro(session, run, libro.id)

        session.refresh(libro)
        assert libro.xml_libro == "<xml>previo</xml>"
        assert libro.datos == {"entradas": []}

    def test_preserva_numero_atencion(self, session, run):
        """Si el usuario ya completó N° Atención, no se pierde al
        reiniciar — corregir montos no debería hacer retipear el número."""
        libro = self._libro_enviado(session, run, numero_atencion=4788488)

        reiniciar_envio_libro(session, run, libro.id)

        session.refresh(libro)
        assert libro.numero_atencion == 4788488

    def test_bloquea_si_avance_declarado(self, session, run):
        libro = self._libro_enviado(session, run)
        libro.avance_declarado_at = datetime.now(timezone.utc)
        session.commit()

        with pytest.raises(ValueError, match="avance"):
            reiniciar_envio_libro(session, run, libro.id)

        # No mutó nada
        session.refresh(libro)
        assert libro.trackid == "247769358"

    def test_bloquea_si_aprobado(self, session, run):
        libro = self._libro_enviado(session, run)
        libro.aprobado_at = datetime.now(timezone.utc)
        session.commit()

        with pytest.raises(ValueError, match="aprobado"):
            reiniciar_envio_libro(session, run, libro.id)

    def test_noop_si_libro_nunca_enviado(self, session, run):
        """Si el libro no tiene trackid, reiniciar es no-op idempotente.
        No falla — el usuario puede hacer click sin consecuencias."""
        libro = _make_libro(session, run, "ventas")

        resultado = reiniciar_envio_libro(session, run, libro.id)

        session.refresh(libro)
        assert resultado["ok"] is True
        assert libro.estado == "pendiente"

    def test_error_si_libro_no_existe(self, session, run):
        with pytest.raises(ValueError, match="no encontrado"):
            reiniciar_envio_libro(session, run, "inexistente-id")

    def test_error_si_libro_no_pertenece_a_run(self, session, run, empresa):
        libro = self._libro_enviado(session, run)
        # Crear otra run para testear cross-run
        otra_run = CertificacionRun(
            id="run-otra", rut_empresa=empresa.rut, estado="emitiendo",
            screen_actual=3,
        )
        session.add(otra_run)
        session.commit()

        with pytest.raises(ValueError, match="no pertenece"):
            reiniciar_envio_libro(session, otra_run, libro.id)


# ══════════════════════════════════════════════════════════════════
# Tests: filtro por SET para libro de ventas
# (Regla SII: "Si obtuvo ambos sets, utilice los documentos del SET
#  BASICO para construir el libro de ventas".)
# ══════════════════════════════════════════════════════════════════


def _make_caso(
    session,
    run,
    set_nombre: str,
    numero_atencion: int,
    numero_caso: str,
    tipo_dte: int,
    folio: int,
    dte_emitido_id: str,
):
    """Helper para crear un CertificacionCaso vinculado a un DteEmitido.

    Bajo la regla del usuario (cert 77829149-5, 2026-04-23) los libros
    solo hidratan con casos que pasaron la **declaración de avance**.
    El helper crea casos con el flujo completo:
    - ``estado="aprobado"``
    - ``avance_declarado_at`` seteado
    - ``aprobado_at`` seteado

    Tests que quieran simular un caso en un estado intermedio (emitido
    sin avance, avance declarado sin aprobar) deben construir el
    CertificacionCaso directamente, no usar este helper.
    """
    now = datetime.now(timezone.utc)
    c = CertificacionCaso(
        run_id=run.id,
        set_nombre=set_nombre,
        numero_caso=numero_caso,
        numero_atencion=numero_atencion,
        tipo_dte=tipo_dte,
        folio=folio,
        dte_emitido_id=dte_emitido_id,
        estado="aprobado",
        estado_sii="EPR",
        emitido_at=now,
        avance_declarado_at=now,
        aprobado_at=now,
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _dte_id_by_folio(session, empresa, tipo_dte: int, folio: int) -> str:
    dte = session.query(DteEmitido).filter_by(
        empresa_id=empresa.id, tipo_dte=tipo_dte, folio=folio,
    ).first()
    assert dte is not None, f"DTE {tipo_dte}-{folio} no encontrado"
    return dte.id


class TestFiltroSetLibroVentas:
    """Libro de ventas debe filtrar por casos del SET BASICO (o EXENTA
    si no hay BASICO). Si no hay casos ⇒ modo producción: todos los
    DTEs del tipo venta."""

    def test_filtra_solo_basico_cuando_hay_ambos_sets(
        self, session, run, empresa, servicio_fake,
    ):
        # Emitir DTEs del SET BASICO (T33+T56+T61) y del SET EXENTA (T34+T61)
        _make_dtes(session, empresa, [
            (33, 61), (33, 62),  # BASICO
            (56, 20),             # BASICO
            (61, 34),             # BASICO
            (34, 69), (34, 70),  # EXENTA — NO deben ir al libro
            (61, 40),             # EXENTA — NO debe ir
        ])
        # Vincular casos
        _make_caso(session, run, "BASICO", 4788482, "4788482-1", 33, 61,
                   _dte_id_by_folio(session, empresa, 33, 61))
        _make_caso(session, run, "BASICO", 4788482, "4788482-2", 33, 62,
                   _dte_id_by_folio(session, empresa, 33, 62))
        _make_caso(session, run, "BASICO", 4788482, "4788482-3", 56, 20,
                   _dte_id_by_folio(session, empresa, 56, 20))
        _make_caso(session, run, "BASICO", 4788482, "4788482-4", 61, 34,
                   _dte_id_by_folio(session, empresa, 61, 34))
        _make_caso(session, run, "EXENTA", 4788488, "4788488-1", 34, 69,
                   _dte_id_by_folio(session, empresa, 34, 69))
        _make_caso(session, run, "EXENTA", 4788488, "4788488-2", 34, 70,
                   _dte_id_by_folio(session, empresa, 34, 70))
        _make_caso(session, run, "EXENTA", 4788488, "4788488-3", 61, 40,
                   _dte_id_by_folio(session, empresa, 61, 40))

        libro = _make_libro(session, run, "ventas", numero_atencion=4788484)
        generar_libro(session, run, libro.id, servicio_fake, empresa)
        session.refresh(libro)

        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")

        # BASICO: 2 T33 + 1 T56 + 1 T61 = 4 entries
        assert xml.count("<Detalle>") == 4, (
            "Libro ventas debe incluir SOLO los 4 DTEs del SET BASICO, "
            "no los 3 del SET EXENTA"
        )
        # No T34 (exentas)
        assert "<TpoDoc>34</TpoDoc>" not in xml, \
            "Libro ventas NO debe incluir T34 del SET EXENTA"
        # No folio 69, 70 (T34 exentas), ni 40 (T61 de EXENTA)
        assert "<NroDoc>69</NroDoc>" not in xml
        assert "<NroDoc>70</NroDoc>" not in xml
        assert "<NroDoc>40</NroDoc>" not in xml
        # Sí los de BASICO
        assert "<NroDoc>61</NroDoc>" in xml  # T33 folio 61
        assert "<NroDoc>34</NroDoc>" in xml  # T61 folio 34
        # ResumenPeriodo: 3 TotalesPeriodo (T33, T56, T61) — NO T34
        assert xml.count("<TotalesPeriodo>") == 3

    def test_usa_exenta_si_no_hay_basico(
        self, session, run, empresa, servicio_fake,
    ):
        """Si solo hay SET EXENTA (sin BASICO) ⇒ usar EXENTA."""
        _make_dtes(session, empresa, [
            (34, 69), (34, 70), (34, 71),  # EXENTA
            (61, 40), (61, 41),             # EXENTA
            (56, 23),                         # EXENTA
        ])
        for i, (t, f) in enumerate([(34, 69), (34, 70), (34, 71),
                                    (61, 40), (61, 41), (56, 23)], start=1):
            _make_caso(session, run, "EXENTA", 4788488, f"4788488-{i}", t, f,
                       _dte_id_by_folio(session, empresa, t, f))

        libro = _make_libro(session, run, "ventas", numero_atencion=4788484)
        generar_libro(session, run, libro.id, servicio_fake, empresa)
        session.refresh(libro)

        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")
        assert xml.count("<Detalle>") == 6
        assert "<TpoDoc>34</TpoDoc>" in xml  # exentas sí, porque no hay BASICO

    def test_fallback_sin_casos_usa_todos_los_dtes(
        self, session, run, empresa, servicio_fake,
    ):
        """Sin casos (modo producción) ⇒ query por tipo_dte como siempre."""
        _make_dtes(session, empresa, [(33, 100), (33, 101), (61, 50)])
        libro = _make_libro(session, run, "ventas", numero_atencion=123)

        generar_libro(session, run, libro.id, servicio_fake, empresa)
        session.refresh(libro)

        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")
        assert xml.count("<Detalle>") == 3  # comportamiento actual preservado


class TestFiltroLibroGuiasPorRunAprobado:
    """Libro de guías solo hidrata con casos tipo_dte=52 del run actual
    que estén aprobados (estado='aprobado' + timestamps de avance/aprobado).

    Regresión real (cert 77829149-5, 2026-04-23): al regenerar el libro
    de guías, el servicio tomaba TODOS los DteEmitido con tipo_dte=52 y
    empresa_id sin filtrar por run → mezclaba 3 folios huérfanos del set
    anterior con 3 del set actual. El SII aceptó schema pero devolvió SRH
    por reparo 'El Numero de Guias Venta/Traslado No Cuadra'.

    Regla del usuario: los libros se hidratan SOLO con documentos
    aprobados del run actual que pasaron la declaración de avance.
    """

    def test_dtes_huerfanos_no_entran_al_libro(
        self, session, run, empresa, servicio_fake,
    ):
        """DTEs tipo 52 sin caso en el run actual NO deben entrar al libro."""
        # 6 guías en la BD: 3 "huérfanas" (folios 81-83, set anterior que
        # quedó en la BD) + 3 del set actual (folios 84-86).
        _make_dtes(session, empresa, [
            (52, 81), (52, 82), (52, 83),  # Huérfanas — NO deben entrar
            (52, 84), (52, 85), (52, 86),  # Set actual — SÍ deben entrar
        ])
        # Solo vinculamos casos aprobados para folios 84-86 (set actual).
        # Folios 81-83 NO tienen CertificacionCaso en este run.
        _make_caso(session, run, "GUIAS", 4791437, "4791437-1", 52, 84,
                   _dte_id_by_folio(session, empresa, 52, 84))
        _make_caso(session, run, "GUIAS", 4791437, "4791437-2", 52, 85,
                   _dte_id_by_folio(session, empresa, 52, 85))
        _make_caso(session, run, "GUIAS", 4791437, "4791437-3", 52, 86,
                   _dte_id_by_folio(session, empresa, 52, 86))

        libro = _make_libro(session, run, "guias", numero_atencion=4791438)
        generar_libro(session, run, libro.id, servicio_fake, empresa)
        session.refresh(libro)

        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")

        # Libro debe incluir SOLO los 3 folios del set actual.
        # LibroGuia usa <Folio>N</Folio>, no <NroDoc> (ese es LibroCV).
        assert xml.count("<Detalle>") == 3, (
            "Libro de guías debe contener SOLO los 3 folios aprobados "
            "del run (84-86), no los 3 huérfanos del set anterior (81-83)"
        )
        # Folios 81-83 NO deben aparecer en el XML.
        for folio_huerfano in (81, 82, 83):
            assert f"<Folio>{folio_huerfano}</Folio>" not in xml, (
                f"Folio huérfano {folio_huerfano} no debe entrar al libro "
                "de guías — no está vinculado a un caso aprobado del run"
            )
        # Folios 84-86 SÍ deben aparecer.
        for folio_vigente in (84, 85, 86):
            assert f"<Folio>{folio_vigente}</Folio>" in xml, (
                f"Folio vigente {folio_vigente} debe entrar al libro — "
                "pertenece a un caso aprobado del run actual"
            )
        # ResumenPeriodo debe reflejar 3 guías de venta (no 6).
        assert "<TotGuiaVenta>3</TotGuiaVenta>" in xml, (
            "Resumen debe contar 3 guías de venta, no 6 (evitar el "
            "reparo SRH 'El Numero de Guias Venta No Cuadra')"
        )

    def test_caso_no_aprobado_no_entra_al_libro(
        self, session, run, empresa, servicio_fake,
    ):
        """Casos en estado 'emitido' (aún no aprobados) NO entran al libro."""
        _make_dtes(session, empresa, [(52, 100), (52, 101)])

        # Solo 1 caso aprobado (folio 100). El otro (folio 101) está en
        # estado 'emitido' — operador aún no declaró avance.
        _make_caso(session, run, "GUIAS", 4791437, "4791437-1", 52, 100,
                   _dte_id_by_folio(session, empresa, 52, 100))
        # Caso en estado 'emitido' — no pasó declaración de avance.
        caso_emitido = CertificacionCaso(
            run_id=run.id,
            set_nombre="GUIAS",
            numero_caso="4791437-2",
            numero_atencion=4791437,
            tipo_dte=52,
            folio=101,
            dte_emitido_id=_dte_id_by_folio(session, empresa, 52, 101),
            estado="emitido",  # ← no aprobado
            estado_sii="EPR",
            emitido_at=datetime.now(timezone.utc),
            # avance_declarado_at y aprobado_at quedan en None
        )
        session.add(caso_emitido)
        session.commit()

        libro = _make_libro(session, run, "guias", numero_atencion=4791438)
        generar_libro(session, run, libro.id, servicio_fake, empresa)
        session.refresh(libro)

        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")

        assert xml.count("<Detalle>") == 1, (
            "Solo debe entrar el caso aprobado (folio 100), no el "
            "emitido-pendiente-aprobación (folio 101)"
        )
        assert "<Folio>100</Folio>" in xml
        assert "<Folio>101</Folio>" not in xml, (
            "Caso emitido sin aprobación NO debe entrar al libro — regla "
            "del usuario: libros se hidratan solo con docs que pasaron "
            "la declaración de avance"
        )


class TestInstruccionesLibroGuias:
    """Libro de guías debe respetar instrucciones del set de pruebas SII
    tipo 'EL CASO N CORRESPONDE A UNA GUIA ANULADA'."""

    def _setup_guias_con_instruccion(
        self, session, run, empresa, instrucciones: str,
    ):
        """Crea 3 guías (folios 81, 82, 83) + casos del set GUIAS."""
        # Override items para que cada guía tenga monto distinto
        for folio, neto, iva, total in [
            (81, 0, 0, 0),          # traslado interno
            (82, 1854741, 352401, 2207142),  # venta
            (83, 1398052, 265630, 1663682),  # venta (será anulada por instr.)
        ]:
            session.add(DteEmitido(
                empresa_id=empresa.id,
                tipo_dte=52,
                folio=folio,
                fecha_emision=date(2026, 4, 22),
                receptor_rut="77829149-5",
                receptor_razon="GRUPO TRESTRES SPA",
                monto_neto=neto,
                monto_exento=0,
                iva=iva,
                monto_total=total,
                xml_firmado=base64.b64encode(
                    (f"<DTE><IndTraslado>"
                     f"{5 if folio == 81 else 1}"
                     f"</IndTraslado></DTE>").encode()
                ).decode(),
                estado_sii="pendiente",
            ))
        session.commit()
        for i, folio in enumerate([81, 82, 83], start=1):
            _make_caso(session, run, "GUIAS", 4788486, f"4788486-{i}",
                       52, folio,
                       _dte_id_by_folio(session, empresa, 52, folio))
        # Set datos_parseados con las instrucciones
        run.datos_parseados = {"libro_guias_instrucciones": instrucciones}
        session.commit()
        session.refresh(run)

    def test_caso_anulado_va_con_anulado_2(
        self, session, run, empresa, servicio_fake,
    ):
        """Instrucción 'EL CASO 3 CORRESPONDE A UNA GUIA ANULADA' ⇒
        folio 83 debe llevar <Anulado>2</Anulado> y contar en
        TotGuiaAnulada, NO en TotGuiaVenta."""
        self._setup_guias_con_instruccion(
            session, run, empresa,
            "CONSTRUYA EL LIBRO CON LAS GUIAS...\n"
            "- EL CASO 2 CORRESPONDE A UNA GUIA QUE SE FACTURO EN EL PERIODO\n"
            "- EL CASO 3 CORRESPONDE A UNA GUIA ANULADA"
        )
        libro = _make_libro(session, run, "guias", numero_atencion=4788487)
        generar_libro(session, run, libro.id, servicio_fake, empresa)
        session.refresh(libro)

        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")

        # Folio 83 va como anulada
        assert "<Folio>83</Folio>\n<Anulado>2</Anulado>" in xml, \
            "Folio 83 debe tener <Anulado>2</Anulado> según instrucción SII"
        # Folio 82 sigue siendo venta (sin Anulado)
        assert "<Folio>82</Folio>\n<TpoOper>1</TpoOper>" in xml
        # Totales: solo folio 82 cuenta como venta
        assert "<TotGuiaVenta>1</TotGuiaVenta>" in xml
        assert "<TotGuiaAnulada>1</TotGuiaAnulada>" in xml
        assert "<TotMntGuiaVta>2207142</TotMntGuiaVta>" in xml  # solo 82

    def test_sin_instrucciones_no_marca_anulada(
        self, session, run, empresa, servicio_fake,
    ):
        """Sin instrucciones ⇒ todas las guías son activas."""
        self._setup_guias_con_instruccion(session, run, empresa, "")
        libro = _make_libro(session, run, "guias", numero_atencion=4788487)
        generar_libro(session, run, libro.id, servicio_fake, empresa)
        session.refresh(libro)

        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")
        assert "<Anulado>" not in xml  # ninguna anulada
        assert "<TotGuiaVenta>2</TotGuiaVenta>" in xml  # 82 + 83
        assert "<TotGuiaAnulada>" not in xml


# ══════════════════════════════════════════════════════════════════
# Tests: libros ESPECIALES de certificación SIEMPRE usan TipoEnvio=TOTAL
#
# Contexto: el 2026-04-23 intentamos auto-emitir ``TipoEnvio=AJUSTE``
# en re-envíos, pensando que resolvería el rechazo LNC. El SII rechazó
# el AJUSTE **antes de generar trackid** — confirmando que para libros
# ESPECIALES de certificación:
#   - ``LibroGuia`` solo acepta TOTAL/PARCIAL (ver sii_formato_libros.md).
#   - IECV (ventas/compras) tolera AJUSTE en el schema pero requiere un
#     N° de Atención nuevo; reusar el del TOTAL previo dispara LNC.
# La única ruta legítima cuando el SII ya cerró el FolioNotificacion:
# pedir un set nuevo al SII y usar el feature "reiniciar certificación
# preservando CAFs" (``crumbpos/certificacion/reiniciar.py``).
#
# El campo ``primer_envio_sii_at`` se mantiene como audit trail (marca
# cuándo el SII registró el primer TOTAL) pero el service NO lo usa
# para decidir ``tipo_envio`` — siempre pasa el default ``TOTAL``.
# El parámetro ``tipo_envio`` de los generadores queda disponible para
# producción (libros MENSUAL, donde AJUSTE sí es válido).
# ══════════════════════════════════════════════════════════════════


class TestPrimerEnvioSiiAtAuditTrail:
    """``primer_envio_sii_at`` como audit trail del primer TOTAL que
    aceptó el SII. NO dispara AJUSTE automático — solo registra el
    timestamp para poder inspeccionarlo si hace falta."""

    def test_primer_envio_setea_primer_envio_sii_at(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        """La primera vez que el SII devuelve trackid, se persiste la
        fecha en ``primer_envio_sii_at``."""
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=4788484)
        assert libro.primer_envio_sii_at is None

        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {"track_id": "TRACK-FIRST", "status": "OK", "raw": ""},
        )

        before = datetime.now(timezone.utc).replace(tzinfo=None)
        enviar_libro(session, run, libro.id, servicio_fake, empresa)
        after = datetime.now(timezone.utc).replace(tzinfo=None)

        session.refresh(libro)
        assert libro.primer_envio_sii_at is not None
        assert before <= libro.primer_envio_sii_at <= after

    def test_primer_envio_rechazado_no_setea_primer_envio_sii_at(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        """Si el SII rechaza (sin trackid), NO se marca el campo."""
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=4788484)

        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {
                "track_id": None, "status": "RCH",
                "glosa": "Schema error", "raw": "",
            },
        )

        enviar_libro(session, run, libro.id, servicio_fake, empresa)

        session.refresh(libro)
        assert libro.primer_envio_sii_at is None

    def test_segundo_envio_no_pisa_primer_envio_sii_at(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        """El campo solo se escribe la PRIMERA vez — el timestamp
        original es inmutable aun después de reiniciar + re-enviar."""
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=4788484)

        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {"track_id": "T1", "status": "OK", "raw": ""},
        )
        enviar_libro(session, run, libro.id, servicio_fake, empresa)
        session.refresh(libro)
        primer_ts = libro.primer_envio_sii_at
        assert primer_ts is not None

        reiniciar_envio_libro(session, run, libro.id)
        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {"track_id": "T2", "status": "OK", "raw": ""},
        )
        enviar_libro(session, run, libro.id, servicio_fake, empresa)
        session.refresh(libro)

        assert libro.primer_envio_sii_at == primer_ts

    def test_reiniciar_no_limpia_primer_envio_sii_at(
        self, session, run, empresa, servicio_fake, monkeypatch,
    ):
        """``reiniciar_envio_libro`` preserva ``primer_envio_sii_at``
        como audit trail."""
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=4788484)

        monkeypatch.setattr(
            envio_libro_cert, "enviar_dte",
            lambda **kw: {"track_id": "T1", "status": "OK", "raw": ""},
        )
        enviar_libro(session, run, libro.id, servicio_fake, empresa)
        session.refresh(libro)
        ts_primer_envio = libro.primer_envio_sii_at

        reiniciar_envio_libro(session, run, libro.id)

        session.refresh(libro)
        assert libro.primer_envio_sii_at == ts_primer_envio
        assert libro.trackid is None
        assert libro.estado == "pendiente"


class TestTipoEnvioLibros:
    """Verifica la lógica de TipoEnvio por tipo de libro:

    IECV (ventas / compras):
      - Primer envío (``primer_envio_sii_at`` es None)  → TOTAL
      - Re-envío correctivo (``primer_envio_sii_at`` seteado) → AJUSTE
        El SII devuelve LNC si se envía TOTAL cuando ya existe un libro
        aceptado para ese FolioNotificacion/período.

    LibroGuia:
      - Siempre TOTAL (el esquema LibroGuia_v10.xsd solo acepta
        TOTAL/PARCIAL; AJUSTE no es un valor válido en ese esquema)."""

    def test_ventas_primera_vez_usa_total(
        self, session, run, empresa, servicio_fake,
    ):
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=4788484)

        generar_libro(session, run, libro.id, servicio_fake, empresa)

        session.refresh(libro)
        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")
        assert "<TipoEnvio>TOTAL</TipoEnvio>" in xml
        assert "<TipoEnvio>AJUSTE</TipoEnvio>" not in xml

    def test_ventas_reenvio_usa_ajuste(
        self, session, run, empresa, servicio_fake,
    ):
        """Cuando ``primer_envio_sii_at`` está seteado (ya hubo un TOTAL
        aceptado), el re-envío correctivo debe generar AJUSTE.
        Enviar TOTAL en este caso provoca rechazo LNC del SII."""
        _make_dtes(session, empresa, [(33, 100)])
        libro = _make_libro(session, run, "ventas", numero_atencion=4788484)
        libro.primer_envio_sii_at = datetime.now(timezone.utc)
        session.commit()

        generar_libro(session, run, libro.id, servicio_fake, empresa)

        session.refresh(libro)
        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")
        assert "<TipoEnvio>AJUSTE</TipoEnvio>" in xml
        assert "<TipoEnvio>TOTAL</TipoEnvio>" not in xml

    def test_ventas_ajuste_delta_solo_t61(
        self, session, run, empresa, servicio_fake,
    ):
        """AJUSTE delta ventas: cuando hay T33+T56+T61 en el set pero ya se
        envió el TOTAL, el AJUSTE debe contener SOLO los T61.

        Raíz (cert 77051056-2, 2026-05): enviar T33+T56+T61 como AJUSTE
        producía LBR-3 ("No Hay Resumen Para Informacion de Detalle") porque
        el SII procesa AJUSTE como reemplazo parcial por TipoDoc — los tipos
        idénticos al TOTAL original no tienen justificación de corrección.

        El único tipo que cambió en ese set era T61 (se eliminó TpoDocRef
        para NCs que referencian T33/T34/T52; ese campo es válido solo para
        liquidaciones T40/T43/T103). T33 y T56 son idénticos al TOTAL.
        """
        _make_dtes(session, empresa, [
            (33, 100), (33, 101),  # idénticos al TOTAL — NO deben ir en AJUSTE
            (56, 10),               # idéntico al TOTAL — NO debe ir en AJUSTE
            (61, 50), (61, 51),    # cambiaron (sin TpoDocRef) — SÍ deben ir
        ])
        libro = _make_libro(session, run, "ventas", numero_atencion=4840936)
        libro.primer_envio_sii_at = datetime.now(timezone.utc)
        session.commit()

        generar_libro(session, run, libro.id, servicio_fake, empresa)

        session.refresh(libro)
        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")

        assert "<TipoEnvio>AJUSTE</TipoEnvio>" in xml

        # Solo 2 Detalle: los 2 T61 (folios 50 y 51)
        assert xml.count("<Detalle>") == 2, (
            "AJUSTE delta debe incluir SOLO los 2 T61; T33 y T56 son "
            "idénticos al TOTAL y causan LBR-3 si se incluyen en AJUSTE"
        )

        # T61 sí deben aparecer
        assert "<TpoDoc>61</TpoDoc>" in xml
        assert "<NroDoc>50</NroDoc>" in xml
        assert "<NroDoc>51</NroDoc>" in xml

        # T33 y T56 NO deben aparecer en Detalle ni en ResumenPeriodo
        assert "<NroDoc>100</NroDoc>" not in xml
        assert "<NroDoc>101</NroDoc>" not in xml
        assert "<NroDoc>10</NroDoc>" not in xml

        # Solo 1 TotalesPeriodo en ResumenPeriodo (solo T61)
        assert xml.count("<TotalesPeriodo>") == 1, (
            "ResumenPeriodo del AJUSTE debe tener solo 1 TotalesPeriodo "
            "(T61); T33 y T56 se conservan del TOTAL original en el SII"
        )

    def test_ventas_ajuste_fallback_sin_t61(
        self, session, run, empresa, servicio_fake,
    ):
        """AJUSTE fallback: si no hay T61 en el set, se usa la lista
        completa (todos los tipos cambiaron en ese envío)."""
        _make_dtes(session, empresa, [(33, 100), (33, 101)])
        libro = _make_libro(session, run, "ventas", numero_atencion=4840936)
        libro.primer_envio_sii_at = datetime.now(timezone.utc)
        session.commit()

        generar_libro(session, run, libro.id, servicio_fake, empresa)

        session.refresh(libro)
        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")

        assert "<TipoEnvio>AJUSTE</TipoEnvio>" in xml
        # Fallback: sin T61, se incluyen todos los DTEs disponibles
        assert xml.count("<Detalle>") == 2
        assert "<TpoDoc>33</TpoDoc>" in xml

    def test_guias_reenvio_sigue_usando_total(
        self, session, run, empresa, servicio_fake,
    ):
        """LibroGuia oficialmente SOLO acepta TOTAL/PARCIAL (no AJUSTE).
        Este test asegura que nunca emitimos AJUSTE para guías."""
        _make_dtes(session, empresa, [(52, 80)])
        libro = _make_libro(session, run, "guias", numero_atencion=4788487)
        libro.primer_envio_sii_at = datetime.now(timezone.utc)
        session.commit()

        generar_libro(session, run, libro.id, servicio_fake, empresa)

        session.refresh(libro)
        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")
        assert "<TipoEnvio>TOTAL</TipoEnvio>" in xml
        assert "<TipoEnvio>AJUSTE</TipoEnvio>" not in xml

    def test_compras_reenvio_usa_ajuste(
        self, session, run, empresa, servicio_fake,
    ):
        """Mismo comportamiento que ventas: AJUSTE en re-envío correctivo."""
        datos = {"entradas": [{
            "tipo_doc": 33,
            "folio": 500,
            "observaciones": "",
            "monto_exento": 0,
            "monto_afecto": 10000,
        }]}
        libro = _make_libro(
            session, run, "compras",
            datos=datos,
            numero_atencion=4788488,
        )
        libro.primer_envio_sii_at = datetime.now(timezone.utc)
        session.commit()

        generar_libro(session, run, libro.id, servicio_fake, empresa)

        session.refresh(libro)
        xml = base64.b64decode(libro.xml_libro).decode("ISO-8859-1")
        assert "<TipoEnvio>AJUSTE</TipoEnvio>" in xml
        assert "<TipoEnvio>TOTAL</TipoEnvio>" not in xml


# ══════════════════════════════════════════════════════════════════
# Guardia anti-LBR-2: _validar_xml_libro_ventas_sin_lbr2
# ══════════════════════════════════════════════════════════════════


class TestValidarXmlLibroVentasSinLbr2:
    """Circuito de corte anti-LBR-2 en el pipeline de libro de ventas.

    El SII emite reparo LBR-2 "Reparo en Calculo de [TpoDoc] debe ser
    [40, 43, 103]" cuando el XML del libro incluye TpoDocRef con un valor
    que no corresponde a una liquidación.  Esta guardia se llama ANTES de
    firmar el XML, de modo que cualquier regresión en el generador sea
    detectada antes de desperdiciar el TOTAL del período.
    """

    def _xml_con_detalle(
        self,
        tpo_doc: int,
        tpo_doc_ref: int | None = None,
        folio_doc_ref: int | None = None,
    ) -> str:
        """XML mínimo con un <Detalle> para TpoDoc dado."""
        ref_xml = ""
        if tpo_doc_ref is not None:
            ref_xml += f"<TpoDocRef>{tpo_doc_ref}</TpoDocRef>"
        if folio_doc_ref is not None:
            ref_xml += f"<FolioDocRef>{folio_doc_ref}</FolioDocRef>"
        return (
            f"<LibroCompraVenta>"
            f"<Detalle>"
            f"<TpoDoc>{tpo_doc}</TpoDoc>"
            f"<NroDoc>141</NroDoc>"
            f"{ref_xml}"
            f"<MntTotal>-100000</MntTotal>"
            f"</Detalle>"
            f"</LibroCompraVenta>"
        )

    def test_t61_sin_tpo_doc_ref_ok(self):
        """NC sin TpoDocRef — sin LBR-2, OK."""
        xml = self._xml_con_detalle(tpo_doc=61)
        _validar_xml_libro_ventas_sin_lbr2(xml)  # no debe lanzar

    def test_t61_tpo_doc_ref_40_ok(self):
        """NC T61 con TpoDocRef=40 (liquidación papel) — válido, OK."""
        xml = self._xml_con_detalle(tpo_doc=61, tpo_doc_ref=40, folio_doc_ref=5)
        _validar_xml_libro_ventas_sin_lbr2(xml)  # no debe lanzar

    def test_t61_tpo_doc_ref_43_ok(self):
        """NC T61 con TpoDocRef=43 (liquidación electrónica) — válido, OK."""
        xml = self._xml_con_detalle(tpo_doc=61, tpo_doc_ref=43, folio_doc_ref=7)
        _validar_xml_libro_ventas_sin_lbr2(xml)  # no debe lanzar

    def test_t61_tpo_doc_ref_103_ok(self):
        """NC T61 con TpoDocRef=103 (liquidación electrónica DTE) — válido, OK."""
        xml = self._xml_con_detalle(tpo_doc=61, tpo_doc_ref=103, folio_doc_ref=9)
        _validar_xml_libro_ventas_sin_lbr2(xml)  # no debe lanzar

    def test_t61_tpo_doc_ref_33_lanza_error(self):
        """NC T61 con TpoDocRef=33 — debe lanzar ValueError con mensaje LBR-2.

        Este es el caso exacto que causó la certificación bloqueada de
        77051056-2 en 2026-05. La guardia debe cortar ANTES de firmar.
        """
        xml = self._xml_con_detalle(tpo_doc=61, tpo_doc_ref=33, folio_doc_ref=140)
        with pytest.raises(ValueError, match="ANTI-LBR-2"):
            _validar_xml_libro_ventas_sin_lbr2(xml)

    def test_t61_tpo_doc_ref_34_lanza_error(self):
        """NC T61 con TpoDocRef=34 (factura exenta) — también inválido."""
        xml = self._xml_con_detalle(tpo_doc=61, tpo_doc_ref=34, folio_doc_ref=10)
        with pytest.raises(ValueError, match="ANTI-LBR-2"):
            _validar_xml_libro_ventas_sin_lbr2(xml)

    def test_t56_tpo_doc_ref_33_lanza_error(self):
        """ND T56 con TpoDocRef=33 — también inválido."""
        xml = self._xml_con_detalle(tpo_doc=56, tpo_doc_ref=33, folio_doc_ref=50)
        with pytest.raises(ValueError, match="ANTI-LBR-2"):
            _validar_xml_libro_ventas_sin_lbr2(xml)

    def test_t33_sin_tpo_doc_ref_ok(self):
        """Factura T33 normal sin TpoDocRef — sin error."""
        xml = self._xml_con_detalle(tpo_doc=33)
        _validar_xml_libro_ventas_sin_lbr2(xml)  # no debe lanzar

    def test_multiples_detalles_uno_invalido_lanza(self):
        """Libro con múltiples detalles — uno inválido dispara el error."""
        xml = (
            "<LibroCompraVenta>"
            "<Detalle><TpoDoc>33</TpoDoc><NroDoc>1</NroDoc><MntTotal>100000</MntTotal></Detalle>"
            "<Detalle><TpoDoc>33</TpoDoc><NroDoc>2</NroDoc><MntTotal>200000</MntTotal></Detalle>"
            # Esta NC con TpoDocRef=33 es la que debe disparar el error:
            "<Detalle><TpoDoc>61</TpoDoc><NroDoc>141</NroDoc>"
            "<TpoDocRef>33</TpoDocRef><FolioDocRef>1</FolioDocRef>"
            "<MntTotal>-50000</MntTotal></Detalle>"
            "</LibroCompraVenta>"
        )
        with pytest.raises(ValueError, match="ANTI-LBR-2"):
            _validar_xml_libro_ventas_sin_lbr2(xml)

    def test_xml_vacio_ok(self):
        """XML sin ningún <Detalle> — no hay nada que verificar, OK."""
        _validar_xml_libro_ventas_sin_lbr2("<LibroCompraVenta></LibroCompraVenta>")
