"""Generador de DTEs para el Set de Simulación (esquema fijo 16 DTEs).

Replica exactamente el layout aprobado por el SII en la certificación de
TRESTRES PUBLICIDAD SPA (77051056-2, track 0246676706 — 2026-03-26):

    9 × T33 + 3 × T34 + 2 × T52 + 1 × T61 + 1 × T56 = 16 DTEs

Cada uno de los 16 "slots" tiene un patrón específico (descuento por ítem,
descuento global, servicio, mixto afecto+exento, traslado interno, etc.).
El wizard pide los items y valores del negocio para cada slot; el
generador aplica el patrón y devuelve los dicts listos para persistir
como ``CertificacionCaso``.

Decisiones de diseño (2026-04-23, tras confirmación del usuario):

- **Esquema fijo, no configurable**. El SII ya aprobó esta distribución;
  replicar exactamente en 77829149-5 minimiza el riesgo de rechazo. No hay
  aleatoriedad, no hay "pool" compartido, no hay reshuffle.

- **Items por slot vienen del wizard**. Cada slot declara cuántos items
  necesita (``min_items``/``max_items``) y qué flags aplican. El usuario
  ingresa nombre + cantidad + precio para cada ítem, consistente con su
  giro real.

- **Patrones fijos por slot** (el generador los aplica, no son
  configurables desde la config):
  - Slot 3: ``DescuentoPct=10`` en TODOS los ítems.
  - Slot 5: ``DscRcgGlobal`` tipo ``D``, tipo_valor ``%``, valor ``5``,
    descripción "Descuento cliente frecuente".
  - Slot 6: ``IndServicio=3`` (factura de servicios).
  - Slot 7: requiere EXACTAMENTE 2 items, el segundo con ``exento=True``.
  - Slot 13: ``TipoDespacho=2``, ``IndTraslado=1`` (guía con venta).
  - Slot 14: ``IndTraslado=5`` (traslado interno), sin ``TipoDespacho``.
    Precios forzados a 0 por el mapper al armar el DTE. ``TipoDespacho``
    solo aplica con venta (IndTraslado=1) o consignación (IndTraslado=3);
    el core rechaza la emisión si viene con traslado interno.
  - Slot 15: T61 referencia al slot 9, ``CodRef=3``, razón editable.
  - Slot 16: T56 referencia al slot 3, ``CodRef=3``, razón editable.

- **Referencias**: el generador resuelve el ``numero_caso`` de los slots
  referenciados dentro del mismo batch (no hace lookup de DB). Slot 15
  apunta al slot 9 y slot 16 al slot 3 — fijo, sin elección del usuario.

- **Numero_caso**: ``SIM-NN`` (2 dígitos, slot 1-16), alineado con el
  orden del layout. Prefijo distinto de BASICO/GUIAS/EXENTA para que los
  filtros por ``set_nombre`` sigan funcionando.

- **Receptor**: no se resuelve aquí. ``_caso_a_factura_request`` usa el
  RUT del emisor (autofactura) cuando ``receptor_rut`` viene vacío, que
  es el único valor válido en ambiente certificación.

Formato del output (cada elemento de la lista devuelta):

    {
        "numero_caso": "SIM-01",
        "slot": 1,
        "tipo_dte": 33,
        "folio": 78,
        "items": [
            {
                "nombre": "Pan de molde integral 500g",
                "cantidad": 50,
                "precio_unitario": 1890,
                "unidad_medida": None,
                "exento": False,
                "descuento_pct": None,
            },
            ...
        ],
        "ind_servicio": None,       # o 3 para slot 6
        "tipo_despacho": None,       # o 1/2 para T52
        "ind_traslado": None,        # o 1/5 para T52
        "descuento_global": None,    # o dict para slot 5
        "referencia": None,          # o dict para slots 15/16
    }
"""
from __future__ import annotations

from typing import Any

# Tipos DTE soportados por el set de simulación.
TIPO_FACTURA_AFECTA = 33
TIPO_FACTURA_EXENTA = 34
TIPO_GUIA = 52
TIPO_NOTA_CREDITO = 61
TIPO_NOTA_DEBITO = 56

# Cantidades EXACTAS del esquema aprobado.
CANTIDAD_POR_TIPO = {
    TIPO_FACTURA_AFECTA: 9,
    TIPO_FACTURA_EXENTA: 3,
    TIPO_GUIA: 2,
    TIPO_NOTA_CREDITO: 1,
    TIPO_NOTA_DEBITO: 1,
}
TOTAL_SLOTS = sum(CANTIDAD_POR_TIPO.values())  # 16

# Orden de emisión de los 16 slots — coincide con el XML aprobado:
# primero los 9 T33, después los 3 T34, después los 2 T52, luego T61 y T56.
ORDEN_TIPOS = (
    [TIPO_FACTURA_AFECTA] * 9
    + [TIPO_FACTURA_EXENTA] * 3
    + [TIPO_GUIA] * 2
    + [TIPO_NOTA_CREDITO]
    + [TIPO_NOTA_DEBITO]
)

# Descuento global del slot 5, fijo por contrato con el set aprobado.
DSC_GLOBAL_SLOT_5 = {
    "tipo_movimiento": "D",        # Descuento (vs "R" recargo)
    "descripcion": "Descuento cliente frecuente",
    "tipo_valor": "%",
    "valor": 5,
}

# Descuento por ítem del slot 3 — se aplica a TODOS los items del slot.
DESCUENTO_PCT_SLOT_3 = 10

# IndServicio del slot 6 — factura de servicios.
IND_SERVICIO_SLOT_6 = 3

# Parámetros de guías.
TIPO_DESPACHO_VENTA = 2        # Slot 13: del cliente
IND_TRASLADO_VENTA = 1          # Slot 13: venta
# Slot 14 (traslado interno, IndTraslado=5): NO lleva TipoDespacho.
# Según la reglas SII y la muestra aprobada por el SII para TRESTRES
# PUBLICIDAD SPA (track 0246676706, folio 140), TipoDespacho solo aplica
# con venta (IndTraslado=1) o consignación (IndTraslado=3). El core
# valida esta regla en _validar_guia_despacho() — si pasamos TipoDespacho
# con IndTraslado=5, rechaza la emisión.
IND_TRASLADO_INTERNO = 5        # Slot 14: traslado interno

# Slots que se referencian con notas. 1-indexed porque el wizard muestra
# los slots numerados del 1 al 16.
SLOT_REF_NC = 9   # Slot 15 (T61) referencia al slot 9 (T33 multi-item)
SLOT_REF_ND = 3   # Slot 16 (T56) referencia al slot 3 (T33 con descuento)
COD_REF_AJUSTE = 3  # "Corrige montos" — el que aprobó el SII en el set

# Requerimientos de items por slot (1-indexed). El wizard fuerza estos
# mínimos/máximos; el validador confirma que la config los cumple.
#
# ``min_items``/``max_items``: cantidad de items del slot.
# ``label``: etiqueta humana mostrada en la UI.
# ``hint``: ayuda adicional para el usuario.
SLOT_SPECS: dict[int, dict[str, Any]] = {
    1:  {"tipo": 33, "min_items": 2, "max_items": 3,
         "label": "Factura afecta — multi-item simple",
         "hint": "2 o 3 productos/servicios del giro."},
    2:  {"tipo": 33, "min_items": 3, "max_items": 4,
         "label": "Factura afecta — volumen",
         "hint": "3 o 4 ítems representativos de una venta grande."},
    3:  {"tipo": 33, "min_items": 2, "max_items": 2,
         "label": "Factura afecta — con descuento por ítem (10%)",
         "hint": "Exactamente 2 ítems. Se aplica 10% de descuento por ítem."},
    4:  {"tipo": 33, "min_items": 2, "max_items": 3,
         "label": "Factura afecta — multi-item",
         "hint": "2 o 3 productos distintos."},
    5:  {"tipo": 33, "min_items": 2, "max_items": 2,
         "label": "Factura afecta — con descuento global (5%)",
         "hint": "Exactamente 2 ítems. Descuento global 5% sobre el total."},
    6:  {"tipo": 33, "min_items": 1, "max_items": 1,
         "label": "Factura afecta — servicios (IndServicio=3)",
         "hint": "1 servicio del giro. Ej: asesoría, catering, capacitación."},
    7:  {"tipo": 33, "min_items": 2, "max_items": 2,
         "label": "Factura afecta — mixta afecto + exento",
         "hint": "2 ítems: el primero afecto, el segundo exento."},
    8:  {"tipo": 33, "min_items": 1, "max_items": 1,
         "label": "Factura afecta — 1 item",
         "hint": "1 producto/servicio simple."},
    9:  {"tipo": 33, "min_items": 2, "max_items": 2,
         "label": "Factura afecta — 2 items (referenciada por NC)",
         "hint": "2 ítems. La nota de crédito (slot 15) referencia esta factura."},
    10: {"tipo": 34, "min_items": 2, "max_items": 2,
         "label": "Factura exenta — 2 items",
         "hint": "2 productos/servicios exentos."},
    11: {"tipo": 34, "min_items": 1, "max_items": 1,
         "label": "Factura exenta — 1 item",
         "hint": "1 servicio o producto exento."},
    12: {"tipo": 34, "min_items": 1, "max_items": 1,
         "label": "Factura exenta — 1 item",
         "hint": "1 servicio o producto exento."},
    13: {"tipo": 52, "min_items": 2, "max_items": 2,
         "label": "Guía de despacho — venta (TipoDespacho=2)",
         "hint": "2 productos despachados al cliente."},
    14: {"tipo": 52, "min_items": 2, "max_items": 2,
         "label": "Guía de despacho — traslado interno (precios en 0)",
         "hint": "2 productos, el precio se fuerza a 0 (traslado entre bodegas)."},
    15: {"tipo": 61, "min_items": 1, "max_items": 1,
         "label": "Nota de crédito — referencia slot 9",
         "hint": "1 ítem devuelto. Requiere razón (ej: 'Devolución parcial')."},
    16: {"tipo": 56, "min_items": 1, "max_items": 1,
         "label": "Nota de débito — referencia slot 3",
         "hint": "1 ítem (ej: interés por mora). Requiere razón."},
}


def armar_dtes_simulacion(
    config: dict,
    folios_por_tipo: dict[int, list[int]],
) -> list[dict]:
    """Arma los 16 DTEs del Set de Simulación con el esquema fijo aprobado.

    Args:
        config: dict con ``slots`` (lista de 16 slots en orden 1→16, cada
            uno con ``items`` y —para slots 15/16— ``razon``). Debe haber
            pasado ``validar_config_simulacion``.
        folios_por_tipo: mapping ``{tipo_dte: [folio, folio, ...]}`` con
            los folios YA reservados del CAF, en cantidad exacta
            (9 T33, 3 T34, 2 T52, 1 T61, 1 T56).

    Returns:
        Lista de 16 dicts en orden de slot. Cada dict es el ``datos``
        de un ``CertificacionCaso``.

    Raises:
        ValueError: si faltan folios o la config no cumple el layout.
    """
    slots_config = _indexar_slots(config)
    _verificar_folios_exactos(folios_por_tipo)

    # Copiar las listas de folios porque vamos a consumirlas en orden.
    cola_folios = {
        tipo: list(folios_por_tipo[tipo]) for tipo in folios_por_tipo
    }

    dtes: list[dict] = []
    # Mapea slot_num → numero_caso para resolver referencias de NC/ND.
    numero_caso_por_slot: dict[int, str] = {}

    for slot_num, tipo in enumerate(ORDEN_TIPOS, start=1):
        spec = SLOT_SPECS[slot_num]
        items_config = slots_config[slot_num]["items"]
        razon = slots_config[slot_num].get("razon")

        folio = cola_folios[tipo].pop(0)
        numero_caso = f"SIM-{slot_num:02d}"
        numero_caso_por_slot[slot_num] = numero_caso

        items = _construir_items(slot_num, items_config)
        dte = {
            "numero_caso": numero_caso,
            "slot": slot_num,
            "tipo_dte": tipo,
            "folio": folio,
            "items": items,
            "ind_servicio": _ind_servicio(slot_num),
            "tipo_despacho": _tipo_despacho(slot_num),
            "ind_traslado": _ind_traslado(slot_num),
            "descuento_global": _descuento_global(slot_num),
            "referencia": _referencia(
                slot_num, numero_caso_por_slot, razon,
            ),
        }
        dtes.append(dte)

    return dtes


# ══════════════════════════════════════════════════════════════════
# Helpers de layout
# ══════════════════════════════════════════════════════════════════


def _indexar_slots(config: dict) -> dict[int, dict]:
    """Devuelve ``{slot_num: slot_config}`` a partir de ``config['slots']``.

    Cada ``slot_config`` tiene ``items`` (lista) y opcionalmente ``razon``
    (para slots 15/16). Acepta claves ``slot`` como int o string.
    """
    raw = config.get("slots") or []
    indexado: dict[int, dict] = {}
    for s in raw:
        try:
            n = int(s.get("slot"))
        except (TypeError, ValueError):
            raise ValueError(
                f"Slot con número inválido: {s.get('slot')!r}",
            ) from None
        indexado[n] = s
    faltantes = [n for n in range(1, TOTAL_SLOTS + 1) if n not in indexado]
    if faltantes:
        raise ValueError(
            f"Faltan slots en la config: {faltantes}. "
            f"Se esperan los 16 slots (1-{TOTAL_SLOTS}).",
        )
    return indexado


def _verificar_folios_exactos(folios_por_tipo: dict[int, list[int]]) -> None:
    for tipo, cantidad_esperada in CANTIDAD_POR_TIPO.items():
        disponibles = len(folios_por_tipo.get(tipo, []))
        if disponibles < cantidad_esperada:
            raise ValueError(
                f"Folios insuficientes para T{tipo}: se requieren "
                f"{cantidad_esperada} y hay {disponibles} reservados.",
            )


def _construir_items(slot_num: int, items_config: list[dict]) -> list[dict]:
    """Construye la lista final de items aplicando el patrón del slot.

    Los items vienen del wizard con ``nombre``, ``cantidad``, ``precio``
    (y opcionalmente ``exento`` para el slot 7). El generador expande a
    la forma que espera ``_caso_a_factura_request`` y aplica los flags
    del slot (descuento, exento, precio=0 para traslado interno).
    """
    salida: list[dict] = []
    for idx, raw in enumerate(items_config, start=1):
        nombre = str(raw["nombre"]).strip()
        cantidad = int(raw["cantidad"])
        precio_ingresado = int(raw["precio"])

        # Slot 14 (T52 traslado interno): precios forzados a 0.
        if slot_num == 14:
            precio = 0
        else:
            precio = precio_ingresado

        # Slot 7: el segundo ítem se marca como exento.
        if slot_num == 7:
            exento = idx == 2 or bool(raw.get("exento"))
        else:
            exento = False

        # Slot 3: todos los ítems llevan DescuentoPct=10.
        descuento_pct = DESCUENTO_PCT_SLOT_3 if slot_num == 3 else None

        salida.append({
            "nombre": nombre,
            "cantidad": cantidad,
            "precio_unitario": precio,
            "unidad_medida": None,
            "exento": exento,
            "descuento_pct": descuento_pct,
        })
    return salida


def _ind_servicio(slot_num: int) -> int | None:
    return IND_SERVICIO_SLOT_6 if slot_num == 6 else None


def _tipo_despacho(slot_num: int) -> int | None:
    if slot_num == 13:
        return TIPO_DESPACHO_VENTA
    # Slot 14 (traslado interno) NO lleva TipoDespacho — ver comentario
    # arriba junto a IND_TRASLADO_INTERNO.
    return None


def _ind_traslado(slot_num: int) -> int | None:
    if slot_num == 13:
        return IND_TRASLADO_VENTA
    if slot_num == 14:
        return IND_TRASLADO_INTERNO
    return None


def _descuento_global(slot_num: int) -> dict | None:
    if slot_num == 5:
        return dict(DSC_GLOBAL_SLOT_5)
    return None


def _referencia(
    slot_num: int,
    numero_caso_por_slot: dict[int, str],
    razon: str | None,
) -> dict | None:
    """Devuelve el dict de referencia para slots 15 (NC) y 16 (ND)."""
    if slot_num == 15:
        referido_num = SLOT_REF_NC
        tipo_ref = SLOT_SPECS[referido_num]["tipo"]
        razon_final = (razon or "Devolucion parcial mercaderia").strip()
    elif slot_num == 16:
        referido_num = SLOT_REF_ND
        tipo_ref = SLOT_SPECS[referido_num]["tipo"]
        razon_final = (razon or "Cobro interes por mora en pago").strip()
    else:
        return None

    caso_referido = numero_caso_por_slot.get(referido_num)
    if not caso_referido:
        # No debería pasar porque iteramos en orden, pero defensa en
        # profundidad — si alguna vez se reordena, fallamos aquí explícito.
        raise ValueError(
            f"Slot {slot_num} referencia al slot {referido_num} pero no "
            "se emitió antes. Revisar ORDEN_TIPOS.",
        )
    return {
        "caso_referido": caso_referido,
        "tipo_doc_referido": tipo_ref,
        "cod_ref": COD_REF_AJUSTE,
        "razon": razon_final,
    }
