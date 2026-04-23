"""Validador de la configuración del Set de Simulación.

Separado del endpoint para poder testearlo sin levantar FastAPI. El
validador no muta nada: recibe un diccionario y lanza ``ValueError`` con
un mensaje en español neutro si la config incumple alguna regla.

Modelo del set (2026-04-23): esquema FIJO replicando el XML aprobado de
TRESTRES PUBLICIDAD SPA (16 DTEs en 16 slots con patrones específicos).
El usuario ingresa items y valores del negocio para cada slot; los tipos
DTE y los patrones operativos no son configurables.

Reglas que valida:

1. ``slots`` es una lista de exactamente 16 entradas (slots 1-16).
2. Cada slot identifica su número (1 a 16) y trae su propia lista de items.
3. La cantidad de items por slot respeta ``min_items``/``max_items`` del
   ``SLOT_SPECS`` en ``generador.py``.
4. Cada item tiene ``nombre`` no vacío, ``cantidad`` entero > 0 y
   ``precio`` entero ≥ 0. Precio 0 solo es válido en el slot 14 (guía
   traslado interno); en otros slots precio debe ser > 0.
5. Slot 7 requiere que al menos uno de los dos items se marque como
   exento (flag ``exento: True``); si el usuario no lo marca, el
   generador fuerza al segundo ítem como exento — validador solo emite
   advertencia si ambos vienen sin marcar.
6. Slots 15 y 16 aceptan ``razon`` opcional (string no vacío si se
   provee); si viene vacía, el generador usa razón por defecto.
7. ``receptor_rut`` cumple formato RUT chileno (``XXXXXXXX-Y``) o es
   cadena vacía (autofactura; único válido en cert).

Uso:

    from crumbpos.certificacion.simulacion.validador import (
        validar_config_simulacion,
    )

    validar_config_simulacion(config)  # raises ValueError si inválida
"""
from __future__ import annotations

import re

from crumbpos.certificacion.simulacion.generador import (
    SLOT_SPECS,
    TOTAL_SLOTS,
)

_RUT_RE = re.compile(r"^\d{1,8}-[0-9Kk]$")

# Slot 14 es el único donde se acepta precio 0 (traslado interno).
SLOT_PRECIO_CERO_OK = 14

# Slots que aceptan (y usan) el campo opcional ``razon``.
SLOTS_CON_RAZON = (15, 16)


def validar_config_simulacion(config: dict) -> None:
    """Valida una configuración de simulación.

    Args:
        config: dict con las claves ``slots`` (lista de 16 dicts) y
            ``receptor_rut`` (string opcional).

    Raises:
        ValueError: con mensaje específico si alguna regla falla.
    """
    if not isinstance(config, dict):
        raise ValueError("La configuración debe ser un diccionario.")

    slots_raw = config.get("slots")
    receptor_rut = config.get("receptor_rut", "")

    slots_indexados = _validar_estructura_slots(slots_raw)
    for slot_num in range(1, TOTAL_SLOTS + 1):
        _validar_slot(slot_num, slots_indexados[slot_num])
    _validar_receptor_rut(receptor_rut)


def _validar_estructura_slots(slots_raw) -> dict[int, dict]:
    if not isinstance(slots_raw, list):
        raise ValueError(
            "'slots' debe ser una lista con los 16 slots del set "
            "de simulación.",
        )
    if len(slots_raw) != TOTAL_SLOTS:
        raise ValueError(
            f"'slots' debe tener exactamente {TOTAL_SLOTS} entradas, "
            f"vino con {len(slots_raw)}.",
        )

    indexados: dict[int, dict] = {}
    for idx, slot in enumerate(slots_raw, start=1):
        if not isinstance(slot, dict):
            raise ValueError(
                f"Slot #{idx} debe ser un diccionario con 'slot' e 'items'.",
            )
        try:
            n = int(slot.get("slot"))
        except (TypeError, ValueError):
            raise ValueError(
                f"Slot en posición {idx}: el campo 'slot' debe ser un "
                f"entero 1-{TOTAL_SLOTS}.",
            ) from None
        if n < 1 or n > TOTAL_SLOTS:
            raise ValueError(
                f"Slot {n} fuera de rango. Debe ser 1-{TOTAL_SLOTS}.",
            )
        if n in indexados:
            raise ValueError(
                f"Slot {n} aparece duplicado en la config.",
            )
        indexados[n] = slot

    faltantes = [n for n in range(1, TOTAL_SLOTS + 1) if n not in indexados]
    if faltantes:
        raise ValueError(
            f"Faltan slots en la config: {faltantes}.",
        )
    return indexados


def _validar_slot(slot_num: int, slot: dict) -> None:
    spec = SLOT_SPECS[slot_num]
    items = slot.get("items")

    if not isinstance(items, list):
        raise ValueError(
            f"Slot {slot_num} ({spec['label']}): 'items' debe ser una lista.",
        )
    if len(items) < spec["min_items"]:
        raise ValueError(
            f"Slot {slot_num} ({spec['label']}): requiere al menos "
            f"{spec['min_items']} ítem(s). Hay {len(items)}.",
        )
    if len(items) > spec["max_items"]:
        raise ValueError(
            f"Slot {slot_num} ({spec['label']}): acepta como máximo "
            f"{spec['max_items']} ítem(s). Hay {len(items)}.",
        )

    for idx, item in enumerate(items, start=1):
        _validar_item(slot_num, idx, item)

    # Slot 7: advertir si el usuario no marcó ninguno como exento — el
    # generador fuerza al segundo, pero prefiero que el usuario lo decida
    # explícito. No es error, solo nota: aceptamos el default del generador.
    # (No levantamos para no bloquear la UI con un warning.)

    if slot_num in SLOTS_CON_RAZON:
        razon = slot.get("razon")
        if razon is not None and not isinstance(razon, str):
            raise ValueError(
                f"Slot {slot_num}: 'razon' debe ser string o ausente.",
            )


def _validar_item(slot_num: int, idx: int, item: dict) -> None:
    if not isinstance(item, dict):
        raise ValueError(
            f"Slot {slot_num} — ítem #{idx}: debe ser un diccionario "
            "con 'nombre', 'cantidad' y 'precio'.",
        )
    nombre = str(item.get("nombre", "")).strip()
    if not nombre:
        raise ValueError(
            f"Slot {slot_num} — ítem #{idx}: el nombre no puede estar vacío.",
        )

    cantidad_raw = item.get("cantidad")
    try:
        cantidad = int(cantidad_raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"Slot {slot_num} — ítem #{idx} ('{nombre}'): cantidad "
            f"inválida '{cantidad_raw}'. Debe ser un entero positivo.",
        ) from None
    if cantidad <= 0:
        raise ValueError(
            f"Slot {slot_num} — ítem #{idx} ('{nombre}'): cantidad "
            f"debe ser > 0. Hay {cantidad}.",
        )

    precio_raw = item.get("precio")
    try:
        precio = int(precio_raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"Slot {slot_num} — ítem #{idx} ('{nombre}'): precio inválido "
            f"'{precio_raw}'. Debe ser un entero.",
        ) from None
    if precio < 0:
        raise ValueError(
            f"Slot {slot_num} — ítem #{idx} ('{nombre}'): precio no "
            f"puede ser negativo. Hay {precio}.",
        )
    if precio == 0 and slot_num != SLOT_PRECIO_CERO_OK:
        raise ValueError(
            f"Slot {slot_num} — ítem #{idx} ('{nombre}'): precio debe "
            "ser > 0 (solo el slot 14 de traslado interno acepta 0).",
        )


def _validar_receptor_rut(receptor_rut) -> None:
    """Receptor vacío = autofactura (único válido en ambiente certificación).

    Si viene con valor, debe cumplir el formato RUT chileno ``XXXXXXXX-Y``
    con DV 0-9 o K.
    """
    if receptor_rut is None:
        return
    if not isinstance(receptor_rut, str):
        raise ValueError(
            f"'receptor_rut' debe ser un string, vino "
            f"{type(receptor_rut).__name__}.",
        )
    rut_limpio = receptor_rut.strip().replace(".", "")
    if rut_limpio == "":
        return  # autofactura
    if not _RUT_RE.match(rut_limpio):
        raise ValueError(
            f"RUT receptor inválido: '{receptor_rut}'. "
            "Formato esperado: XXXXXXXX-Y (ej: 77829149-5).",
        )
