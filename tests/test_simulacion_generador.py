"""Tests de ``crumbpos.certificacion.simulacion.generador``.

Actualizado 2026-04-23 tras la reescritura completa del generador:

- Esquema FIJO de 16 DTEs (9 T33 + 3 T34 + 2 T52 + 1 T61 + 1 T56),
  réplica exacta del set aprobado a TRESTRES PUBLICIDAD SPA.
- Sin aleatoriedad, sin seed: el output es determinístico por construcción.
- Cada slot tiene su patrón operativo (descuentos, servicios, mixto,
  traslado interno, referencias).
- Items por slot vienen de la config — el generador solo aplica patrones
  y arma el dict final.

Invariantes que se testean:

1. Cantidades: el output tiene exactamente 16 DTEs en orden de slot.
2. Tipos por slot: siguen ``ORDEN_TIPOS``.
3. Folios asignados en el orden en que se reciben por tipo.
4. ``numero_caso`` = ``SIM-NN`` (2 dígitos, 1..16).
5. Patrones específicos:
   - Slot 3 → ``descuento_pct=10`` en todos los items.
   - Slot 5 → ``descuento_global`` = DSC_GLOBAL_SLOT_5.
   - Slot 6 → ``ind_servicio=3``.
   - Slot 7 → item #2 marcado ``exento=True``.
   - Slot 13 → ``tipo_despacho=2``, ``ind_traslado=1``.
   - Slot 14 → ``tipo_despacho=None``, ``ind_traslado=5``, precio 0 forzado.
     (TipoDespacho no va con traslado interno — regla SII.)
   - Slot 15 → referencia al slot 9 (numero_caso SIM-09), cod_ref=3.
   - Slot 16 → referencia al slot 3 (numero_caso SIM-03), cod_ref=3.
6. Razones custom de slots 15/16 se propagan al dict de referencia.
7. Folios insuficientes levanta ``ValueError``.
8. Slots faltantes levantan ``ValueError``.
"""
from __future__ import annotations

import pytest

from crumbpos.certificacion.simulacion.generador import (
    CANTIDAD_POR_TIPO,
    COD_REF_AJUSTE,
    DESCUENTO_PCT_SLOT_3,
    DSC_GLOBAL_SLOT_5,
    IND_SERVICIO_SLOT_6,
    IND_TRASLADO_INTERNO,
    IND_TRASLADO_VENTA,
    ORDEN_TIPOS,
    SLOT_SPECS,
    TIPO_DESPACHO_VENTA,
    TOTAL_SLOTS,
    armar_dtes_simulacion,
)


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════


def _items_validos(n: int, precio: int = 10_000) -> list[dict]:
    """Genera ``n`` items simples con cantidad 1 y precio dado."""
    return [
        {"nombre": f"Producto {i + 1}", "cantidad": 1, "precio": precio}
        for i in range(n)
    ]


def _slot(num: int, items: list[dict], razon: str | None = None) -> dict:
    out: dict = {"slot": num, "items": items}
    if razon is not None:
        out["razon"] = razon
    return out


def _config_valida() -> dict:
    """Config base con los 16 slots poblados con items mínimos válidos."""
    slots = [
        _slot(1, _items_validos(2)),
        _slot(2, _items_validos(3)),
        _slot(3, _items_validos(2)),
        _slot(4, _items_validos(2)),
        _slot(5, _items_validos(2)),
        _slot(6, _items_validos(1)),
        _slot(7, _items_validos(2)),
        _slot(8, _items_validos(1)),
        _slot(9, _items_validos(2)),
        _slot(10, _items_validos(2)),
        _slot(11, _items_validos(1)),
        _slot(12, _items_validos(1)),
        _slot(13, _items_validos(2)),
        _slot(14, [
            {"nombre": "Produccion bodega", "cantidad": 10, "precio": 0},
            {"nombre": "Traslado sucursal", "cantidad": 5, "precio": 0},
        ]),
        _slot(15, _items_validos(1), razon="Devolucion parcial"),
        _slot(16, _items_validos(1), razon="Cobro interes mora"),
    ]
    return {"slots": slots, "receptor_rut": ""}


def _folios_validos(base: int = 100) -> dict[int, list[int]]:
    """Asigna folios contiguos por tipo, distintos entre tipos."""
    out: dict[int, list[int]] = {}
    cursor = base
    for tipo, cantidad in CANTIDAD_POR_TIPO.items():
        out[tipo] = list(range(cursor, cursor + cantidad))
        cursor += 100
    return out


# ══════════════════════════════════════════════════════════════════
# Layout: cantidades y orden
# ══════════════════════════════════════════════════════════════════


def test_total_slots_es_16():
    assert TOTAL_SLOTS == 16
    assert sum(CANTIDAD_POR_TIPO.values()) == 16


def test_armar_produce_16_dtes():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    assert len(dtes) == TOTAL_SLOTS


def test_dtes_en_orden_de_slot():
    """El output viene ordenado por slot ascendente (1..16)."""
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    slots = [d["slot"] for d in dtes]
    assert slots == list(range(1, TOTAL_SLOTS + 1))


def test_tipos_siguen_orden_tipos():
    """Slot N siempre trae ``ORDEN_TIPOS[N-1]`` como tipo DTE."""
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    tipos = [d["tipo_dte"] for d in dtes]
    assert tipos == list(ORDEN_TIPOS)


def test_cantidades_por_tipo():
    """9 T33 + 3 T34 + 2 T52 + 1 T61 + 1 T56."""
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    contador: dict[int, int] = {}
    for d in dtes:
        contador[d["tipo_dte"]] = contador.get(d["tipo_dte"], 0) + 1
    assert contador == {33: 9, 34: 3, 52: 2, 61: 1, 56: 1}


# ══════════════════════════════════════════════════════════════════
# Identificadores y folios
# ══════════════════════════════════════════════════════════════════


def test_numero_caso_es_sim_con_2_digitos():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    esperados = [f"SIM-{i:02d}" for i in range(1, TOTAL_SLOTS + 1)]
    assert [d["numero_caso"] for d in dtes] == esperados


def test_folios_consumidos_en_orden_por_tipo():
    """Cada tipo consume sus folios en el orden provisto."""
    folios = _folios_validos()
    dtes = armar_dtes_simulacion(_config_valida(), folios)
    folios_por_tipo: dict[int, list[int]] = {}
    for d in dtes:
        folios_por_tipo.setdefault(d["tipo_dte"], []).append(d["folio"])
    for tipo, esperados in folios.items():
        assert folios_por_tipo[tipo] == esperados, (
            f"T{tipo}: se esperaban folios {esperados} en orden, "
            f"se obtuvieron {folios_por_tipo[tipo]}"
        )


# ══════════════════════════════════════════════════════════════════
# Patrón slot 3: descuento por ítem 10%
# ══════════════════════════════════════════════════════════════════


def test_slot_3_aplica_descuento_pct_10_a_todos_los_items():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    slot3 = dtes[2]  # slot 3 → índice 2
    assert slot3["slot"] == 3
    for item in slot3["items"]:
        assert item["descuento_pct"] == DESCUENTO_PCT_SLOT_3


def test_otros_slots_no_llevan_descuento_pct():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    for d in dtes:
        if d["slot"] == 3:
            continue
        for item in d["items"]:
            assert item["descuento_pct"] is None, (
                f"Slot {d['slot']} item con descuento_pct "
                f"{item['descuento_pct']}, esperaba None"
            )


# ══════════════════════════════════════════════════════════════════
# Patrón slot 5: descuento global 5%
# ══════════════════════════════════════════════════════════════════


def test_slot_5_lleva_descuento_global():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    slot5 = dtes[4]  # slot 5 → índice 4
    assert slot5["slot"] == 5
    assert slot5["descuento_global"] == DSC_GLOBAL_SLOT_5


def test_otros_slots_sin_descuento_global():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    for d in dtes:
        if d["slot"] != 5:
            assert d["descuento_global"] is None


# ══════════════════════════════════════════════════════════════════
# Patrón slot 6: IndServicio = 3
# ══════════════════════════════════════════════════════════════════


def test_slot_6_lleva_ind_servicio_3():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    slot6 = dtes[5]
    assert slot6["slot"] == 6
    assert slot6["ind_servicio"] == IND_SERVICIO_SLOT_6


def test_otros_slots_sin_ind_servicio():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    for d in dtes:
        if d["slot"] != 6:
            assert d["ind_servicio"] is None


# ══════════════════════════════════════════════════════════════════
# Patrón slot 7: mixto afecto + exento
# ══════════════════════════════════════════════════════════════════


def test_slot_7_item_2_es_exento():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    slot7 = dtes[6]
    assert slot7["slot"] == 7
    assert len(slot7["items"]) == 2
    assert slot7["items"][0]["exento"] is False
    assert slot7["items"][1]["exento"] is True


def test_otros_slots_items_no_exentos():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    for d in dtes:
        if d["slot"] == 7:
            continue
        for item in d["items"]:
            assert item["exento"] is False, (
                f"Slot {d['slot']} item marcado como exento"
            )


# ══════════════════════════════════════════════════════════════════
# Patrón slots 13 y 14: guías
# ══════════════════════════════════════════════════════════════════


def test_slot_13_guia_venta():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    slot13 = dtes[12]
    assert slot13["slot"] == 13
    assert slot13["tipo_dte"] == 52
    assert slot13["tipo_despacho"] == TIPO_DESPACHO_VENTA
    assert slot13["ind_traslado"] == IND_TRASLADO_VENTA


def test_slot_14_guia_traslado_interno_con_precio_cero():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    slot14 = dtes[13]
    assert slot14["slot"] == 14
    assert slot14["tipo_dte"] == 52
    # SII: TipoDespacho solo aplica con IndTraslado=1 (venta) o
    # IndTraslado=3 (consignación). Traslado interno (5) va SIN
    # TipoDespacho. El set aprobado a TRESTRES PUBLICIDAD SPA
    # (track 0246676706, folio 140 T52) confirma esta forma.
    assert slot14["tipo_despacho"] is None
    assert slot14["ind_traslado"] == IND_TRASLADO_INTERNO
    for item in slot14["items"]:
        assert item["precio_unitario"] == 0, (
            f"Slot 14 item con precio {item['precio_unitario']}, "
            "debe forzarse a 0 (traslado interno)"
        )


def test_slot_14_fuerza_precio_cero_aunque_usuario_meta_valor():
    """Protege contra que el usuario ingrese un precio > 0 por accidente."""
    config = _config_valida()
    config["slots"][13] = _slot(14, [
        {"nombre": "Pan de molde", "cantidad": 20, "precio": 1500},
        {"nombre": "Croissant", "cantidad": 50, "precio": 800},
    ])
    dtes = armar_dtes_simulacion(config, _folios_validos())
    slot14 = dtes[13]
    for item in slot14["items"]:
        assert item["precio_unitario"] == 0


def test_otros_slots_sin_tipo_despacho_ni_ind_traslado():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    for d in dtes:
        if d["slot"] in (13, 14):
            continue
        assert d["tipo_despacho"] is None
        assert d["ind_traslado"] is None


# ══════════════════════════════════════════════════════════════════
# Patrón slots 15 y 16: NC y ND con referencia
# ══════════════════════════════════════════════════════════════════


def test_slot_15_referencia_al_slot_9():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    slot15 = dtes[14]
    assert slot15["slot"] == 15
    assert slot15["tipo_dte"] == 61
    ref = slot15["referencia"]
    assert ref is not None
    assert ref["caso_referido"] == "SIM-09"
    assert ref["tipo_doc_referido"] == 33  # slot 9 es T33
    assert ref["cod_ref"] == COD_REF_AJUSTE
    assert ref["razon"] == "Devolucion parcial"


def test_slot_16_referencia_al_slot_3():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    slot16 = dtes[15]
    assert slot16["slot"] == 16
    assert slot16["tipo_dte"] == 56
    ref = slot16["referencia"]
    assert ref is not None
    assert ref["caso_referido"] == "SIM-03"
    assert ref["tipo_doc_referido"] == 33  # slot 3 es T33
    assert ref["cod_ref"] == COD_REF_AJUSTE
    assert ref["razon"] == "Cobro interes mora"


def test_slots_15_16_usan_razon_default_si_viene_vacia():
    config = _config_valida()
    # Eliminar razón de slot 15 y 16.
    config["slots"][14] = _slot(15, _items_validos(1))
    config["slots"][15] = _slot(16, _items_validos(1))
    dtes = armar_dtes_simulacion(config, _folios_validos())
    assert dtes[14]["referencia"]["razon"] == "Devolucion parcial mercaderia"
    assert dtes[15]["referencia"]["razon"] == "Cobro interes por mora en pago"


def test_otros_slots_sin_referencia():
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    for d in dtes:
        if d["slot"] in (15, 16):
            continue
        assert d.get("referencia") is None


def test_referencias_apuntan_a_slots_previos():
    """Invariante topológica: NC/ND referencian slots ya emitidos antes."""
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    idx_por_caso = {d["numero_caso"]: i for i, d in enumerate(dtes)}
    for i, d in enumerate(dtes):
        ref = d.get("referencia")
        if ref is None:
            continue
        assert idx_por_caso[ref["caso_referido"]] < i, (
            f"{d['numero_caso']} referencia un caso posterior"
        )


# ══════════════════════════════════════════════════════════════════
# Items: forma y contenido
# ══════════════════════════════════════════════════════════════════


def test_items_incluyen_campos_esperados():
    """Cada item del output trae todos los campos de ``_caso_a_factura_request``."""
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    for d in dtes:
        for item in d["items"]:
            assert "nombre" in item
            assert "cantidad" in item
            assert "precio_unitario" in item
            assert "unidad_medida" in item
            assert "exento" in item
            assert "descuento_pct" in item


def test_items_preservan_nombre_y_cantidad_del_usuario():
    config = _config_valida()
    config["slots"][0] = _slot(1, [
        {"nombre": "Café colombiano 500g", "cantidad": 12, "precio": 8_990},
        {"nombre": "Te verde japones", "cantidad": 5, "precio": 12_500},
    ])
    dtes = armar_dtes_simulacion(config, _folios_validos())
    slot1_items = dtes[0]["items"]
    assert slot1_items[0]["nombre"] == "Café colombiano 500g"
    assert slot1_items[0]["cantidad"] == 12
    assert slot1_items[0]["precio_unitario"] == 8_990
    assert slot1_items[1]["nombre"] == "Te verde japones"


def test_cantidad_items_respeta_spec_de_cada_slot():
    """Cada slot entrega la cantidad de items que el usuario ingresó."""
    dtes = armar_dtes_simulacion(_config_valida(), _folios_validos())
    for d in dtes:
        spec = SLOT_SPECS[d["slot"]]
        assert spec["min_items"] <= len(d["items"]) <= spec["max_items"]


# ══════════════════════════════════════════════════════════════════
# Casos borde / errores
# ══════════════════════════════════════════════════════════════════


def test_folios_insuficientes_levantan_value_error():
    """Si faltan folios para algún tipo, el generador aborta explícito."""
    folios = _folios_validos()
    folios[33] = folios[33][:-1]  # 8 folios T33 en vez de 9
    with pytest.raises(ValueError, match="[Ff]olios"):
        armar_dtes_simulacion(_config_valida(), folios)


def test_slots_faltantes_levantan_value_error():
    """Si falta algún slot (1..16), el generador aborta explícito."""
    config = _config_valida()
    config["slots"] = config["slots"][:-1]  # sin slot 16
    with pytest.raises(ValueError, match="[Ff]altan"):
        armar_dtes_simulacion(config, _folios_validos())
