"""Tests para ``_caso_a_factura_request`` — síntesis de items para NC/ND.

Cubre el fix observado en 2026-04-22 con 77829149-5 caso 4788482-5:
el parser del SET SII NO lista ítems para las NC/ND con CodRef=1 (anula
doc completo) ni CodRef=2 (corrige texto/giro), porque el SET sólo lista
el tipo de corrección. La spec SII exige:

- **CodRef=2**: exactamente 1 ítem placeholder con ``NmbItem=razón``,
  ``cantidad=0``, ``precio_unitario=0`` (MontoItem=0).
- **CodRef=1**: replicar los ítems del documento original referenciado.
- **CodRef=3** (MODIFICA MONTO): el SET declara ítems con ``nombre`` y
  ``cantidad`` pero sin ``precio_unitario``. El mapper los deja tal cual;
  el enriquecimiento de precios ocurre en el CORE
  (``ServicioEmisionDTE._enriquecer_items_codref3``) leyendo el XML
  firmado del ``DteEmitido`` referenciado. Un solo core procesa
  documentos tanto en certificación como en producción — directriz del
  usuario: "todos los fixes deben ser globales, no parches".
  Ver tests de enriquecimiento en ``tests/test_enriquecer_codref3_core.py``.

El router ``_caso_a_factura_request`` sintetiza items para CodRef=1 y 2
cuando el parser devuelve ``items=[]``, y es pasthru puro para CodRef=3.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from datetime import date

from crumbpos.api.routers.certificacion import _caso_a_factura_request
from crumbpos.db.models import (
    Base, CertificacionCaso, CertificacionRun, DteEmitido, Empresa,
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
        giro="SERVICIOS",
        direccion="AV PROVIDENCIA 123",
        comuna="PROVIDENCIA",
        ciudad="SANTIAGO",
        cert_rut_firmante="11111111-1",
    )
    # Persistir para que tenga id — necesario para FKs de DteEmitido en
    # los helpers que simulan casos "emitido".
    session.add(e)
    session.flush()
    return e


# Fecha estable para los DteEmitido sintéticos en tests. Cualquier fecha
# pasada sirve — el mapper la usa tal cual como FchRef.
_FECHA_REF_TEST = date(2026, 4, 22)


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


def _mk_caso(
    run_id: str,
    numero: str,
    tipo_dte: int,
    datos: dict,
    folio: int | None = None,
    estado: str = "pendiente",
) -> CertificacionCaso:
    return CertificacionCaso(
        run_id=run_id,
        set_nombre="BASICO",
        numero_caso=numero,
        numero_atencion=4788482,
        tipo_dte=tipo_dte,
        datos=datos,
        folio=folio,
        estado=estado,
    )


def _mk_caso_emitido(
    session,
    empresa: Empresa,
    run_id: str,
    numero: str,
    tipo_dte: int,
    datos: dict,
    folio: int,
    fecha_emision: date = _FECHA_REF_TEST,
) -> CertificacionCaso:
    """Crea un CertificacionCaso *emitido* con su DteEmitido linkado.

    Refleja la realidad de producción: todo caso con ``estado="emitido"``
    tiene una fila ``DteEmitido`` apuntada por ``caso.dte_emitido_id``,
    y de ahí se lee la ``fecha_emision`` que pasa al FchRef de la NC/ND
    que lo referencia (ver ``_caso_a_factura_request``).
    """
    dte = DteEmitido(
        empresa_id=empresa.id,
        tipo_dte=tipo_dte,
        folio=folio,
        fecha_emision=fecha_emision,
        receptor_rut=empresa.rut,
        receptor_razon=empresa.razon_social,
        monto_total=0,
        estado_sii="pendiente",
    )
    session.add(dte)
    session.flush()
    caso = _mk_caso(run_id, numero, tipo_dte, datos, folio=folio, estado="emitido")
    caso.dte_emitido_id = dte.id
    session.add(caso)
    session.flush()
    return caso


# ══════════════════════════════════════════════════════════════════
# CodRef=2 — CORRIGE TEXTO/GIRO
# ══════════════════════════════════════════════════════════════════


class TestCodRef2CorrigeTexto:
    def test_nc_corrige_giro_sintetiza_placeholder(self, session, run, empresa):
        # Factura original ya emitida (DteEmitido linkado para resolver FchRef)
        _mk_caso_emitido(session, empresa, run.id, "4788482-1", 33, {
            "items": [{"nombre": "Item 1", "cantidad": 1, "precio_unitario": 1000}],
        }, folio=53)

        # NC CORRIGE GIRO: items=[] en el SET
        caso = _mk_caso(run.id, "4788482-5", 61, {
            "items": [],
            "referencia": {
                "caso_referido": "4788482-1",
                "tipo_doc_referido": 33,
                "razon": "CORRIGE GIRO DEL RECEPTOR",
                "cod_ref": 2,
            },
        })
        session.add(caso)
        session.flush()

        req = _caso_a_factura_request(session, caso, empresa)

        assert len(req.items) == 1
        item = req.items[0]
        assert item["nombre"] == "CORRIGE GIRO DEL RECEPTOR"
        assert item["cantidad"] == 0
        assert item["precio_unitario"] == 0
        assert item["exento"] is False
        # Referencia con folio y fecha resueltos desde DteEmitido del referido.
        assert req.referencias == [{
            "tipo_doc": 33,
            "folio": 53,
            "fecha": _FECHA_REF_TEST.isoformat(),
            "razon": "CORRIGE GIRO DEL RECEPTOR",
            "codigo": 2,
        }]


# ══════════════════════════════════════════════════════════════════
# CodRef=1 — ANULA DOC COMPLETO
# ══════════════════════════════════════════════════════════════════


class TestCodRef1Anula:
    def test_nc_anula_factura_copia_items_del_original(self, session, run, empresa):
        items_originales = [
            {"nombre": "Producto A", "cantidad": 2, "precio_unitario": 500, "exento": False},
            {"nombre": "Producto B", "cantidad": 1, "precio_unitario": 300, "exento": False},
        ]
        _mk_caso_emitido(session, empresa, run.id, "4788482-3", 33, {
            "items": items_originales,
        }, folio=55)

        caso = _mk_caso(run.id, "4788482-7", 61, {
            "items": [],
            "referencia": {
                "caso_referido": "4788482-3",
                "tipo_doc_referido": 33,
                "razon": "ANULA FACTURA",
                "cod_ref": 1,
            },
        })
        session.add(caso)
        session.flush()

        req = _caso_a_factura_request(session, caso, empresa)

        assert len(req.items) == 2
        assert req.items[0]["nombre"] == "Producto A"
        assert req.items[0]["cantidad"] == 2
        assert req.items[0]["precio_unitario"] == 500
        assert req.items[1]["nombre"] == "Producto B"

    def test_nd_anula_nc_corrige_giro_usa_placeholder_transitivo(
        self, session, run, empresa,
    ):
        """Caso 4788482-8: ND anula la NC 4788482-5 (CORRIGE GIRO).
        La NC-5 no tiene ítems propios en SET; debe inferir el placeholder."""
        _mk_caso_emitido(session, empresa, run.id, "4788482-1", 33, {
            "items": [{"nombre": "Item 1", "cantidad": 1, "precio_unitario": 1000}],
        }, folio=53)
        _mk_caso_emitido(session, empresa, run.id, "4788482-5", 61, {
            "items": [],
            "referencia": {
                "caso_referido": "4788482-1",
                "tipo_doc_referido": 33,
                "razon": "CORRIGE GIRO DEL RECEPTOR",
                "cod_ref": 2,
            },
        }, folio=57)

        # ND anula NC-5
        caso_8 = _mk_caso(run.id, "4788482-8", 56, {
            "items": [],
            "referencia": {
                "caso_referido": "4788482-5",
                "tipo_doc_referido": 61,
                "razon": "ANULA NOTA DE CREDITO ELECTRONICA",
                "cod_ref": 1,
            },
        })
        session.add(caso_8)
        session.flush()

        req = _caso_a_factura_request(session, caso_8, empresa)

        assert len(req.items) == 1
        assert req.items[0]["nombre"] == "CORRIGE GIRO DEL RECEPTOR"
        assert req.items[0]["cantidad"] == 0
        assert req.items[0]["precio_unitario"] == 0


# ══════════════════════════════════════════════════════════════════
# CodRef=3 — MODIFICA MONTO (items con cantidad, precio del caso ref)
# ══════════════════════════════════════════════════════════════════


class TestCodRef3ModificaMonto:
    """El mapper es puro para CodRef=3: pasa items tal cual vienen del SET
    (con ``precio_unitario=None``). El enriquecimiento real ocurre en el CORE
    (``ServicioEmisionDTE._enriquecer_items_codref3``) leyendo el XML firmado
    del DteEmitido referenciado. Ver: ``tests/test_enriquecer_codref3_core.py``.

    Por qué este límite:
      - El mapper NO consulta DteEmitido — mantiene el nivel como pura
        traducción de modelo-de-certificación → FacturaRequest.
      - El core es el único lugar que procesa documentos; por tanto es
        el único lugar que sabe leer el XML firmado original y heredar
        precios. Mismo mecanismo funciona en producción y certificación.
    """

    def test_mapper_pasa_cantidades_del_set_sin_tocar_precios(
        self, session, run, empresa,
    ):
        """Cantidades del SET se respetan y precios quedan None tal cual —
        el mapper no infiere precios desde el caso referenciado.
        (Sí resuelve ``numero_caso → folio`` para la referencia — eso es
        distinto al enriquecimiento de precios.)"""
        # Caso ref sólo para resolver folio en la referencia:
        _mk_caso_emitido(session, empresa, run.id, "4788482-2", 33, {
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 857, "precio_unitario": 6619},
                {"nombre": "ITEM 2 AFECTO", "cantidad": 805, "precio_unitario": 5667},
            ],
        }, folio=54)

        caso = _mk_caso(run.id, "4788482-6", 61, {
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 315, "precio_unitario": None},
                {"nombre": "ITEM 2 AFECTO", "cantidad": 546, "precio_unitario": None},
            ],
            "referencia": {
                "caso_referido": "4788482-2",
                "tipo_doc_referido": 33,
                "razon": "DEVOLUCION DE MERCADERIAS",
                "cod_ref": 3,
            },
        })
        session.add(caso)
        session.flush()

        req = _caso_a_factura_request(session, caso, empresa)

        assert len(req.items) == 2
        assert req.items[0]["nombre"] == "Pañuelo AFECTO"
        assert req.items[0]["cantidad"] == 315  # del SET, no 857
        assert req.items[1]["cantidad"] == 546  # del SET, no 805
        # Precios quedan en 0 (normalizado None→0 por el mapper) — el core
        # los enriquecerá al emitir ya que su gate es ``precio <= 0``:
        assert req.items[0]["precio_unitario"] == 0, (
            "El mapper NO debe inferir precios — eso lo hace el core"
        )
        assert req.items[1]["precio_unitario"] == 0

    def test_mapper_respeta_precios_explicitos_del_caso(
        self, session, run, empresa,
    ):
        """Si el SET (o el usuario) ya declaró precios, el mapper los pasa
        sin modificar. El core sólo enriquece items con precio<=0."""
        _mk_caso_emitido(session, empresa, run.id, "4788482-2", 33, {
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 857, "precio_unitario": 6619},
            ],
        }, folio=54)

        caso = _mk_caso(run.id, "4788482-6", 61, {
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 315, "precio_unitario": 8000},
            ],
            "referencia": {
                "caso_referido": "4788482-2",
                "tipo_doc_referido": 33,
                "razon": "DEVOLUCION DE MERCADERIAS",
                "cod_ref": 3,
            },
        })
        session.add(caso)
        session.flush()

        req = _caso_a_factura_request(session, caso, empresa)

        assert req.items[0]["precio_unitario"] == 8000  # no sobrescrito a 6619

    def test_mapper_no_infiere_precios_aunque_referido_los_tenga(
        self, session, run, empresa,
    ):
        """Guard arquitectónico: aunque el caso referenciado tenga precios
        disponibles en ``datos.items``, el mapper NO debe inferirlos. Eso
        ocurre antes solo en el mapper (cert-specific) — ahora vive en el
        core (``ServicioEmisionDTE._enriquecer_items_codref3``) que lee el
        XML firmado del DteEmitido. Un solo lugar de procesamiento."""
        _mk_caso_emitido(session, empresa, run.id, "4788482-2", 33, {
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 857,
                 "precio_unitario": 6619, "descuento_pct": 11},
            ],
        }, folio=54)

        caso = _mk_caso(run.id, "4788482-6", 61, {
            "items": [
                {"nombre": "Pañuelo AFECTO", "cantidad": 315,
                 "precio_unitario": None, "descuento_pct": None},
            ],
            "referencia": {
                "caso_referido": "4788482-2",
                "tipo_doc_referido": 33,
                "razon": "DEVOLUCION DE MERCADERIAS",
                "cod_ref": 3,
            },
        })
        session.add(caso)
        session.flush()

        req = _caso_a_factura_request(session, caso, empresa)

        # Aunque el caso referenciado tiene precio=6619 y descuento_pct=11,
        # el mapper DEBE dejar el item pasthru sin inferir nada desde el
        # caso referenciado:
        assert req.items[0]["precio_unitario"] == 0  # normalizado None→0
        assert req.items[0]["descuento_pct"] is None


# ══════════════════════════════════════════════════════════════════
# Casos de error
# ══════════════════════════════════════════════════════════════════


class TestErrores:
    def test_factura_normal_sin_items_falla(self, session, run, empresa):
        """Una factura T33 sin referencia ni items sí debe fallar."""
        from fastapi import HTTPException

        caso = _mk_caso(run.id, "4788482-99", 33, {"items": []})
        session.add(caso)
        session.flush()

        with pytest.raises(HTTPException) as exc:
            _caso_a_factura_request(session, caso, empresa)
        assert exc.value.status_code == 422
        assert "no tiene items" in exc.value.detail

    def test_referencia_a_caso_no_emitido_falla(self, session, run, empresa):
        from fastapi import HTTPException

        caso_ref = _mk_caso(run.id, "4788482-1", 33, {
            "items": [{"nombre": "Foo", "cantidad": 1, "precio_unitario": 100}],
        })  # sin folio → no emitido
        session.add(caso_ref)

        caso = _mk_caso(run.id, "4788482-5", 61, {
            "items": [],
            "referencia": {
                "caso_referido": "4788482-1",
                "tipo_doc_referido": 33,
                "razon": "CORRIGE GIRO",
                "cod_ref": 2,
            },
        })
        session.add(caso)
        session.flush()

        with pytest.raises(HTTPException) as exc:
            _caso_a_factura_request(session, caso, empresa)
        assert exc.value.status_code == 422
        assert "todavía no se emitió" in exc.value.detail


# ══════════════════════════════════════════════════════════════════
# Guía de Despacho — compatibilidad IndTraslado / TipoDespacho
#
# Regla SII: TipoDespacho solo aplica con IndTraslado=1 (venta) o
# IndTraslado=3 (consignación). Si caso.datos tiene un TipoDespacho
# stale para traslado interno (IndTraslado=5) — por ejemplo de un
# preview generado antes del fix en simulacion.generador — el router
# debe limpiarlo antes de construir FacturaRequest. Si no, el
# validador del core rechaza la emisión.
# ══════════════════════════════════════════════════════════════════


class TestGuiaDespachoNormalizacionDefensiva:
    def test_traslado_interno_descarta_tipo_despacho_stale(
        self, session, run, empresa,
    ):
        """IndTraslado=5 + TipoDespacho=1 stale → request sin TipoDespacho."""
        caso = _mk_caso(run.id, "SIM-14", 52, {
            "items": [
                {"nombre": "Pan", "cantidad": 10, "precio_unitario": 0},
                {"nombre": "Galleta", "cantidad": 5, "precio_unitario": 0},
            ],
            # Datos stale persistidos antes del fix del generador.
            "tipo_despacho": 1,
            "ind_traslado": 5,
            "slot": 14,
        })
        session.add(caso)
        session.flush()

        req = _caso_a_factura_request(session, caso, empresa)

        assert req.ind_traslado == 5
        assert req.tipo_despacho is None, (
            "Router debe descartar TipoDespacho cuando IndTraslado=5 "
            "(traslado interno) para no gatillar el validador del core."
        )

    def test_venta_conserva_tipo_despacho(self, session, run, empresa):
        """IndTraslado=1 (venta) + TipoDespacho=2 → se pasa tal cual."""
        caso = _mk_caso(run.id, "SIM-13", 52, {
            "items": [
                {"nombre": "Pan", "cantidad": 10, "precio_unitario": 1500},
            ],
            "tipo_despacho": 2,
            "ind_traslado": 1,
            "slot": 13,
        })
        session.add(caso)
        session.flush()

        req = _caso_a_factura_request(session, caso, empresa)

        assert req.ind_traslado == 1
        assert req.tipo_despacho == 2

    def test_consignacion_conserva_tipo_despacho(self, session, run, empresa):
        """IndTraslado=3 (consignación) + TipoDespacho=3 → se pasa tal cual."""
        caso = _mk_caso(run.id, "GUIA-X", 52, {
            "items": [{"nombre": "X", "cantidad": 1, "precio_unitario": 100}],
            "tipo_despacho": 3,
            "ind_traslado": 3,
        })
        session.add(caso)
        session.flush()

        req = _caso_a_factura_request(session, caso, empresa)

        assert req.ind_traslado == 3
        assert req.tipo_despacho == 3

    def test_otros_traslados_descarta_tipo_despacho(
        self, session, run, empresa,
    ):
        """IndTraslado=6 (otros) + TipoDespacho → descartar TipoDespacho."""
        caso = _mk_caso(run.id, "GUIA-Y", 52, {
            "items": [{"nombre": "X", "cantidad": 1, "precio_unitario": 100}],
            "tipo_despacho": 1,
            "ind_traslado": 6,
        })
        session.add(caso)
        session.flush()

        req = _caso_a_factura_request(session, caso, empresa)

        assert req.ind_traslado == 6
        assert req.tipo_despacho is None

    def test_sin_ind_traslado_no_toca_tipo_despacho(
        self, session, run, empresa,
    ):
        """DTE no-T52 (no hay ind_traslado) → no se aplica la normalización."""
        caso = _mk_caso(run.id, "BASICO-1", 33, {
            "items": [{"nombre": "X", "cantidad": 1, "precio_unitario": 1000}],
            # No es T52, pero igual verificamos que no rompa.
        })
        session.add(caso)
        session.flush()

        req = _caso_a_factura_request(session, caso, empresa)

        assert req.ind_traslado is None
        assert req.tipo_despacho is None
