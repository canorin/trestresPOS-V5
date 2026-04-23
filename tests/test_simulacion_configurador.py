"""Tests de ``crumbpos.certificacion.simulacion.validador``.

Actualizado 2026-04-23 tras la reescritura completa del validador:

- Esquema FIJO de 16 slots (1..16), sin flexibilidad en tipos ni
  cantidades totales.
- Cada slot tiene sus propias reglas de min/max items, precios
  permitidos, etc. definidas en ``SLOT_SPECS``.
- Los únicos campos configurables son los ítems por slot, sus nombres,
  cantidades y precios, y la razón opcional de slots 15/16.
- Receptor RUT vacío (autofactura) o formato ``XXXXXXXX-Y``.

Son tests unitarios puros: construyen dicts y llaman al validador.
"""
from __future__ import annotations

import pytest

from crumbpos.certificacion.simulacion.validador import (
    validar_config_simulacion,
)


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════


def _items_validos(n: int, precio: int = 10_000) -> list[dict]:
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
    """Config base válida con los 16 slots mínimamente poblados."""
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


# ══════════════════════════════════════════════════════════════════
# Caso feliz
# ══════════════════════════════════════════════════════════════════


def test_config_valida_pasa_sin_errores():
    validar_config_simulacion(_config_valida())


def test_acepta_receptor_rut_valido():
    config = _config_valida()
    config["receptor_rut"] = "77829149-5"
    validar_config_simulacion(config)


def test_acepta_receptor_rut_con_dv_k():
    config = _config_valida()
    config["receptor_rut"] = "60803000-K"
    validar_config_simulacion(config)


def test_acepta_receptor_rut_ausente():
    """Receptor no provisto = autofactura implícita."""
    config = _config_valida()
    del config["receptor_rut"]
    validar_config_simulacion(config)


# ══════════════════════════════════════════════════════════════════
# Estructura de ``slots``
# ══════════════════════════════════════════════════════════════════


def test_rechaza_config_que_no_es_dict():
    with pytest.raises(ValueError, match="diccionario"):
        validar_config_simulacion(["no", "es", "dict"])


def test_rechaza_slots_no_lista():
    config = _config_valida()
    config["slots"] = "no es lista"
    with pytest.raises(ValueError, match="lista"):
        validar_config_simulacion(config)


def test_rechaza_menos_de_16_slots():
    config = _config_valida()
    config["slots"] = config["slots"][:15]
    with pytest.raises(ValueError, match="16"):
        validar_config_simulacion(config)


def test_rechaza_mas_de_16_slots():
    config = _config_valida()
    extra = _slot(17, _items_validos(1))
    config["slots"] = config["slots"] + [extra]
    with pytest.raises(ValueError, match="16"):
        validar_config_simulacion(config)


def test_rechaza_slot_fuera_de_rango():
    config = _config_valida()
    # Reemplazar el slot 16 por uno con número 99.
    config["slots"][15] = _slot(99, _items_validos(1))
    with pytest.raises(ValueError, match="[Ff]uera de rango|99"):
        validar_config_simulacion(config)


def test_rechaza_slot_duplicado():
    config = _config_valida()
    # Duplicar el slot 1 reemplazando el slot 2.
    config["slots"][1] = _slot(1, _items_validos(2))
    with pytest.raises(ValueError, match="duplicado"):
        validar_config_simulacion(config)


def test_rechaza_slot_no_dict():
    config = _config_valida()
    config["slots"][0] = "no es dict"
    with pytest.raises(ValueError, match="diccionario"):
        validar_config_simulacion(config)


def test_rechaza_slot_numero_no_entero():
    config = _config_valida()
    config["slots"][0] = {"slot": "abc", "items": _items_validos(2)}
    with pytest.raises(ValueError, match="entero"):
        validar_config_simulacion(config)


# ══════════════════════════════════════════════════════════════════
# Cantidad de items por slot (min/max)
# ══════════════════════════════════════════════════════════════════


def test_rechaza_slot_con_menos_items_que_el_minimo():
    """Slot 1 requiere al menos 2 items."""
    config = _config_valida()
    config["slots"][0] = _slot(1, _items_validos(1))
    with pytest.raises(ValueError, match="al menos"):
        validar_config_simulacion(config)


def test_rechaza_slot_con_mas_items_que_el_maximo():
    """Slot 3 acepta exactamente 2 items."""
    config = _config_valida()
    config["slots"][2] = _slot(3, _items_validos(5))
    with pytest.raises(ValueError, match="máximo"):
        validar_config_simulacion(config)


def test_rechaza_slot_con_items_no_lista():
    config = _config_valida()
    config["slots"][0] = {"slot": 1, "items": "no es lista"}
    with pytest.raises(ValueError, match="lista"):
        validar_config_simulacion(config)


# ══════════════════════════════════════════════════════════════════
# Validación de ítems
# ══════════════════════════════════════════════════════════════════


def test_rechaza_item_sin_nombre():
    config = _config_valida()
    config["slots"][0]["items"][0]["nombre"] = ""
    with pytest.raises(ValueError, match="nombre"):
        validar_config_simulacion(config)


def test_rechaza_item_con_nombre_solo_espacios():
    config = _config_valida()
    config["slots"][0]["items"][0]["nombre"] = "   "
    with pytest.raises(ValueError, match="nombre"):
        validar_config_simulacion(config)


def test_rechaza_cantidad_cero():
    config = _config_valida()
    config["slots"][0]["items"][0]["cantidad"] = 0
    with pytest.raises(ValueError, match="cantidad"):
        validar_config_simulacion(config)


def test_rechaza_cantidad_negativa():
    config = _config_valida()
    config["slots"][0]["items"][0]["cantidad"] = -3
    with pytest.raises(ValueError, match="cantidad"):
        validar_config_simulacion(config)


def test_rechaza_cantidad_no_numerica():
    config = _config_valida()
    config["slots"][0]["items"][0]["cantidad"] = "tres"
    with pytest.raises(ValueError, match="cantidad"):
        validar_config_simulacion(config)


def test_rechaza_precio_negativo():
    config = _config_valida()
    config["slots"][0]["items"][0]["precio"] = -100
    with pytest.raises(ValueError, match="precio"):
        validar_config_simulacion(config)


def test_rechaza_precio_no_numerico():
    config = _config_valida()
    config["slots"][0]["items"][0]["precio"] = "gratis"
    with pytest.raises(ValueError, match="precio"):
        validar_config_simulacion(config)


def test_rechaza_precio_cero_en_slot_distinto_de_14():
    """Solo el slot 14 (traslado interno) acepta precio 0."""
    config = _config_valida()
    config["slots"][0]["items"][0]["precio"] = 0
    with pytest.raises(ValueError, match="precio"):
        validar_config_simulacion(config)


def test_acepta_precio_cero_en_slot_14():
    """Slot 14 es el único donde precio 0 es válido (traslado interno)."""
    # El helper _config_valida ya setea precio 0 en slot 14; reconfirmar.
    validar_config_simulacion(_config_valida())


# ══════════════════════════════════════════════════════════════════
# Razón opcional en slots 15 y 16
# ══════════════════════════════════════════════════════════════════


def test_acepta_slot_15_sin_razon():
    config = _config_valida()
    # Quitar razón del slot 15 (índice 14).
    del config["slots"][14]["razon"]
    validar_config_simulacion(config)


def test_acepta_slot_16_sin_razon():
    config = _config_valida()
    del config["slots"][15]["razon"]
    validar_config_simulacion(config)


def test_rechaza_razon_no_string_en_slot_15():
    config = _config_valida()
    config["slots"][14]["razon"] = 12345
    with pytest.raises(ValueError, match="razon"):
        validar_config_simulacion(config)


def test_rechaza_razon_no_string_en_slot_16():
    config = _config_valida()
    config["slots"][15]["razon"] = ["no", "string"]
    with pytest.raises(ValueError, match="razon"):
        validar_config_simulacion(config)


# ══════════════════════════════════════════════════════════════════
# Receptor RUT
# ══════════════════════════════════════════════════════════════════


def test_rechaza_receptor_rut_no_string():
    config = _config_valida()
    config["receptor_rut"] = 12345678
    with pytest.raises(ValueError, match="string"):
        validar_config_simulacion(config)


def test_rechaza_receptor_rut_con_formato_invalido():
    config = _config_valida()
    config["receptor_rut"] = "ABC-XYZ"
    with pytest.raises(ValueError, match="RUT"):
        validar_config_simulacion(config)


def test_rechaza_receptor_rut_sin_dv():
    config = _config_valida()
    config["receptor_rut"] = "77829149"
    with pytest.raises(ValueError, match="RUT"):
        validar_config_simulacion(config)


def test_rechaza_receptor_rut_con_dv_invalido():
    """DV válidos: 0-9 y K. Letras arbitrarias se rechazan."""
    config = _config_valida()
    config["receptor_rut"] = "77829149-A"
    with pytest.raises(ValueError, match="RUT"):
        validar_config_simulacion(config)


def test_acepta_receptor_rut_con_puntos_y_los_limpia():
    """Si el usuario pega RUT con puntos (12.345.678-9) igual se acepta."""
    config = _config_valida()
    config["receptor_rut"] = "77.829.149-5"
    validar_config_simulacion(config)
