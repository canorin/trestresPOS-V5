"""Tests del parser de Intercambio de Información.

Fixture: `tests/fixtures/intercambio/ENVIO_DTE_4792140.xml` — XML real
que el SII envió a GRUPO TRESTRES SPA (77829149-5) durante certificación.

Casos canónicos:
- 2 DTEs tipo 33.
- Folio 52126 → RUTRecep 77829149-5 (nosotros) → debe aceptarse.
- Folio 52127 → RUTRecep 69507000-4 (otro)    → debe rechazarse.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from crumbpos.core.intercambio.parser import (
    DteIntercambio,
    SobreIntercambio,
    parsear_envio_dte_desde_archivo,
    parsear_envio_dte_sii,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "intercambio"
FIXTURE_SII = FIXTURE_DIR / "ENVIO_DTE_4792140.xml"


# ═══════════════════════════════════════════════════════════════════
# Fixture real del SII
# ═══════════════════════════════════════════════════════════════════

class TestParserXmlRealDelSii:
    def test_parsea_caratula(self):
        sobre = parsear_envio_dte_desde_archivo(FIXTURE_SII)

        assert isinstance(sobre, SobreIntercambio)
        assert sobre.set_id == "SetDoc"
        assert sobre.rut_emisor == "88888888-8"       # Simulador SII
        assert sobre.rut_envia == "8414240-9"          # Firmante del sobre
        assert sobre.rut_receptor == "77829149-5"      # GRUPO TRESTRES
        assert sobre.tmst_firma_env == "2026-04-23T18:28:50"

    def test_conserva_nombre_archivo(self):
        sobre = parsear_envio_dte_desde_archivo(FIXTURE_SII)
        assert sobre.nombre_archivo == "ENVIO_DTE_4792140.xml"

    def test_conserva_bytes_originales(self):
        bytes_originales = FIXTURE_SII.read_bytes()
        sobre = parsear_envio_dte_desde_archivo(FIXTURE_SII)
        assert sobre.xml_bytes == bytes_originales

    def test_digest_es_sha1_base64_de_bytes_originales(self):
        bytes_originales = FIXTURE_SII.read_bytes()
        esperado = base64.b64encode(hashlib.sha1(bytes_originales).digest()).decode()

        sobre = parsear_envio_dte_desde_archivo(FIXTURE_SII)
        assert sobre.digest_sha1_b64 == esperado

    def test_extrae_dos_dtes(self):
        sobre = parsear_envio_dte_desde_archivo(FIXTURE_SII)
        assert len(sobre.dtes) == 2

    def test_primer_dte_es_para_el_receptor(self):
        """Folio 52126 va dirigido a GRUPO TRESTRES — debe aceptarse."""
        sobre = parsear_envio_dte_desde_archivo(FIXTURE_SII)
        dte1 = sobre.dtes[0]

        assert dte1.tipo_dte == 33
        assert dte1.folio == 52126
        assert dte1.fch_emis == "2026-04-23"
        assert dte1.rut_emisor == "88888888-8"
        assert dte1.rut_recep == "77829149-5"
        assert dte1.mnt_total == 2565

    def test_segundo_dte_es_para_otro_rut(self):
        """Folio 52127 va dirigido a otro RUT — debe rechazarse en Recepcion."""
        sobre = parsear_envio_dte_desde_archivo(FIXTURE_SII)
        dte2 = sobre.dtes[1]

        assert dte2.tipo_dte == 33
        assert dte2.folio == 52127
        assert dte2.fch_emis == "2013-06-21"
        assert dte2.rut_emisor == "88888888-8"
        assert dte2.rut_recep == "69507000-4"
        assert dte2.mnt_total == 3755


# ═══════════════════════════════════════════════════════════════════
# Edge cases y validación
# ═══════════════════════════════════════════════════════════════════

class TestParserValidacion:
    def test_bytes_vacios_falla(self):
        with pytest.raises(ValueError, match="vacío"):
            parsear_envio_dte_sii(b"")

    def test_whitespace_vacio_falla(self):
        with pytest.raises(ValueError, match="vacío"):
            parsear_envio_dte_sii(b"   \n  \t  ")

    def test_xml_corrupto_falla(self):
        with pytest.raises(ValueError, match="inválido"):
            parsear_envio_dte_sii(b"<EnvioDTE><roto")

    def test_root_incorrecto_falla(self):
        with pytest.raises(ValueError, match="EnvioDTE"):
            parsear_envio_dte_sii(
                b'<?xml version="1.0"?><Otro xmlns="http://www.sii.cl/SiiDte"/>'
            )

    def test_sin_setdte_falla(self):
        xml = (
            b'<?xml version="1.0" encoding="ISO-8859-1"?>'
            b'<EnvioDTE xmlns="http://www.sii.cl/SiiDte"/>'
        )
        with pytest.raises(ValueError, match="SetDTE"):
            parsear_envio_dte_sii(xml)

    def test_sin_dtes_falla(self):
        xml = (
            b'<?xml version="1.0" encoding="ISO-8859-1"?>'
            b'<EnvioDTE xmlns="http://www.sii.cl/SiiDte">'
            b'<SetDTE ID="SetDoc"><Caratula>'
            b'<RutEmisor>88888888-8</RutEmisor>'
            b'<RutReceptor>77829149-5</RutReceptor>'
            b'<TmstFirmaEnv>2026-04-23T18:28:50</TmstFirmaEnv>'
            b'</Caratula></SetDTE></EnvioDTE>'
        )
        with pytest.raises(ValueError, match="no contiene DTEs"):
            parsear_envio_dte_sii(xml)

    def test_acepta_bytearray(self):
        bytes_originales = FIXTURE_SII.read_bytes()
        sobre = parsear_envio_dte_sii(bytearray(bytes_originales))
        assert len(sobre.dtes) == 2

    def test_rechaza_string_en_vez_de_bytes(self):
        with pytest.raises(TypeError, match="bytes"):
            parsear_envio_dte_sii("<EnvioDTE/>")  # type: ignore[arg-type]

    def test_set_id_por_defecto(self):
        """Si SetDTE no trae ID, devolver 'SetDoc'."""
        xml = (
            b'<?xml version="1.0" encoding="ISO-8859-1"?>'
            b'<EnvioDTE xmlns="http://www.sii.cl/SiiDte">'
            b'<SetDTE><Caratula>'
            b'<RutEmisor>88888888-8</RutEmisor>'
            b'<RutReceptor>77829149-5</RutReceptor>'
            b'<TmstFirmaEnv>2026-04-23T18:28:50</TmstFirmaEnv>'
            b'</Caratula>'
            b'<DTE xmlns="http://www.sii.cl/SiiDte"><Documento ID="T33">'
            b'<Encabezado><IdDoc><TipoDTE>33</TipoDTE><Folio>1</Folio>'
            b'<FchEmis>2026-01-01</FchEmis></IdDoc>'
            b'<Emisor><RUTEmisor>88888888-8</RUTEmisor></Emisor>'
            b'<Receptor><RUTRecep>77829149-5</RUTRecep></Receptor>'
            b'<Totales><MntTotal>1000</MntTotal></Totales>'
            b'</Encabezado></Documento></DTE>'
            b'</SetDTE></EnvioDTE>'
        )
        sobre = parsear_envio_dte_sii(xml)
        assert sobre.set_id == "SetDoc"

    def test_conserva_doc_id(self):
        """El `ID` del Documento se preserva para armar IDs de Recibo."""
        sobre = parsear_envio_dte_desde_archivo(FIXTURE_SII)
        assert sobre.dtes[0].doc_id is not None
        # El XML real usa "T33" como ID del Documento (no T33F52126).
        # El generador deberá componer IDs LibreDTE_T{tipo}F{folio}
        # usando tipo_dte/folio, sin depender de doc_id.
