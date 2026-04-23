"""Guard core para NC/ND con CodRef=3 (MODIFICA MONTO).

El SII rechaza con REF-2-768 "Modificacion de montos debe tener monto mayor
a cero" cuando una NC (tipo 61) o ND (tipo 56) con CodRef=3 llega con
MontoTotal=0. Este guard ataca la raíz: si TODOS los ítems del documento
traen ``precio_unitario <= 0``, el MontoTotal será 0 y el SII rebotará.
Rechazar antes de quemar folio.

Análogo a los guards existentes para:
  - CodRef=1 (ANULA) — ``_validar_request`` exige ítems presentes (líneas
    324-330).
  - CodRef=2 (CORRIGE TEXTO) — exige 1 ítem placeholder con montos en 0
    (líneas 300-322).

Contexto: bug detectado 2026-04-22 en certificación 77829149-5 caso
4788482-6 F32. El parser del SET SII declara ítems de CodRef=3 sin
precio; ``_caso_a_factura_request`` del wizard los enriquece desde el caso
referenciado. Este guard es la red de seguridad final por si el
enriquecimiento falla O por si en producción un caller manda una NC
CodRef=3 con ítems mal construidos. Directriz del usuario:
"todos los fixes deben ser globales, no parches".
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
# Fixtures — servicio mínimo para validar (no emite, no firma)
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def servicio():
    """``ServicioEmisionDTE`` con config dummy — solo para usar
    ``_validar_request``. El método es pura lógica, no toca firma/BD/red."""
    with tempfile.NamedTemporaryFile(suffix=".pfx", delete=False) as f:
        dummy_pfx = Path(f.name)
    config = EmisorConfig(
        rut="77829149-5",
        razon_social="GRUPO TRESTRES SPA",
        giro="SERVICIOS DE PUBLICIDAD",
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


def _req_nc_codref3(items: list[dict]) -> FacturaRequest:
    """NC (tipo 61) CodRef=3 con ref a una factura T33. Solo varían items."""
    return FacturaRequest(
        tipo_dte=61,
        receptor_rut="77829149-5",
        receptor_razon="GRUPO TRESTRES SPA",
        receptor_giro="SERVICIOS DE PUBLICIDAD",
        receptor_dir="AV PROVIDENCIA 123",
        receptor_comuna="PROVIDENCIA",
        items=items,
        referencias=[{
            "tipo_doc": 33,
            "folio": 61,
            "razon": "Modifica monto",
            "codigo": 3,
        }],
    )


# ══════════════════════════════════════════════════════════════════
# Guard CodRef=3: todos los ítems con precio 0 → rechazo pre-envío
# ══════════════════════════════════════════════════════════════════


class TestGuardCodRef3MontoCero:
    """El core debe rechazar antes del SII si ``MontoTotal`` saldría en 0."""

    def test_rechaza_todos_items_precio_cero(self, servicio):
        """Caso del bug: items {nombre, cantidad} con ``precio_unitario=0``
        dan MontoTotal=0 y el SII rebota REF-2-768."""
        req = _req_nc_codref3([
            {"nombre": "Pañuelos", "cantidad": 315, "precio_unitario": 0},
            {"nombre": "Pañales", "cantidad": 546, "precio_unitario": 0},
        ])
        error = servicio._validar_request(req)
        assert error is not None, (
            "Debería rechazar NC CodRef=3 con todos los items en precio=0."
        )
        assert "CodRef=3" in error
        assert "REF-2-768" in error
        assert "precio" in error.lower()

    def test_rechaza_todos_items_precio_none(self, servicio):
        """``precio_unitario=None`` es equivalente a 0 para esta validación."""
        req = _req_nc_codref3([
            {"nombre": "Pañuelos", "cantidad": 315, "precio_unitario": None},
        ])
        error = servicio._validar_request(req)
        assert error is not None
        assert "REF-2-768" in error

    def test_acepta_items_con_precio_mayor_cero(self, servicio):
        """Caso válido: items con precio real → MontoTotal > 0, SII no rebota."""
        req = _req_nc_codref3([
            {"nombre": "Pañuelos", "cantidad": 315, "precio_unitario": 6619,
             "descuento_pct": 11},
            {"nombre": "Pañales", "cantidad": 546, "precio_unitario": 5667,
             "descuento_pct": 26},
        ])
        error = servicio._validar_request(req)
        assert error is None, (
            f"NC CodRef=3 con precios > 0 debe pasar, error: {error}"
        )

    def test_acepta_cuando_al_menos_un_item_tiene_precio(self, servicio):
        """Si UN item tiene precio > 0, MontoTotal > 0 y la NC es válida.
        (Un item con precio=0 podría ser legítimo si el resto compensa.)"""
        req = _req_nc_codref3([
            {"nombre": "Item con ajuste", "cantidad": 1, "precio_unitario": 1000},
            {"nombre": "Item sin ajuste", "cantidad": 1, "precio_unitario": 0},
        ])
        error = servicio._validar_request(req)
        assert error is None, (
            f"NC CodRef=3 con al menos un item con precio > 0 debe pasar, "
            f"error: {error}"
        )


# ══════════════════════════════════════════════════════════════════
# Simetría: ND (tipo 56) CodRef=3 también debe estar protegida
# ══════════════════════════════════════════════════════════════════


class TestGuardCodRef3ParaND:
    """Las ND (tipo 56) también usan CodRef=3 y reciben el mismo rechazo SII."""

    def test_rechaza_nd_codref3_precio_cero(self, servicio):
        """El guard aplica por igual a tipo 56 y 61 — mismo error SII."""
        req = FacturaRequest(
            tipo_dte=56,  # Nota de Débito
            receptor_rut="77829149-5",
            receptor_razon="GRUPO TRESTRES SPA",
            receptor_giro="SERVICIOS",
            receptor_dir="AV PROVIDENCIA 123",
            receptor_comuna="PROVIDENCIA",
            items=[{"nombre": "X", "cantidad": 1, "precio_unitario": 0}],
            referencias=[{
                "tipo_doc": 33, "folio": 61, "codigo": 3,
                "razon": "Aumenta monto",
            }],
        )
        error = servicio._validar_request(req)
        assert error is not None
        assert "Nota de Débito" in error or "REF-2-768" in error


# ══════════════════════════════════════════════════════════════════
# No-regresión: guards CodRef=1 y CodRef=2 siguen funcionando igual
# ══════════════════════════════════════════════════════════════════


class TestNoRegresionCodRef1y2:
    """El guard nuevo no debe interferir con los ya existentes."""

    def test_codref2_sin_montos_sigue_pasando(self, servicio):
        """CodRef=2 (corrige texto) con ítem placeholder válido — pasa."""
        req = FacturaRequest(
            tipo_dte=61,
            receptor_rut="77829149-5",
            receptor_razon="GRUPO TRESTRES SPA",
            receptor_giro="SERVICIOS",
            receptor_dir="AV PROVIDENCIA 123",
            receptor_comuna="PROVIDENCIA",
            items=[{
                "nombre": "CORRIGE GIRO RECEPTOR",
                "cantidad": 0,
                "precio_unitario": 0,
            }],
            referencias=[{
                "tipo_doc": 33, "folio": 61, "codigo": 2,
                "razon": "Corrige texto",
            }],
        )
        error = servicio._validar_request(req)
        assert error is None, (
            f"CodRef=2 con placeholder correcto debe pasar; el guard CodRef=3 "
            f"no debería afectarlo. Error: {error}"
        )

    def test_codref1_con_items_sigue_pasando(self, servicio):
        """CodRef=1 (anula) con ítems replicados del original — pasa."""
        req = FacturaRequest(
            tipo_dte=61,
            receptor_rut="77829149-5",
            receptor_razon="GRUPO TRESTRES SPA",
            receptor_giro="SERVICIOS",
            receptor_dir="AV PROVIDENCIA 123",
            receptor_comuna="PROVIDENCIA",
            items=[{"nombre": "Item orig", "cantidad": 1, "precio_unitario": 1000}],
            referencias=[{
                "tipo_doc": 33, "folio": 61, "codigo": 1,
                "razon": "Anula documento",
            }],
        )
        error = servicio._validar_request(req)
        assert error is None, (
            f"CodRef=1 con items válidos debe pasar; error: {error}"
        )
