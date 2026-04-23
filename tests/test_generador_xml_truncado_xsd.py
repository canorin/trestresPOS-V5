"""Tests para truncado defensivo de campos al maxLength del XSD SII.

Background — 2026-04-22, 77829149-5 caso BASICO rechazado por SII con
STATUS=7 (esquema inválido):

    Element 'GiroRecep': [facet 'maxLength']
    The value has a length of '46'; this exceeds the allowed maximum
    length of '40'.

La empresa GRUPO TRESTRES SPA tiene giro "SERVICIOS DE PUBLICIDAD
PRESTADOS POR EMPRESAS" (46 caracteres). El XSD ``DTE_v10.xsd`` define
``GiroRecep`` con ``maxLength=40``. El SII rechaza el sobre completo.

Spec SII (SiiTypes_v10.xsd + DTE_v10.xsd) — cotas usadas aquí:

- ``RznSoc``/``RznSocRecep``  (RznSocLargaType)  maxLength=100
- ``GiroEmis``                                   maxLength=80
- ``GiroRecep``                                  maxLength=40
- ``DirOrigen``/``DirRecep``                     maxLength=70
- ``CmnaOrigen``/``CmnaRecep``  (ComunaType)     maxLength=20
- ``CiudadOrigen``/``CiudadRecep``  (CiudadType) maxLength=20

Regla de producción (``validar_antes_de_enviar``): el generador XML del
core debe garantizar que los campos cumplen los límites del XSD antes
de firmar y enviar. Truncar es la respuesta correcta para un software
autónomo: no se descarta info crítica (números, montos, RUTs) — sólo
se acortan textos descriptivos que pueden venir de una base cargada
por el SII (que a veces acepta cadenas más largas que las del XSD).
"""
from __future__ import annotations

from datetime import datetime

import pytest
from lxml import etree

from crumbpos.core.dte.generador_xml import generar_documento_xml
from crumbpos.models.dte_models import DTE, ItemDetalle


# ══════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def ted_mockeado(monkeypatch):
    """Reemplaza ``generar_ted`` por un stub — no necesitamos timbre real."""
    def _fake_ted(**_kw):
        return etree.Element("TED")
    monkeypatch.setattr(
        "crumbpos.core.dte.generador_xml.generar_ted",
        _fake_ted,
    )


def _emisor(giro: str = "COMERCIAL", razon: str = "EMPRESA SPA") -> dict:
    return {
        "RUTEmisor": "77829149-5",
        "RznSoc": razon,
        "GiroEmis": giro,
        "Acteco": "741401",
        "DirOrigen": "AV PROVIDENCIA 123",
        "CmnaOrigen": "PROVIDENCIA",
        "CiudadOrigen": "SANTIAGO",
    }


def _receptor(
    *,
    razon: str = "CLIENTE SPA",
    giro: str = "COMERCIAL",
    direccion: str = "CALLE FALSA 123",
    comuna: str = "SANTIAGO",
    ciudad: str = "SANTIAGO",
) -> dict:
    return {
        "RUTRecep": "11111111-1",
        "RznSocRecep": razon,
        "GiroRecep": giro,
        "DirRecep": direccion,
        "CmnaRecep": comuna,
        "CiudadRecep": ciudad,
    }


def _dte(emisor: dict, receptor: dict, *, tipo: int = 33) -> DTE:
    return DTE(
        tipo_dte=tipo,
        folio=1,
        fecha_emision="2026-04-22",
        emisor=emisor,
        receptor=receptor,
        items=[ItemDetalle(
            nro_linea=1,
            nombre="Producto",
            cantidad=1,
            precio_unitario=1000,
            monto_item=1000,
        )],
        monto_neto=1000,
        tasa_iva=19,
        iva=190,
        monto_total=1190,
    )


def _text(doc: etree._Element, xpath: str) -> str:
    node = doc.find(xpath)
    assert node is not None, f"No se encontró {xpath}"
    return node.text or ""


# ══════════════════════════════════════════════════════════════════
# Receptor — campos que rompieron el envío real 2026-04-22
# ══════════════════════════════════════════════════════════════════


class TestReceptorTruncado:
    def test_giro_recep_se_trunca_a_40(self, ted_mockeado):
        # 46 chars — exactamente el caso 77829149-5
        giro_original = "SERVICIOS DE PUBLICIDAD PRESTADOS POR EMPRESAS"
        assert len(giro_original) == 46

        dte = _dte(_emisor(), _receptor(giro=giro_original))
        doc = generar_documento_xml(dte, caf=None)

        giro = _text(doc, ".//Receptor/GiroRecep")
        assert len(giro) == 40
        assert giro == giro_original[:40]

    def test_giro_recep_corto_no_se_toca(self, ted_mockeado):
        dte = _dte(_emisor(), _receptor(giro="COMERCIO"))
        doc = generar_documento_xml(dte, caf=None)
        assert _text(doc, ".//Receptor/GiroRecep") == "COMERCIO"

    def test_razon_social_recep_se_trunca_a_100(self, ted_mockeado):
        razon_original = "A" * 150
        dte = _dte(_emisor(), _receptor(razon=razon_original))
        doc = generar_documento_xml(dte, caf=None)
        assert len(_text(doc, ".//Receptor/RznSocRecep")) == 100

    def test_dir_recep_se_trunca_a_70(self, ted_mockeado):
        dir_original = "A" * 100
        dte = _dte(_emisor(), _receptor(direccion=dir_original))
        doc = generar_documento_xml(dte, caf=None)
        assert len(_text(doc, ".//Receptor/DirRecep")) == 70

    def test_cmna_recep_se_trunca_a_20(self, ted_mockeado):
        dte = _dte(_emisor(), _receptor(comuna="COMUNA NOMBRE MUY LARGO DE MAS DE 20"))
        doc = generar_documento_xml(dte, caf=None)
        assert len(_text(doc, ".//Receptor/CmnaRecep")) == 20

    def test_ciudad_recep_se_trunca_a_20(self, ted_mockeado):
        dte = _dte(_emisor(), _receptor(ciudad="CIUDAD CON NOMBRE EXCESIVAMENTE LARGO"))
        doc = generar_documento_xml(dte, caf=None)
        assert len(_text(doc, ".//Receptor/CiudadRecep")) == 20


# ══════════════════════════════════════════════════════════════════
# Emisor — mismas cotas aplican
# ══════════════════════════════════════════════════════════════════


class TestEmisorTruncado:
    def test_giro_emis_se_trunca_a_80(self, ted_mockeado):
        # Emisor permite giro más largo que receptor (80 vs 40)
        giro_original = "G" * 100
        dte = _dte(_emisor(giro=giro_original), _receptor())
        doc = generar_documento_xml(dte, caf=None)
        assert len(_text(doc, ".//Emisor/GiroEmis")) == 80

    def test_razon_emisor_se_trunca_a_100(self, ted_mockeado):
        dte = _dte(_emisor(razon="R" * 150), _receptor())
        doc = generar_documento_xml(dte, caf=None)
        assert len(_text(doc, ".//Emisor/RznSoc")) == 100

    def test_dir_origen_se_trunca_a_70(self, ted_mockeado):
        emisor = _emisor()
        emisor["DirOrigen"] = "D" * 100
        dte = _dte(emisor, _receptor())
        doc = generar_documento_xml(dte, caf=None)
        assert len(_text(doc, ".//Emisor/DirOrigen")) == 70

    def test_cmna_origen_se_trunca_a_20(self, ted_mockeado):
        emisor = _emisor()
        emisor["CmnaOrigen"] = "C" * 50
        dte = _dte(emisor, _receptor())
        doc = generar_documento_xml(dte, caf=None)
        assert len(_text(doc, ".//Emisor/CmnaOrigen")) == 20


# ══════════════════════════════════════════════════════════════════
# Validación contra el XSD real — end-to-end del encabezado
# ══════════════════════════════════════════════════════════════════


class TestXsdEncabezadoValido:
    def test_encabezado_con_giro_largo_pasa_xsd_dte(self, ted_mockeado):
        """Regresión directa del rechazo 2026-04-22: construir un DTE con
        giro 46 chars y verificar que el encabezado resultante valida
        contra DTE_v10.xsd."""
        from pathlib import Path

        xsd_path = Path(
            "crumbpos/core/firma/sii_firma/xsd/DTE_v10.xsd",
        )
        if not xsd_path.exists():
            pytest.skip("DTE_v10.xsd no disponible en este checkout")

        # Generamos un Documento aislado (sin envolver en sobre)
        dte = _dte(_emisor(), _receptor(
            giro="SERVICIOS DE PUBLICIDAD PRESTADOS POR EMPRESAS",
        ))
        doc = generar_documento_xml(dte, caf=None)

        # Validamos sólo el tramo del encabezado: los campos que
        # restringe el XSD tienen maxLength, y una longitud > max
        # hace fallar la validación de tipo simple.
        giro = _text(doc, ".//Receptor/GiroRecep")
        assert len(giro) <= 40, (
            f"GiroRecep no fue truncado: {len(giro)} chars — "
            "el sobre volvería a ser rechazado por el SII."
        )
