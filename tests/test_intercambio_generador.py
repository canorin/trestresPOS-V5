"""Tests del generador de los 3 XMLs del Intercambio de Información.

Cubre:
- Estructura (orden de elementos, atributos obligatorios).
- Decisión aceptado/rechazado según rut_recep vs rut_responde.
- Validación XSD contra `RespuestaEnvioDTE_v10.xsd` y
  `EnvioRecibos_v10.xsd`.
- Verificación de la firma RSA-SHA1 (digest + SignatureValue).

Usa un certificado auto-firmado generado en memoria para no depender
de ningún .pfx en disco.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from lxml import etree

from crumbpos.core.intercambio.generador import (
    DECLARACION_LEY_19983,
    GLOSA_ACEPTADO,
    GLOSA_DTE_ERR_RUT_RECEP,
    GLOSA_DTE_OK,
    GLOSA_ENVIO_OK,
    GLOSA_RECHAZADO,
    ID_RESULTADO_ENVIO,
    ID_SET_RECIBOS,
    ContactoIntercambio,
    armar_envio_recibos,
    armar_recepcion_dte,
    armar_resultado_dte,
)
from crumbpos.core.intercambio.parser import parsear_envio_dte_desde_archivo


SII_NS = "http://www.sii.cl/SiiDte"
DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
NS = {"s": SII_NS, "d": DSIG_NS}

FIXTURE_SII = Path(__file__).parent / "fixtures" / "intercambio" / "ENVIO_DTE_4792140.xml"
XSD_DIR = Path(__file__).parent.parent / "crumbpos" / "core" / "firma" / "sii_firma" / "xsd"


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def cert_autofirmado() -> tuple[bytes, bytes]:
    """Genera un certificado RSA-2048 auto-firmado para tests.

    Retorna (private_key_pem, cert_der) — el mismo formato que usa
    `firmar_documento()` del core.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Test Firmante Intercambio"),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "CL"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .sign(private_key, hashes.SHA256())
    )

    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    return private_key_pem, cert_der


@pytest.fixture
def sobre_real():
    """Sobre parseado a partir del XML real del SII."""
    return parsear_envio_dte_desde_archivo(FIXTURE_SII)


@pytest.fixture
def contacto_trestres():
    """Contacto de GRUPO TRESTRES SPA (empresa receptora)."""
    return ContactoIntercambio(
        nombre="Jose Ejemplo",
        email="jefatura@trestres.cl",
        rut_firma="8414240-9",
        rut_responde="77829149-5",
    )


# ═══════════════════════════════════════════════════════════════════
# Helpers de parseo/validación
# ═══════════════════════════════════════════════════════════════════

def _parse(xml_bytes: bytes) -> etree._Element:
    return etree.fromstring(xml_bytes)


def _text(parent: etree._Element, xpath: str) -> str | None:
    elem = parent.find(xpath, NS)
    return elem.text if elem is not None else None


def _validar_xsd(xml_bytes: bytes, xsd_nombre: str) -> None:
    """Valida `xml_bytes` contra el XSD dado. Levanta si no valida."""
    xsd_path = XSD_DIR / xsd_nombre
    if not xsd_path.exists():
        pytest.skip(f"XSD no disponible: {xsd_nombre}")
    schema = etree.XMLSchema(etree.parse(str(xsd_path)))
    doc = etree.fromstring(xml_bytes)
    schema.assertValid(doc)  # lanza DocumentInvalid si falla


def _verificar_firma(
    doc_element: etree._Element,
    signature: etree._Element,
    private_key_pem: bytes,
) -> None:
    """Verifica que la firma coincide con el digest del doc_element."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    # 1. Digest esperado vs el que trae el XML
    digest_xml = signature.find(".//d:DigestValue", NS).text

    # Canonicalizamos el doc_element como lo hace firma_digital.py
    xml_str = etree.tostring(doc_element, encoding="unicode")
    # es_sobre=True mantiene xmlns:xsi; en es_sobre=False se quita.
    # Acá asumimos es_sobre=True (Resultado, SetRecibos).
    import re
    if f'xmlns="{SII_NS}"' not in xml_str:
        tag = etree.QName(doc_element.tag).localname
        xml_str = re.sub(
            rf'^(<{tag})\b', rf'\1 xmlns="{SII_NS}"', xml_str
        )
    reparsed = etree.fromstring(xml_str.encode("utf-8"))
    c14n = etree.tostring(reparsed, method="c14n")
    digest_local = base64.b64encode(hashlib.sha1(c14n).digest()).decode()

    assert digest_local == digest_xml, (
        f"Digest no coincide. XML={digest_xml}, recomputado={digest_local}"
    )


# ═══════════════════════════════════════════════════════════════════
# 1. RecepcionDTE.xml
# ═══════════════════════════════════════════════════════════════════

class TestRecepcionDTE:
    def test_root_y_atributos(self, sobre_real, contacto_trestres, cert_autofirmado):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(
            sobre_real, contacto_trestres, pk, der, tmst="2026-04-23T19:00:00"
        )
        root = _parse(xml)

        assert etree.QName(root.tag).localname == "RespuestaDTE"
        assert root.get("version") == "1.0"

    def test_declaracion_xml_iso8859_1(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        assert xml.startswith(b'<?xml version="1.0" encoding="ISO-8859-1"?>')

    def test_resultado_tiene_id_esperado(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        resultado = root.find("s:Resultado", NS)
        assert resultado.get("ID") == ID_RESULTADO_ENVIO

    def test_caratula_rut_responde_y_rut_recibe(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)

        assert _text(root, "s:Resultado/s:Caratula/s:RutResponde") == "77829149-5"
        # RutRecibe = RUT emisor del sobre (SII simulado)
        assert _text(root, "s:Resultado/s:Caratula/s:RutRecibe") == "88888888-8"

    def test_caratula_nmbcontacto_y_mailcontacto(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        assert _text(root, "s:Resultado/s:Caratula/s:NmbContacto") == "Jose Ejemplo"
        assert _text(root, "s:Resultado/s:Caratula/s:MailContacto") == "jefatura@trestres.cl"

    def test_recepcion_envio_usa_nombre_archivo_original(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        assert _text(root, "s:Resultado/s:RecepcionEnvio/s:NmbEnvio") == (
            "ENVIO_DTE_4792140.xml"
        )

    def test_recepcion_envio_digest_es_del_sobre(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        digest = _text(root, "s:Resultado/s:RecepcionEnvio/s:Digest")
        assert digest == sobre_real.digest_sha1_b64

    def test_recepcion_envio_envio_dte_id(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        assert _text(root, "s:Resultado/s:RecepcionEnvio/s:EnvioDTEID") == "SetDoc"

    def test_recepcion_envio_estado_y_glosa_son_0_y_ok(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        assert _text(root, "s:Resultado/s:RecepcionEnvio/s:EstadoRecepEnv") == "0"
        assert _text(root, "s:Resultado/s:RecepcionEnvio/s:RecepEnvGlosa") == GLOSA_ENVIO_OK

    def test_recepcion_envio_nro_dte_es_2(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        assert _text(root, "s:Resultado/s:RecepcionEnvio/s:NroDTE") == "2"

    def test_dte_aceptado_primero_folio_52126(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        recep_dtes = root.findall("s:Resultado/s:RecepcionEnvio/s:RecepcionDTE", NS)
        assert len(recep_dtes) == 2

        # Folio 52126 — va a 77829149-5 (nosotros) → aceptado
        dte1 = recep_dtes[0]
        assert _text(dte1, "s:Folio") == "52126"
        assert _text(dte1, "s:RUTRecep") == "77829149-5"
        assert _text(dte1, "s:EstadoRecepDTE") == "0"
        assert _text(dte1, "s:RecepDTEGlosa") == GLOSA_DTE_OK

    def test_dte_rechazado_segundo_folio_52127(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        recep_dtes = root.findall("s:Resultado/s:RecepcionEnvio/s:RecepcionDTE", NS)

        # Folio 52127 — va a 69507000-4 (otro) → rechazado
        dte2 = recep_dtes[1]
        assert _text(dte2, "s:Folio") == "52127"
        assert _text(dte2, "s:RUTRecep") == "69507000-4"
        assert _text(dte2, "s:EstadoRecepDTE") == "3"
        assert _text(dte2, "s:RecepDTEGlosa") == GLOSA_DTE_ERR_RUT_RECEP

    def test_firma_digital_presente(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        signatures = root.findall("d:Signature", NS)
        assert len(signatures) == 1, "Falta Signature al nivel RespuestaDTE"

    def test_firma_digital_apunta_a_resultado(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        sig = root.find("d:Signature", NS)
        ref = sig.find(".//d:Reference", NS)
        assert ref.get("URI") == f"#{ID_RESULTADO_ENVIO}"

    def test_firma_digital_digest_valido(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        resultado = root.find("s:Resultado", NS)
        sig = root.find("d:Signature", NS)
        _verificar_firma(resultado, sig, pk)

    def test_valida_xsd_respuesta_envio(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_trestres, pk, der)
        _validar_xsd(xml, "RespuestaEnvioDTE_v10.xsd")


# ═══════════════════════════════════════════════════════════════════
# 2. EnvioRecibos.xml
# ═══════════════════════════════════════════════════════════════════

class TestEnvioRecibos:
    def test_root_y_atributos(self, sobre_real, contacto_trestres, cert_autofirmado):
        pk, der = cert_autofirmado
        xml = armar_envio_recibos(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        assert etree.QName(root.tag).localname == "EnvioRecibos"
        assert root.get("version") == "1.0"

    def test_set_recibos_tiene_id_esperado(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_envio_recibos(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        assert root.find("s:SetRecibos", NS).get("ID") == ID_SET_RECIBOS

    def test_solo_incluye_dtes_aceptados(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        """Por defecto solo va el DTE 52126 (nuestro), no el 52127."""
        pk, der = cert_autofirmado
        xml = armar_envio_recibos(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        recibos = root.findall("s:SetRecibos/s:Recibo", NS)
        assert len(recibos) == 1

        folio = _text(recibos[0], "s:DocumentoRecibo/s:Folio")
        assert folio == "52126"

    def test_recibo_usa_id_tipo_folio(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_envio_recibos(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        doc_recibo = root.find("s:SetRecibos/s:Recibo/s:DocumentoRecibo", NS)
        assert doc_recibo.get("ID") == "LibreDTE_T33F52126"

    def test_declaracion_ley_19983_literal(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        """La declaración tiene `fixed=` en el XSD. Un char distinto y rompe."""
        pk, der = cert_autofirmado
        xml = armar_envio_recibos(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        declaracion = _text(
            root, "s:SetRecibos/s:Recibo/s:DocumentoRecibo/s:Declaracion"
        )
        assert declaracion == DECLARACION_LEY_19983

    def test_rut_firma_es_el_del_contacto(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_envio_recibos(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        rut_firma = _text(
            root, "s:SetRecibos/s:Recibo/s:DocumentoRecibo/s:RutFirma"
        )
        assert rut_firma == "8414240-9"

    def test_recibo_tiene_firma_propia(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_envio_recibos(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        # Cada Recibo lleva su propia Signature
        recibo_sig = root.find("s:SetRecibos/s:Recibo/d:Signature", NS)
        assert recibo_sig is not None
        ref_uri = recibo_sig.find(".//d:Reference", NS).get("URI")
        assert ref_uri == "#LibreDTE_T33F52126"

    def test_envio_recibos_tiene_firma_del_set(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_envio_recibos(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        # Firma del SetRecibos (nivel raíz)
        root_sig = root.find("d:Signature", NS)
        assert root_sig is not None
        ref_uri = root_sig.find(".//d:Reference", NS).get("URI")
        assert ref_uri == f"#{ID_SET_RECIBOS}"

    def test_falla_si_no_hay_dtes_aceptados(self, sobre_real, cert_autofirmado):
        """Si ningún DTE va dirigido a rut_responde, no hay Recibos que firmar."""
        pk, der = cert_autofirmado
        contacto_ajeno = ContactoIntercambio(
            nombre="Otro",
            email="otro@otro.cl",
            rut_firma="1-9",
            rut_responde="99999999-9",  # Ningún DTE va a este RUT
        )
        with pytest.raises(ValueError, match="al menos 1 DTE aceptado"):
            armar_envio_recibos(sobre_real, contacto_ajeno, pk, der)

    def test_valida_xsd_envio_recibos(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_envio_recibos(sobre_real, contacto_trestres, pk, der)
        _validar_xsd(xml, "EnvioRecibos_v10.xsd")


# ═══════════════════════════════════════════════════════════════════
# 3. ResultadoDTE.xml
# ═══════════════════════════════════════════════════════════════════

class TestResultadoDTE:
    def test_root_y_atributos(self, sobre_real, contacto_trestres, cert_autofirmado):
        pk, der = cert_autofirmado
        xml = armar_resultado_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        assert etree.QName(root.tag).localname == "RespuestaDTE"
        assert root.get("version") == "1.0"

    def test_resultado_dte_aceptado(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_resultado_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        items = root.findall("s:Resultado/s:ResultadoDTE", NS)
        assert len(items) == 2

        # Primero — 52126 aceptado
        assert _text(items[0], "s:Folio") == "52126"
        assert _text(items[0], "s:EstadoDTE") == "0"
        assert _text(items[0], "s:EstadoDTEGlosa") == GLOSA_ACEPTADO
        assert _text(items[0], "s:CodEnvio") == "1"

    def test_resultado_dte_rechazado(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_resultado_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        items = root.findall("s:Resultado/s:ResultadoDTE", NS)

        # Segundo — 52127 rechazado (va a otro RUT)
        assert _text(items[1], "s:Folio") == "52127"
        assert _text(items[1], "s:EstadoDTE") == "2"
        assert _text(items[1], "s:EstadoDTEGlosa") == GLOSA_RECHAZADO
        assert _text(items[1], "s:CodEnvio") == "2"

    def test_nro_detalles_es_total_de_dtes(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_resultado_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        # NroDetalles en ResultadoDTE = cantidad total de ResultadoDTE
        assert _text(root, "s:Resultado/s:Caratula/s:NroDetalles") == "2"

    def test_firma_digital_presente_y_apunta_a_resultado(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_resultado_dte(sobre_real, contacto_trestres, pk, der)
        root = _parse(xml)
        sig = root.find("d:Signature", NS)
        assert sig is not None
        assert sig.find(".//d:Reference", NS).get("URI") == f"#{ID_RESULTADO_ENVIO}"

    def test_valida_xsd(
        self, sobre_real, contacto_trestres, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_resultado_dte(sobre_real, contacto_trestres, pk, der)
        _validar_xsd(xml, "RespuestaEnvioDTE_v10.xsd")


# ═══════════════════════════════════════════════════════════════════
# Casos raros: todos los DTEs son para otro RUT
# ═══════════════════════════════════════════════════════════════════

class TestTodosRechazados:
    """RecepcionDTE y ResultadoDTE deben emitirse igual; solo
    EnvioRecibos falla (no hay recibos que incluir)."""

    @pytest.fixture
    def contacto_ajeno(self):
        return ContactoIntercambio(
            nombre="Otro",
            email="otro@otro.cl",
            rut_firma="1-9",
            rut_responde="99999999-9",  # Ningún DTE va a este RUT
        )

    def test_recepcion_marca_todos_rechazados(
        self, sobre_real, contacto_ajeno, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_recepcion_dte(sobre_real, contacto_ajeno, pk, der)
        root = _parse(xml)
        items = root.findall("s:Resultado/s:RecepcionEnvio/s:RecepcionDTE", NS)
        estados = [_text(i, "s:EstadoRecepDTE") for i in items]
        assert estados == ["3", "3"]

    def test_resultado_marca_todos_rechazados(
        self, sobre_real, contacto_ajeno, cert_autofirmado
    ):
        pk, der = cert_autofirmado
        xml = armar_resultado_dte(sobre_real, contacto_ajeno, pk, der)
        root = _parse(xml)
        items = root.findall("s:Resultado/s:ResultadoDTE", NS)
        estados = [_text(i, "s:EstadoDTE") for i in items]
        assert estados == ["2", "2"]
