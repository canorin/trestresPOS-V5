"""Guard pre-emisión: límite de 20 líneas de detalle para DTEs carta.

El diseño carta (215.9×279.4mm) fija el timbre PDF417 a y=223.4mm.
Con encabezado hasta y≈97mm y totales+son_pesos = 22mm, la tabla de
detalle dispone de 96mm útiles. A 5mm por fila caben 19 filas antes de
chocar con el área del timbre. Se establece 20 como límite redondo.

Aplica a: T33 T34 T52 T56 T61 (todos usan PDFCarta / formato carta).
NO aplica a: T39 T41 (boletas — formato térmico dinámico sin límite).

Regla SII: el XML permite hasta 60 líneas; nuestro límite físico es 20.
El guard actúa ANTES de consumir folio (en _validar_request).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from crumbpos.api.services.emision_dte import (
    EmisorConfig,
    FacturaRequest,
    ServicioEmisionDTE,
)


# ══════════════════════════════════════════════════════════════════
# Fixture — servicio dummy para validar sin firma ni red
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def servicio():
    """``ServicioEmisionDTE`` mínimo — solo para usar ``_validar_request``."""
    with tempfile.NamedTemporaryFile(suffix=".pfx", delete=False) as f:
        dummy_pfx = Path(f.name)
    config = EmisorConfig(
        rut="77051056-2",
        razon_social="TRESTRES PUBLICIDAD SPA",
        giro="SERVICIOS DE PUBLICIDAD",
        acteco=700200,
        direccion="AV PROVIDENCIA 123",
        comuna="PROVIDENCIA",
        ciudad="SANTIAGO",
        fecha_resolucion="2014-08-22",
        numero_resolucion=80,
        cert_path=str(dummy_pfx),
        ambiente="certificacion",
    )
    try:
        yield ServicioEmisionDTE(config=config)
    finally:
        dummy_pfx.unlink(missing_ok=True)


def _items(n: int) -> list[dict]:
    """Genera ``n`` ítems distintos válidos."""
    return [
        {"nombre": f"Producto {i}", "cantidad": 1, "precio_unitario": 1000}
        for i in range(1, n + 1)
    ]


def _req_carta(tipo_dte: int, n_items: int, **kwargs) -> FacturaRequest:
    """``FacturaRequest`` mínimo válido para DTEs carta (no boleta)."""
    base = dict(
        tipo_dte=tipo_dte,
        receptor_rut="76354771-K",
        receptor_razon="EMPRESA RECEPTORA SPA",
        receptor_giro="COMERCIO",
        receptor_dir="AV ALAMEDA 100",
        receptor_comuna="SANTIAGO",
        items=_items(n_items),
    )
    # T52 Guía de Despacho requiere IndTraslado + TipoDespacho cuando es venta
    if tipo_dte == 52:
        base["ind_traslado"] = 1   # 1=operación constituye venta
        base["tipo_despacho"] = 1  # 1=por cuenta del receptor
    # NC/ND requieren al menos una referencia DTE para no fallar por esa validación
    if tipo_dte in (56, 61):
        base["referencias"] = [{
            "tipo_doc": 33, "folio": 10, "fecha": "2026-01-15",
            "razon": "Test", "codigo": 3,
        }]
    base.update(kwargs)
    return FacturaRequest(**base)


LIMITE = ServicioEmisionDTE.MAX_ITEMS_FACTURA_CARTA  # 20


# ══════════════════════════════════════════════════════════════════
# Constante pública
# ══════════════════════════════════════════════════════════════════


def test_constante_es_20():
    """MAX_ITEMS_FACTURA_CARTA debe ser exactamente 20."""
    assert ServicioEmisionDTE.MAX_ITEMS_FACTURA_CARTA == 20


# ══════════════════════════════════════════════════════════════════
# DTEs carta: T33 / T34 / T52 / T56 / T61 — el límite aplica
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("tipo_dte", [33, 34, 52, 56, 61])
class TestLimiteDTEsCarta:
    """Todos los tipos carta rechazan más de 20 ítems."""

    def test_rechaza_21_items(self, servicio, tipo_dte):
        req = _req_carta(tipo_dte, LIMITE + 1)
        error = servicio._validar_request(req)
        assert error is not None, (
            f"T{tipo_dte} con {LIMITE + 1} ítems debe ser rechazado."
        )

    def test_rechaza_60_items_limite_xml_sii(self, servicio, tipo_dte):
        """60 ítems (límite del XML SII) igual rechazado por impresión."""
        req = _req_carta(tipo_dte, 60)
        error = servicio._validar_request(req)
        assert error is not None, (
            f"T{tipo_dte} con 60 ítems debe ser rechazado por límite carta."
        )

    def test_acepta_exactamente_20_items(self, servicio, tipo_dte):
        req = _req_carta(tipo_dte, LIMITE)
        error = servicio._validar_request(req)
        assert error is None, (
            f"T{tipo_dte} con {LIMITE} ítems (límite exacto) debe pasar. "
            f"Error: {error}"
        )

    def test_acepta_1_item(self, servicio, tipo_dte):
        req = _req_carta(tipo_dte, 1)
        error = servicio._validar_request(req)
        assert error is None, (
            f"T{tipo_dte} con 1 ítem debe pasar. Error: {error}"
        )

    def test_mensaje_menciona_cantidad_recibida(self, servicio, tipo_dte):
        n = LIMITE + 7
        req = _req_carta(tipo_dte, n)
        error = servicio._validar_request(req)
        assert error is not None
        assert str(n) in error, (
            f"T{tipo_dte}: el error debe mencionar la cantidad recibida ({n}). "
            f"Error: {error}"
        )

    def test_mensaje_menciona_limite(self, servicio, tipo_dte):
        req = _req_carta(tipo_dte, LIMITE + 1)
        error = servicio._validar_request(req)
        assert error is not None
        assert str(LIMITE) in error, (
            f"T{tipo_dte}: el error debe mencionar el límite ({LIMITE}). "
            f"Error: {error}"
        )


# ══════════════════════════════════════════════════════════════════
# Boletas T39 / T41 — el límite NO aplica
# ══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("tipo_dte", [39, 41])
class TestBoletas_SinLimite:
    """Boletas (T39/T41) usan formato térmico — no tienen el límite carta."""

    def test_acepta_mas_de_20_items(self, servicio, tipo_dte):
        """Una boleta con 30 ítems no debe fallar por el límite carta."""
        req = FacturaRequest(
            tipo_dte=tipo_dte,
            receptor_rut="66666666-6",
            receptor_razon="CONSUMIDOR FINAL",
            receptor_giro="",
            receptor_dir="",
            receptor_comuna="",
            items=_items(LIMITE + 10),
        )
        error = servicio._validar_request(req)
        # Si hay error debe ser por otra razón, no por el límite de ítems carta.
        if error:
            assert "líneas de detalle" not in error, (
                f"Boleta T{tipo_dte} no debe fallar por límite carta. "
                f"Error recibido: {error}"
            )
            assert str(LIMITE) not in error or "máximo" not in error, (
                f"Boleta T{tipo_dte} no debe mencionar el límite de 20 ítems. "
                f"Error recibido: {error}"
            )
