"""Tests del enriquecimiento CodRef=3 en el CORE único de emisión.

Arquitectura (directriz 2026-04-22):

    ROUTERS (cert / producción)
        │  (mappers puros, sin lógica de procesamiento)
        ▼  FacturaRequest
    CORE ÚNICO: ServicioEmisionDTE.emitir_factura()
        - Enriquece items CodRef=3 desde DteEmitido referenciado
        - Valida (guards CodRef=1/2/3)
        - Firma, asigna folio, arma XML
        - Envía al SII (destino = config.ambiente)

**Un solo lugar procesa documentos.** Lo único que cambia entre cert y
producción es el destino del envío; el core es idéntico.

Este test cubre el enriquecimiento de NC CodRef=3 que ANTES vivía en
``_caso_a_factura_request`` (capa cert). Ahora vive en el core leyendo
``DteEmitido`` referenciado — fuente válida tanto en cert como en
producción (misma tabla, distinta DB por tenant).

Casos cubiertos:
  - Core enriquece items con precio=None leyendo XML del DTE referenciado.
  - Core enriquece items con precio=0 (análogo a None).
  - Core hereda ``descuento_pct`` cuando el NC no lo trae.
  - Core preserva precios ya presentes en el request (no sobrescribe).
  - Core falla cuando el DTE referenciado no existe en la BD.
  - Core falla cuando un item del NC no matchea por nombre con el original.
  - Core NO enriquece si CodRef != 3 (solo aplica a MODIFICA MONTO).
  - Core NO enriquece si todos los items traen precio > 0.
"""
from __future__ import annotations

import base64
import tempfile
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.api.services.emision_dte import (
    EmisorConfig,
    FacturaRequest,
    ServicioEmisionDTE,
)
from crumbpos.db.models import Base, DteEmitido, Empresa


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
        giro="SERVICIOS",
        direccion="AV PROVIDENCIA 123",
        comuna="PROVIDENCIA",
        ciudad="SANTIAGO",
        cert_rut_firmante="11111111-1",
    )
    session.add(e)
    session.flush()
    return e


@pytest.fixture
def servicio():
    """Servicio con config dummy — solo ejercemos el enriquecimiento."""
    with tempfile.NamedTemporaryFile(suffix=".pfx", delete=False) as f:
        dummy_pfx = Path(f.name)
    config = EmisorConfig(
        rut="77829149-5",
        razon_social="GRUPO TRESTRES SPA",
        giro="SERVICIOS",
        acteco=700200,
        direccion="AV PROVIDENCIA 123",
        comuna="PROVIDENCIA",
        ciudad="SANTIAGO",
        fecha_resolucion="2020-01-01",
        numero_resolucion=0,
        cert_path=str(dummy_pfx),
    )
    try:
        yield ServicioEmisionDTE(config=config)
    finally:
        dummy_pfx.unlink(missing_ok=True)


def _xml_dte_con_items(
    tipo_dte: int, folio: int, items: list[dict],
) -> str:
    """Genera un XML EnvioDTE mínimo pero estructuralmente válido.

    Replica la estructura que ``generar_xml.py`` produce: EnvioDTE > SetDTE
    > DTE > Documento > Detalle×N.
    """
    detalles = []
    for i, it in enumerate(items, start=1):
        parts = [f'<NroLinDet>{i}</NroLinDet>', f'<NmbItem>{it["nombre"]}</NmbItem>']
        if it.get("cantidad") is not None:
            parts.append(f'<QtyItem>{it["cantidad"]}</QtyItem>')
        if it.get("precio_unitario") is not None:
            parts.append(f'<PrcItem>{it["precio_unitario"]}</PrcItem>')
        if it.get("descuento_pct") is not None:
            parts.append(f'<DescuentoPct>{it["descuento_pct"]}</DescuentoPct>')
        parts.append(f'<MontoItem>{it.get("monto_item", 0)}</MontoItem>')
        detalles.append(f'<Detalle>{"".join(parts)}</Detalle>')

    documento = (
        f'<Documento ID="F{folio}T{tipo_dte}">'
        f'<Encabezado><IdDoc><TipoDTE>{tipo_dte}</TipoDTE>'
        f'<Folio>{folio}</Folio></IdDoc></Encabezado>'
        f'{"".join(detalles)}'
        f'</Documento>'
    )
    return (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<EnvioDTE xmlns="http://www.sii.cl/SiiDte" version="1.0">'
        '<SetDTE ID="SetDoc">'
        '<Caratula version="1.0"/>'
        f'<DTE version="1.0">{documento}</DTE>'
        '</SetDTE>'
        '</EnvioDTE>'
    )


def _mk_dte_referenciado(
    session,
    empresa_id: str,
    tipo_dte: int,
    folio: int,
    items_originales: list[dict],
) -> DteEmitido:
    """Crea un DteEmitido con XML firmado conteniendo los items dados."""
    xml_str = _xml_dte_con_items(tipo_dte, folio, items_originales)
    xml_bytes = xml_str.encode("ISO-8859-1")
    xml_b64 = base64.b64encode(xml_bytes).decode("ascii")
    dte = DteEmitido(
        empresa_id=empresa_id,
        tipo_dte=tipo_dte,
        folio=folio,
        fecha_emision=date(2026, 4, 22),
        receptor_rut="60803000-K",
        receptor_razon="SII",
        monto_neto=1000,
        iva=190,
        monto_total=1190,
        xml_firmado=xml_b64,
        estado_sii="DOK",
    )
    session.add(dte)
    session.flush()
    return dte


def _req_nc_codref3(
    items: list[dict],
    tipo_ref: int = 33,
    folio_ref: int = 61,
) -> FacturaRequest:
    return FacturaRequest(
        tipo_dte=61,
        receptor_rut="77829149-5",
        receptor_razon="GRUPO TRESTRES SPA",
        receptor_giro="SERVICIOS",
        receptor_dir="AV PROVIDENCIA 123",
        receptor_comuna="PROVIDENCIA",
        items=items,
        referencias=[{
            "tipo_doc": tipo_ref,
            "folio": folio_ref,
            "razon": "Modifica monto",
            "codigo": 3,
        }],
    )


# ══════════════════════════════════════════════════════════════════
# Enriquecimiento — happy path
# ══════════════════════════════════════════════════════════════════


class TestEnriquecimientoHappyPath:
    """El core lee el DteEmitido referenciado y hereda precio/descuento."""

    def test_enriquece_precio_unitario_desde_dte_referenciado(
        self, servicio, session, empresa,
    ):
        """Items con ``precio_unitario=None`` se llenan desde el XML original."""
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=33, folio=61,
            items_originales=[
                {"nombre": "Pañuelos", "cantidad": 315, "precio_unitario": 6619,
                 "descuento_pct": 11},
                {"nombre": "Pañales", "cantidad": 546, "precio_unitario": 5667,
                 "descuento_pct": 26},
            ],
        )
        req = _req_nc_codref3([
            {"nombre": "Pañuelos", "cantidad": 315, "precio_unitario": None},
            {"nombre": "Pañales", "cantidad": 546, "precio_unitario": None},
        ], tipo_ref=33, folio_ref=61)

        servicio._enriquecer_items_codref3(req, session, empresa.id)

        assert req.items[0]["precio_unitario"] == 6619
        assert req.items[0]["descuento_pct"] == 11
        assert req.items[1]["precio_unitario"] == 5667
        assert req.items[1]["descuento_pct"] == 26

    def test_enriquece_precio_cero_tambien(self, servicio, session, empresa):
        """``precio_unitario=0`` es equivalente a None — enriquecer."""
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=33, folio=61,
            items_originales=[
                {"nombre": "X", "cantidad": 1, "precio_unitario": 1000},
            ],
        )
        req = _req_nc_codref3([
            {"nombre": "X", "cantidad": 1, "precio_unitario": 0},
        ], tipo_ref=33, folio_ref=61)
        servicio._enriquecer_items_codref3(req, session, empresa.id)
        assert req.items[0]["precio_unitario"] == 1000

    def test_hereda_descuento_pct_cuando_nc_no_lo_trae(
        self, servicio, session, empresa,
    ):
        """Si el NC no declara descuento_pct y el original lo tiene, se hereda."""
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=33, folio=61,
            items_originales=[
                {"nombre": "X", "cantidad": 1, "precio_unitario": 1000,
                 "descuento_pct": 15},
            ],
        )
        req = _req_nc_codref3([
            {"nombre": "X", "cantidad": 1, "precio_unitario": None},
        ], tipo_ref=33, folio_ref=61)
        servicio._enriquecer_items_codref3(req, session, empresa.id)
        assert req.items[0]["descuento_pct"] == 15


# ══════════════════════════════════════════════════════════════════
# Enriquecimiento — casos edge
# ══════════════════════════════════════════════════════════════════


class TestEnriquecimientoPreservaExistente:
    """El enriquecimiento NO pisa valores ya presentes en el request."""

    def test_preserva_precio_si_request_ya_lo_trae(
        self, servicio, session, empresa,
    ):
        """Si el NC viene con precio > 0, el core no lo toca."""
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=33, folio=61,
            items_originales=[
                {"nombre": "X", "cantidad": 1, "precio_unitario": 1000},
            ],
        )
        req = _req_nc_codref3([
            {"nombre": "X", "cantidad": 1, "precio_unitario": 2500},
        ], tipo_ref=33, folio_ref=61)
        servicio._enriquecer_items_codref3(req, session, empresa.id)
        # No se sobrescribe: el request es autoridad cuando el valor existe.
        assert req.items[0]["precio_unitario"] == 2500


class TestEnriquecimientoNoAplica:
    """El enriquecimiento no se activa fuera de CodRef=3."""

    def test_no_enriquece_si_codref_distinto_a_3(
        self, servicio, session, empresa,
    ):
        """CodRef=1 (anula) no dispara enriquecimiento aunque precio=0."""
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=33, folio=61,
            items_originales=[
                {"nombre": "X", "cantidad": 1, "precio_unitario": 1000},
            ],
        )
        req = FacturaRequest(
            tipo_dte=61,
            receptor_rut="77829149-5",
            receptor_razon="GRUPO TRESTRES SPA",
            receptor_giro="SERVICIOS",
            receptor_dir="AV PROVIDENCIA 123",
            receptor_comuna="PROVIDENCIA",
            items=[{"nombre": "X", "cantidad": 1, "precio_unitario": 0}],
            referencias=[{"tipo_doc": 33, "folio": 61, "codigo": 1,
                          "razon": "Anula"}],
        )
        servicio._enriquecer_items_codref3(req, session, empresa.id)
        # precio sigue en 0 — CodRef=1 no es target de este método
        assert req.items[0]["precio_unitario"] == 0

    def test_no_enriquece_si_no_es_nc_ni_nd(self, servicio, session, empresa):
        """Una Factura (tipo 33) no debería ser tocada."""
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=33, folio=61,
            items_originales=[{"nombre": "X", "cantidad": 1,
                               "precio_unitario": 1000}],
        )
        req = FacturaRequest(
            tipo_dte=33,
            receptor_rut="77829149-5",
            receptor_razon="GRUPO TRESTRES SPA",
            receptor_giro="SERVICIOS",
            receptor_dir="AV PROVIDENCIA 123",
            receptor_comuna="PROVIDENCIA",
            items=[{"nombre": "X", "cantidad": 1, "precio_unitario": 500}],
            referencias=None,
        )
        servicio._enriquecer_items_codref3(req, session, empresa.id)
        # No se toca
        assert req.items[0]["precio_unitario"] == 500


# ══════════════════════════════════════════════════════════════════
# Enriquecimiento — error paths
# ══════════════════════════════════════════════════════════════════


class TestEnriquecimientoErrores:
    """Errores claros cuando no se puede enriquecer."""

    def test_error_si_dte_referenciado_no_existe(
        self, servicio, session, empresa,
    ):
        """Sin DteEmitido en la BD con ese (tipo, folio), falla claro."""
        req = _req_nc_codref3([
            {"nombre": "X", "cantidad": 1, "precio_unitario": None},
        ], tipo_ref=33, folio_ref=999)  # Folio 999 no existe
        with pytest.raises(ValueError) as exc:
            servicio._enriquecer_items_codref3(req, session, empresa.id)
        msg = str(exc.value)
        assert "999" in msg
        assert "33" in msg  # tipo

    def test_error_si_item_no_matchea_por_nombre(
        self, servicio, session, empresa,
    ):
        """Si el item del NC no existe en el original, error de matcheo."""
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=33, folio=61,
            items_originales=[
                {"nombre": "Pañuelos", "cantidad": 1, "precio_unitario": 1000},
            ],
        )
        req = _req_nc_codref3([
            {"nombre": "Pañales", "cantidad": 1, "precio_unitario": None},
        ], tipo_ref=33, folio_ref=61)
        with pytest.raises(ValueError) as exc:
            servicio._enriquecer_items_codref3(req, session, empresa.id)
        assert "Pañales" in str(exc.value)


# ══════════════════════════════════════════════════════════════════
# Enriquecimiento de cantidad (nuevo, bug SET EXENTA 4788488-2)
# ══════════════════════════════════════════════════════════════════


class TestEnriquecimientoCantidad:
    """El SET SII tiene dos patrones duales para NC/ND CodRef=3 "MODIFICA MONTO":

    - **Patrón A**: SET declara solo CANTIDAD → NC hereda precio del original.
      (Caso BASICO 4788482-6 — ya funciona.)
    - **Patrón B**: SET declara solo VALOR UNITARIO → NC hereda cantidad del
      original. (Caso EXENTA 4788488-2 — este test lo cubre.)

    En ambos, lo que no declara el SET se hereda del DTE referenciado.
    Sin herencia de cantidad, el NC sale con ``cantidad=1`` (default) y el
    SII rechaza con "Los Valores de la Linea 1 del Detalle No Cuadran".
    """

    def test_hereda_cantidad_cuando_request_no_la_declara(
        self, servicio, session, empresa,
    ):
        """Patrón EXENTA: NC solo trae precio — cantidad None se hereda."""
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=34, folio=66,
            items_originales=[
                {"nombre": "HORAS PROGRAMADOR", "cantidad": 16,
                 "precio_unitario": 8699},
            ],
        )
        req = _req_nc_codref3(
            [{"nombre": "HORAS PROGRAMADOR", "cantidad": None,
              "precio_unitario": 1087}],
            tipo_ref=34, folio_ref=66,
        )
        servicio._enriquecer_items_codref3(req, session, empresa.id)
        assert req.items[0]["cantidad"] == 16, (
            "NC CodRef=3 sin cantidad debe heredar del original"
        )
        assert req.items[0]["precio_unitario"] == 1087, (
            "precio declarado por el SET se respeta (no se pisa)"
        )

    def test_preserva_cantidad_si_request_la_trae(
        self, servicio, session, empresa,
    ):
        """Patrón BASICO: SET declara cantidad → NC NO sobrescribe."""
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=33, folio=61,
            items_originales=[
                {"nombre": "Pañuelos", "cantidad": 857, "precio_unitario": 6619},
            ],
        )
        req = _req_nc_codref3(
            [{"nombre": "Pañuelos", "cantidad": 315, "precio_unitario": None}],
            tipo_ref=33, folio_ref=61,
        )
        servicio._enriquecer_items_codref3(req, session, empresa.id)
        assert req.items[0]["cantidad"] == 315, (
            "Si el SET declara cantidad, NO heredar del original"
        )
        assert req.items[0]["precio_unitario"] == 6619

    def test_hereda_cantidad_y_precio_a_la_vez_sobre_exenta(
        self, servicio, session, empresa,
    ):
        """Caso edge: NC viene con cantidad=None Y precio=None (parser limpio).
        Hereda ambos del original."""
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=34, folio=66,
            items_originales=[
                {"nombre": "CAPACITACION USO CIGUEÑALES", "cantidad": 1,
                 "precio_unitario": 380275},
            ],
        )
        req = _req_nc_codref3(
            [{"nombre": "CAPACITACION USO CIGUEÑALES",
              "cantidad": None, "precio_unitario": None}],
            tipo_ref=34, folio_ref=66,
        )
        servicio._enriquecer_items_codref3(req, session, empresa.id)
        assert req.items[0]["cantidad"] == 1
        assert req.items[0]["precio_unitario"] == 380275

    def test_caso_real_4788488_2_exenta_monto_correcto(
        self, servicio, session, empresa,
    ):
        """E2E del bug: caso EXENTA 4788488-2 (cert 77829149-5).

        SII rechazó con 'Los Valores de la Linea 1 del Detalle No Cuadran'.

        Factura F66: HORAS PROGRAMADOR 16 × 8699 = 139,184 (exenta).
        NC F37 emitida: cantidad=1, precio=1087 → MntExe=1087 ❌
        NC esperada:    cantidad=16, precio=1087 → MntExe=17,392 ✓
        """
        _mk_dte_referenciado(
            session, empresa.id, tipo_dte=34, folio=66,
            items_originales=[
                {"nombre": "HORAS PROGRAMADOR", "cantidad": 16,
                 "precio_unitario": 8699},
            ],
        )
        # Shape exacto de lo que viene del mapper tras el bug fix completo
        req = _req_nc_codref3(
            [{"nombre": "HORAS PROGRAMADOR", "cantidad": None,
              "precio_unitario": 1087}],
            tipo_ref=34, folio_ref=66,
        )
        servicio._enriquecer_items_codref3(req, session, empresa.id)
        # El monto que irá al XML es cantidad × precio
        assert req.items[0]["cantidad"] * req.items[0]["precio_unitario"] == 17392
