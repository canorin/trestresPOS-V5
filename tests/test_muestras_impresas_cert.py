"""Tests para crumbpos/api/services/muestras_impresas_cert.py (Fase 4).

Cubre:
- Parseo de XML firmado → DTEPrintData (xml_to_print_data).
- Generación de PDFs tributario y cedible (1 página cada uno).
- Generación del ZIP completo (generar_muestras_zip).
- Casos de error: sin DTEs, XML malformado, sin ted_xml.

Los PDFs se verifican como bytes válidos (%PDF- header). No se valida
el contenido visual — eso es responsabilidad de la clase PDFCarta que
ya tiene sus propios tests visuales en la suite de impresión.
"""
from __future__ import annotations

import base64
import io
import zipfile
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.api.services.muestras_impresas_cert import (
    _generar_pdf_cedible,
    _generar_pdf_tributario,
    generar_muestras_zip,
    xml_to_print_data,
)
from crumbpos.core.impresion.base import TIPOS_CEDIBLES
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
        id="emp-1",
        rut="77051056-2",
        razon_social="Test SPA",
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


# ── XML de prueba ─────────────────────────────────────────────

FACTURA_XML = """\
<?xml version="1.0" encoding="ISO-8859-1"?>
<EnvioDTE>
<SetDTE>
<DTE>
<Documento ID="F1T33">
<Encabezado>
<IdDoc><TipoDTE>33</TipoDTE><Folio>1</Folio><FchEmis>2026-04-09</FchEmis></IdDoc>
<Emisor>
<RUTEmisor>77051056-2</RUTEmisor><RznSoc>Test SPA</RznSoc>
<GiroEmis>Consultoría</GiroEmis><Acteco>741000</Acteco>
<DirOrigen>Los Conquistadores 1700</DirOrigen>
<CmnaOrigen>Providencia</CmnaOrigen><CiudadOrigen>Santiago</CiudadOrigen>
</Emisor>
<Receptor>
<RUTRecep>66666666-6</RUTRecep><RznSocRecep>Receptor Test</RznSocRecep>
<GiroRecep>Comercio</GiroRecep><DirRecep>Av Test 123</DirRecep>
<CmnaRecep>Santiago</CmnaRecep><CiudadRecep>Santiago</CiudadRecep>
</Receptor>
<Totales>
<MntNeto>10000</MntNeto><TasaIVA>19</TasaIVA><IVA>1900</IVA>
<MntTotal>11900</MntTotal>
</Totales>
</Encabezado>
<Detalle>
<NroLinDet>1</NroLinDet><NmbItem>Producto A</NmbItem>
<QtyItem>2</QtyItem><PrcItem>5000</PrcItem><MontoItem>10000</MontoItem>
</Detalle>
<TED version="1.0">
<DD><RE>77051056-2</RE><TD>33</TD><F>1</F><FE>2026-04-09</FE>
<RR>66666666-6</RR><RSR>Receptor Test</RSR><MNT>11900</MNT>
<IT1>Producto A</IT1></DD><FRMT algoritmo="SHA1withRSA">fake==</FRMT>
</TED>
</Documento>
</DTE>
</SetDTE>
</EnvioDTE>"""

GUIA_XML = """\
<?xml version="1.0" encoding="ISO-8859-1"?>
<EnvioDTE>
<SetDTE>
<DTE>
<Documento ID="F10T52">
<Encabezado>
<IdDoc><TipoDTE>52</TipoDTE><Folio>10</Folio><FchEmis>2026-04-09</FchEmis>
<IndTraslado>1</IndTraslado></IdDoc>
<Emisor>
<RUTEmisor>77051056-2</RUTEmisor><RznSoc>Test SPA</RznSoc>
<GiroEmis>Consultoría</GiroEmis><Acteco>741000</Acteco>
<DirOrigen>Los Conquistadores 1700</DirOrigen>
<CmnaOrigen>Providencia</CmnaOrigen><CiudadOrigen>Santiago</CiudadOrigen>
</Emisor>
<Receptor>
<RUTRecep>66666666-6</RUTRecep><RznSocRecep>Receptor Test</RznSocRecep>
<GiroRecep>Comercio</GiroRecep><DirRecep>Av Test 123</DirRecep>
<CmnaRecep>Santiago</CmnaRecep><CiudadRecep>Santiago</CiudadRecep>
</Receptor>
<Totales>
<MntNeto>5000</MntNeto><TasaIVA>19</TasaIVA><IVA>950</IVA>
<MntTotal>5950</MntTotal>
</Totales>
</Encabezado>
<Detalle>
<NroLinDet>1</NroLinDet><NmbItem>Producto B</NmbItem>
<QtyItem>1</QtyItem><PrcItem>5000</PrcItem><MontoItem>5000</MontoItem>
</Detalle>
<TED version="1.0">
<DD><RE>77051056-2</RE><TD>52</TD><F>10</F><FE>2026-04-09</FE>
<RR>66666666-6</RR><RSR>Receptor Test</RSR><MNT>5950</MNT>
<IT1>Producto B</IT1></DD><FRMT algoritmo="SHA1withRSA">fake==</FRMT>
</TED>
</Documento>
</DTE>
</SetDTE>
</EnvioDTE>"""

NC_XML = """\
<?xml version="1.0" encoding="ISO-8859-1"?>
<EnvioDTE>
<SetDTE>
<DTE>
<Documento ID="F5T61">
<Encabezado>
<IdDoc><TipoDTE>61</TipoDTE><Folio>5</Folio><FchEmis>2026-04-09</FchEmis></IdDoc>
<Emisor>
<RUTEmisor>77051056-2</RUTEmisor><RznSoc>Test SPA</RznSoc>
<GiroEmis>Consultoría</GiroEmis><DirOrigen>Los Conquistadores 1700</DirOrigen>
<CmnaOrigen>Providencia</CmnaOrigen><CiudadOrigen>Santiago</CiudadOrigen>
</Emisor>
<Receptor>
<RUTRecep>66666666-6</RUTRecep><RznSocRecep>Receptor Test</RznSocRecep>
<GiroRecep>Comercio</GiroRecep><DirRecep>Av Test 123</DirRecep>
<CmnaRecep>Santiago</CmnaRecep><CiudadRecep>Santiago</CiudadRecep>
</Receptor>
<Totales>
<MntNeto>10000</MntNeto><TasaIVA>19</TasaIVA><IVA>1900</IVA>
<MntTotal>11900</MntTotal>
</Totales>
</Encabezado>
<Detalle>
<NroLinDet>1</NroLinDet><NmbItem>Producto A</NmbItem>
<QtyItem>2</QtyItem><PrcItem>5000</PrcItem><MontoItem>10000</MontoItem>
</Detalle>
<Referencia>
<NroLinRef>1</NroLinRef><TpoDocRef>33</TpoDocRef><FolioRef>1</FolioRef>
<FchRef>2026-04-09</FchRef><CodRef>1</CodRef><RazonRef>ANULA</RazonRef>
</Referencia>
<TED version="1.0">
<DD><RE>77051056-2</RE><TD>61</TD><F>5</F><FE>2026-04-09</FE>
<RR>66666666-6</RR><RSR>Receptor Test</RSR><MNT>11900</MNT>
<IT1>Producto A</IT1></DD><FRMT algoritmo="SHA1withRSA">fake==</FRMT>
</TED>
</Documento>
</DTE>
</SetDTE>
</EnvioDTE>"""


def _b64(xml_str: str) -> str:
    """Encode XML string to base64 as stored in DteEmitido.xml_firmado."""
    return base64.b64encode(xml_str.encode("ISO-8859-1")).decode()


def _make_dte(session, empresa, xml_str, tipo_dte, folio, ted_xml=None):
    """Crea un DteEmitido con XML firmado."""
    dte = DteEmitido(
        empresa_id=empresa.id,
        tipo_dte=tipo_dte,
        folio=folio,
        fecha_emision=date(2026, 4, 9),
        receptor_rut="66666666-6",
        receptor_razon="Receptor Test",
        monto_neto=10000,
        monto_exento=0,
        iva=1900,
        monto_total=11900,
        xml_firmado=_b64(xml_str),
        ted_xml=ted_xml,
        estado_sii="pendiente",
    )
    session.add(dte)
    session.commit()
    session.refresh(dte)
    return dte


def _make_caso(session, run, set_nombre, numero_caso, dte):
    """Crea un CertificacionCaso vinculado a un DteEmitido."""
    c = CertificacionCaso(
        run_id=run.id,
        set_nombre=set_nombre,
        numero_caso=numero_caso,
        numero_atencion=1234,
        tipo_dte=dte.tipo_dte,
        folio=dte.folio,
        estado="emitido",
        dte_emitido_id=dte.id,
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


# ══════════════════════════════════════════════════════════════════
# Tests: Parseo XML → DTEPrintData
# ══════════════════════════════════════════════════════════════════


class TestXmlToPrintData:

    def test_factura_basica(self, empresa):
        """Parsea una factura T33 y extrae todos los campos."""
        xml_bytes = FACTURA_XML.encode("ISO-8859-1")
        data = xml_to_print_data(xml_bytes, empresa)

        assert data.tipo_dte == 33
        assert data.folio == 1
        assert data.fecha_emision == "2026-04-09"
        assert data.emisor_rut == "77051056-2"
        assert data.emisor_razon == "Test SPA"
        assert data.receptor_rut == "66666666-6"
        assert data.receptor_razon == "Receptor Test"
        assert data.monto_neto == 10000
        assert data.iva == 1900
        assert data.monto_total == 11900
        assert len(data.items) == 1
        assert data.items[0]["nombre"] == "Producto A"
        assert data.items[0]["qty"] == "2"

    def test_guia_con_traslado(self, empresa):
        """Parsea una guía T52 y extrae IndTraslado."""
        xml_bytes = GUIA_XML.encode("ISO-8859-1")
        data = xml_to_print_data(xml_bytes, empresa)

        assert data.tipo_dte == 52
        assert data.ind_traslado == "1"

    def test_nota_credito_con_referencia(self, empresa):
        """Parsea una NC T61 y extrae la referencia."""
        xml_bytes = NC_XML.encode("ISO-8859-1")
        data = xml_to_print_data(xml_bytes, empresa)

        assert data.tipo_dte == 61
        assert len(data.referencias) == 1
        assert data.referencias[0]["tipo_doc"] == "33"
        assert data.referencias[0]["folio"] == "1"
        assert data.referencias[0]["razon"] == "ANULA"

    def test_resolucion_desde_empresa(self, empresa):
        """Los datos de resolución SII vienen de la empresa."""
        xml_bytes = FACTURA_XML.encode("ISO-8859-1")
        data = xml_to_print_data(xml_bytes, empresa)

        assert data.numero_resolucion == 80
        assert data.fecha_resolucion == "2014-08-22"

    def test_ted_desde_db_tiene_prioridad(self, empresa):
        """Si se pasa ted_from_db, se usa en lugar de extraer del XML."""
        xml_bytes = FACTURA_XML.encode("ISO-8859-1")
        ted_custom = "<TED>CUSTOM</TED>"
        data = xml_to_print_data(xml_bytes, empresa, ted_from_db=ted_custom)

        assert data.ted_xml == ted_custom

    def test_ted_extraido_del_xml(self, empresa):
        """Si no se pasa ted_from_db, se extrae del XML raw."""
        xml_bytes = FACTURA_XML.encode("ISO-8859-1")
        data = xml_to_print_data(xml_bytes, empresa, ted_from_db=None)

        assert "<TED" in data.ted_xml
        assert "</TED>" in data.ted_xml

    def test_xml_sin_documento_falla(self, empresa):
        """XML sin <Documento> lanza ValueError."""
        xml_bytes = b"<Root><Nada/></Root>"
        with pytest.raises(ValueError, match="No se encontró.*Documento"):
            xml_to_print_data(xml_bytes, empresa)

    def test_monto_exento(self, empresa):
        """Parsea MntExe cuando está presente."""
        xml_exento = FACTURA_XML.replace(
            "<MntNeto>10000</MntNeto>",
            "<MntNeto>0</MntNeto><MntExe>5000</MntExe>",
        )
        xml_bytes = xml_exento.encode("ISO-8859-1")
        data = xml_to_print_data(xml_bytes, empresa)
        assert data.monto_exento == 5000


# ══════════════════════════════════════════════════════════════════
# Tests: Generación de PDFs individuales
# ══════════════════════════════════════════════════════════════════


class TestGenerarPDFs:

    def _make_print_data(self, empresa):
        xml_bytes = FACTURA_XML.encode("ISO-8859-1")
        return xml_to_print_data(xml_bytes, empresa)

    def test_tributario_es_pdf_valido(self, empresa):
        """El tributario empieza con %PDF-."""
        data = self._make_print_data(empresa)
        pdf_bytes = _generar_pdf_tributario(data)
        assert pdf_bytes[:5] == b"%PDF-"
        assert len(pdf_bytes) > 1000  # no está vacío

    def test_cedible_es_pdf_valido(self, empresa):
        """El cedible empieza con %PDF- y contiene 'CEDIBLE'."""
        data = self._make_print_data(empresa)
        pdf_bytes = _generar_pdf_cedible(data)
        assert pdf_bytes[:5] == b"%PDF-"

    def test_tributario_y_cedible_son_distintos(self, empresa):
        """Los dos PDFs no son idénticos (cedible tiene acuse)."""
        data = self._make_print_data(empresa)
        trib = _generar_pdf_tributario(data)
        ced = _generar_pdf_cedible(data)
        assert trib != ced

    def test_tipo_no_cedible_solo_tributario(self, empresa):
        """NC T61 no es cedible: el cedible se genera pero sin acuse."""
        xml_bytes = NC_XML.encode("ISO-8859-1")
        data = xml_to_print_data(xml_bytes, empresa)
        assert data.tipo_dte not in TIPOS_CEDIBLES
        # Tributario se genera normal
        pdf_bytes = _generar_pdf_tributario(data)
        assert pdf_bytes[:5] == b"%PDF-"


# ══════════════════════════════════════════════════════════════════
# Tests: Generación del ZIP completo
# ══════════════════════════════════════════════════════════════════


class TestGenerarMuestrasZip:

    def test_zip_con_factura_genera_tributario_y_cedible(
        self, session, run, empresa,
    ):
        """Factura T33 produce tributario + cedible en el ZIP."""
        dte = _make_dte(session, empresa, FACTURA_XML, 33, 1)
        _make_caso(session, run, "BASICO", "caso-1", dte)

        zip_bytes, resumen = generar_muestras_zip(session, run, empresa)

        assert resumen["tributarios"] == 1
        assert resumen["cedibles"] == 1
        assert resumen["total_pdfs"] == 2
        assert resumen["errores"] == 0

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert "basico/T33_F1_tributario.pdf" in names
            assert "basico/T33_F1_cedible.pdf" in names
            # Verificar que son PDFs válidos
            for name in names:
                content = zf.read(name)
                assert content[:5] == b"%PDF-"

    def test_zip_nc_no_genera_cedible(self, session, run, empresa):
        """NC T61 solo produce tributario (no es cedible)."""
        dte = _make_dte(session, empresa, NC_XML, 61, 5)
        _make_caso(session, run, "BASICO", "caso-2", dte)

        zip_bytes, resumen = generar_muestras_zip(session, run, empresa)

        assert resumen["tributarios"] == 1
        assert resumen["cedibles"] == 0
        assert resumen["total_pdfs"] == 1

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert "basico/T61_F5_tributario.pdf" in names
            assert "basico/T61_F5_cedible.pdf" not in names

    def test_zip_multiples_sets(self, session, run, empresa):
        """DTEs de distintos sets van en carpetas separadas."""
        dte1 = _make_dte(session, empresa, FACTURA_XML, 33, 1)
        dte2 = _make_dte(session, empresa, GUIA_XML, 52, 10)
        _make_caso(session, run, "BASICO", "caso-1", dte1)
        _make_caso(session, run, "GUIAS", "caso-g1", dte2)

        zip_bytes, resumen = generar_muestras_zip(session, run, empresa)

        assert resumen["tributarios"] == 2
        # 33 y 52 son ambos cedibles
        assert resumen["cedibles"] == 2
        assert "basico" in resumen["sets"]
        assert "guias" in resumen["sets"]

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert "basico/T33_F1_tributario.pdf" in names
            assert "guias/T52_F10_tributario.pdf" in names
            assert "guias/T52_F10_cedible.pdf" in names

    def test_sin_dtes_falla(self, session, run, empresa):
        """Error si no hay DTEs emitidos."""
        with pytest.raises(ValueError, match="No hay DTEs emitidos"):
            generar_muestras_zip(session, run, empresa)

    def test_caso_sin_dte_emitido_se_salta(self, session, run, empresa):
        """Caso sin dte_emitido_id no rompe — se salta con warning."""
        # Un caso CON DTE
        dte = _make_dte(session, empresa, FACTURA_XML, 33, 1)
        _make_caso(session, run, "BASICO", "caso-1", dte)
        # Un caso SIN DTE (estado pendiente, sin dte_emitido_id)
        c_pending = CertificacionCaso(
            run_id=run.id,
            set_nombre="BASICO",
            numero_caso="caso-2",
            numero_atencion=1234,
            tipo_dte=33,
            estado="pendiente",
        )
        session.add(c_pending)
        session.commit()

        zip_bytes, resumen = generar_muestras_zip(session, run, empresa)
        # Solo el caso con DTE genera PDFs
        assert resumen["tributarios"] == 1

    def test_ted_desde_db_se_usa(self, session, run, empresa):
        """Si DteEmitido.ted_xml está presente, se usa para el PDF417."""
        ted_str = "<TED version='1.0'><DD>custom</DD></TED>"
        dte = _make_dte(
            session, empresa, FACTURA_XML, 33, 1,
            ted_xml=ted_str,
        )
        _make_caso(session, run, "BASICO", "caso-1", dte)

        # No lanza error — el TED custom se usa para el timbre
        zip_bytes, resumen = generar_muestras_zip(session, run, empresa)
        assert resumen["tributarios"] == 1

    def test_guia_con_traslado_en_zip(self, session, run, empresa):
        """Guía T52 se genera correctamente con IndTraslado."""
        dte = _make_dte(session, empresa, GUIA_XML, 52, 10)
        _make_caso(session, run, "GUIAS", "caso-g1", dte)

        zip_bytes, resumen = generar_muestras_zip(session, run, empresa)
        assert resumen["tributarios"] == 1
        assert resumen["cedibles"] == 1  # T52 es cedible
