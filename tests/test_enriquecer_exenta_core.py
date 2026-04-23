"""Enriquecimiento core: NC/ND que referencia a T34 → items a ``exento=True``.

Una NC/ND que corrige o modifica una Factura Exenta (T34) debe tener TODOS
sus ítems marcados como exentos (``IndExe=1`` en el XML SII). Si no, el SII
rechaza porque un DTE exento no puede tener ítems afectos y viceversa.

El validador del core ya lo exigía (``_validar_request`` levantaba error
"todos los ítems deben marcarse como exentos"), pero el SET del SII declara
los ítems sin la marca ``exento``. El mapper del wizard tampoco la propaga.
Resultado: el usuario ve ERROR al emitir aunque el caso es totalmente válido.

Fix: enriquecer en el core (patrón idéntico al CodRef=3). Si la referencia
apunta a un T34 y no es CodRef=2 (CORRIGE TEXTO, que usa placeholder con
montos en 0), marcar todos los ítems como ``exento=True`` automáticamente.

**Único core**: este enriquecimiento corre en ``ServicioEmisionDTE`` y por
tanto aplica a certificación y producción por igual. Directriz del usuario:
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


@pytest.fixture
def servicio():
    """``ServicioEmisionDTE`` con config dummy para ejercitar el método
    de enriquecimiento. No firma, no emite, no toca BD."""
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


def _req_nc_a_exenta(codigo_ref: int, items: list[dict]) -> FacturaRequest:
    """NC (tipo 61) que referencia una Factura Exenta (T34)."""
    return FacturaRequest(
        tipo_dte=61,
        receptor_rut="77829149-5",
        receptor_razon="GRUPO TRESTRES SPA",
        receptor_giro="SERVICIOS",
        receptor_dir="AV PROVIDENCIA 123",
        receptor_comuna="PROVIDENCIA",
        items=items,
        referencias=[{
            "tipo_doc": 34,
            "folio": 66,
            "razon": "Corrección sobre Factura Exenta",
            "codigo": codigo_ref,
        }],
    )


# ══════════════════════════════════════════════════════════════════
# NC/ND que referencia T34 → items se marcan exento=True automáticamente
# ══════════════════════════════════════════════════════════════════


class TestEnriquecimientoNCRefExenta:
    """El core marca items como exentos cuando la referencia apunta a T34."""

    def test_nc_codref3_a_exenta_marca_items_exento(self, servicio):
        """NC CodRef=3 (modifica monto) sobre T34 → todos los items exentos."""
        req = _req_nc_a_exenta(3, [
            {"nombre": "HORAS PROGRAMADOR", "cantidad": 8,
             "precio_unitario": 619, "exento": False},
        ])
        servicio._enriquecer_items_referencia_a_exenta(req)
        assert req.items[0]["exento"] is True, (
            "Item de NC sobre T34 debe quedar exento=True tras enriquecer"
        )

    def test_nc_codref1_a_exenta_marca_items_exento(self, servicio):
        """NC CodRef=1 (anula) sobre T34 → items exentos automático."""
        req = _req_nc_a_exenta(1, [
            {"nombre": "HORAS PROGRAMADOR", "cantidad": 8,
             "precio_unitario": 619},  # sin exento declarado
        ])
        servicio._enriquecer_items_referencia_a_exenta(req)
        assert req.items[0]["exento"] is True

    def test_nc_multiples_items_todos_quedan_exentos(self, servicio):
        """Con varios items, cada uno queda marcado exento."""
        req = _req_nc_a_exenta(3, [
            {"nombre": "ITEM 1", "cantidad": 1, "precio_unitario": 1000},
            {"nombre": "ITEM 2", "cantidad": 2, "precio_unitario": 500, "exento": False},
            {"nombre": "ITEM 3", "cantidad": 1, "precio_unitario": 200, "exento": True},
        ])
        servicio._enriquecer_items_referencia_a_exenta(req)
        assert all(it["exento"] is True for it in req.items)

    def test_nd_tipo_56_a_exenta_tambien_se_enriquece(self, servicio):
        """ND (tipo 56) sobre T34 sigue la misma regla."""
        req = FacturaRequest(
            tipo_dte=56,
            receptor_rut="77829149-5",
            receptor_razon="GRUPO TRESTRES SPA",
            receptor_giro="SERVICIOS",
            receptor_dir="AV PROVIDENCIA 123",
            receptor_comuna="PROVIDENCIA",
            items=[{"nombre": "ITEM", "cantidad": 1, "precio_unitario": 1000}],
            referencias=[{
                "tipo_doc": 34, "folio": 66, "codigo": 1,
                "razon": "Aumenta monto sobre exenta",
            }],
        )
        servicio._enriquecer_items_referencia_a_exenta(req)
        assert req.items[0]["exento"] is True


class TestEnriquecimientoNoAplica:
    """Casos donde NO se debe enriquecer — preserva la intención del caller."""

    def test_codref2_corrige_texto_no_enriquece(self, servicio):
        """CodRef=2 usa placeholder (cantidad=0, precio=0) — dejar como está.
        El validador del core tampoco exige exento en este caso."""
        req = _req_nc_a_exenta(2, [
            {"nombre": "CORRIGE TEXTO", "cantidad": 0,
             "precio_unitario": 0, "exento": False},
        ])
        servicio._enriquecer_items_referencia_a_exenta(req)
        assert req.items[0]["exento"] is False, (
            "CodRef=2 (corrige texto) no debe modificarse — placeholder"
        )

    def test_nc_a_t33_afecta_no_enriquece(self, servicio):
        """NC a T33 (afecta) → items no deben quedar exentos."""
        req = FacturaRequest(
            tipo_dte=61,
            receptor_rut="77829149-5",
            receptor_razon="GRUPO TRESTRES SPA",
            receptor_giro="SERVICIOS",
            receptor_dir="AV PROVIDENCIA 123",
            receptor_comuna="PROVIDENCIA",
            items=[{"nombre": "ITEM AFECTO", "cantidad": 1,
                    "precio_unitario": 1000, "exento": False}],
            referencias=[{
                "tipo_doc": 33, "folio": 61, "codigo": 3,
                "razon": "Modifica monto sobre afecta",
            }],
        )
        servicio._enriquecer_items_referencia_a_exenta(req)
        assert req.items[0]["exento"] is False, (
            "Referencia a T33 no debe marcar items como exentos"
        )

    def test_factura_normal_sin_referencias_no_enriquece(self, servicio):
        """Factura normal sin ninguna referencia — no aplica."""
        req = FacturaRequest(
            tipo_dte=33,
            receptor_rut="77829149-5",
            receptor_razon="GRUPO TRESTRES SPA",
            receptor_giro="SERVICIOS",
            receptor_dir="AV PROVIDENCIA 123",
            receptor_comuna="PROVIDENCIA",
            items=[{"nombre": "ITEM", "cantidad": 1, "precio_unitario": 1000}],
        )
        servicio._enriquecer_items_referencia_a_exenta(req)
        # No debe haber tocado nada (no aseveramos exento — solo no crashea)
        assert req.items[0].get("exento") in (None, False)


# ══════════════════════════════════════════════════════════════════
# E2E: el request real del bug cert 77829149-5 caso 4788488-2
# ══════════════════════════════════════════════════════════════════


class TestE2EBugCertificacion4788488:
    """Reproducción del caso real que bloqueó la cert EXENTA del 2026-04-22."""

    def test_caso_4788488_2_se_emite_tras_enriquecimiento(self, servicio):
        """NC sobre F66 (Factura Exenta) — item 'HORAS PROGRAMADOR' sin
        marca exento. Tras enriquecer, el validador del core debe aceptarlo."""
        req = FacturaRequest(
            tipo_dte=61,
            receptor_rut="77829149-5",
            receptor_razon="GRUPO TRESTRES SPA",
            receptor_giro="SERVICIOS",
            receptor_dir="AV PROVIDENCIA 123",
            receptor_comuna="PROVIDENCIA",
            items=[{"nombre": "HORAS PROGRAMADOR", "cantidad": 8,
                    "precio_unitario": 619}],
            referencias=[{
                "tipo_doc": 34, "folio": 66, "codigo": 3,
                "razon": "Modifica monto de factura exenta",
            }],
        )
        # Antes del enriquecimiento, el validador rechaza:
        error_pre = servicio._validar_request(req)
        assert error_pre is not None and "exento" in error_pre.lower(), (
            f"Previo a enriquecer, debe fallar por exento; error: {error_pre}"
        )
        # Enriquecer, luego validar de nuevo: debe pasar.
        servicio._enriquecer_items_referencia_a_exenta(req)
        error_post = servicio._validar_request(req)
        assert error_post is None, (
            f"Tras enriquecer, no debe haber error. error_post={error_post}"
        )
