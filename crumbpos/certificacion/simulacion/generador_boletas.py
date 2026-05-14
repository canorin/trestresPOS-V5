"""Generador de DTEs para el Set de Pruebas de Boleta Electrónica (T39).

El SII envía un set fijo de 5 casos para certificar la boleta electrónica
de ventas y servicios. A diferencia del set básico/guías/exenta, el set
de boletas NO viene en el SIISetDePruebas{RUT}.txt del certificado normal
— el SII lo entrega por separado junto con el track de boletas.

Casos extraídos del instructivo oficial (Set de Prueba BE):
    CASO-1: Cambio de aceite (1×$19.900) + Alineacion y balanceo (1×$9.900)
    CASO-2: Papel de regalo (17×$120)
    CASO-3: Sandwic (2×$1.500) + Bebida (2×$550)
    CASO-4: item afecto 1 (8×$1.590) + item exento 2 (2×$1.000)  — mixto
    CASO-5: Arroz (5×$700) — con unidad de medida "Kg"

Reglas del set:
- Todos los casos son T39 (Boleta Electrónica Afecta).
- Los precios son CON IVA incluido (IndMntBruto=1 en el XML).
  El DTE.calcular_totales() los trata así automáticamente para T39.
- CASO-4: item exento 2 lleva exento=True; el core calcula neto/iva
  solo sobre la parte afecta.
- CASO-5: Arroz lleva unidad_medida="Kg" según la observación del set.
- Cada boleta referencia su caso via <CodRef>SET</CodRef><RazonRef>CASO-N</RazonRef>.
  El campo caso_set del FacturaRequest controla esto; la lógica de
  emision_dte.py agrega codigo_ref="SET" para boletas.

Formato del output (cada elemento de la lista devuelta por armar_dtes_boletas):

    {
        "numero_caso": "CASO-1",
        "tipo_dte": 39,
        "folio": <asignado por el caller>,
        "items": [
            {
                "nombre": "Cambio de aceite",
                "cantidad": 1,
                "precio_unitario": 19900,
                "unidad_medida": None,
                "exento": False,
                "descuento_pct": None,
            },
            ...
        ],
    }
"""
from __future__ import annotations

# Nombre canónico del set en la base de datos (set_nombre en CertificacionCaso).
SET_BOLETAS_NOMBRE = "BOLETAS"

# Tipo DTE del set de boletas afectas.
TIPO_BOLETA_AFECTA = 39

# Datos fijos del set de prueba de Boleta Electrónica.
# Los precios son "Precio Unitario con IVA" tal como indica el instructivo.
# Orden idéntico al documento oficial (CASO-1 … CASO-5).
_CASOS_RAW: list[dict] = [
    {
        "numero_caso": "CASO-1",
        "items": [
            {"nombre": "Cambio de aceite", "cantidad": 1, "precio_unitario": 19900},
            {"nombre": "Alineacion y balanceo", "cantidad": 1, "precio_unitario": 9900},
        ],
    },
    {
        "numero_caso": "CASO-2",
        "items": [
            {"nombre": "Papel de regalo", "cantidad": 17, "precio_unitario": 120},
        ],
    },
    {
        "numero_caso": "CASO-3",
        "items": [
            {"nombre": "Sandwic", "cantidad": 2, "precio_unitario": 1500},
            {"nombre": "Bebida", "cantidad": 2, "precio_unitario": 550},
        ],
    },
    {
        "numero_caso": "CASO-4",
        # Observación del set: "El item 1 es un servicio afecto. El item 2 es un servicio exento."
        "items": [
            {"nombre": "item afecto 1", "cantidad": 8, "precio_unitario": 1590, "exento": False},
            {"nombre": "item exento 2", "cantidad": 2, "precio_unitario": 1000, "exento": True},
        ],
    },
    {
        "numero_caso": "CASO-5",
        # Observación del set: "Se debe informar en el XML Unidad de medida en Kg."
        "items": [
            {"nombre": "Arroz", "cantidad": 5, "precio_unitario": 700, "unidad_medida": "Kg"},
        ],
    },
]

# Número de folios T39 que requiere el set.
FOLIOS_REQUERIDOS = len(_CASOS_RAW)  # 5


def armar_dtes_boletas(folios_t39: list[int]) -> list[dict]:
    """Arma los 5 casos del Set de Pruebas de Boleta Electrónica.

    Args:
        folios_t39: Lista de al menos 5 folios T39 ya reservados del CAF,
            en orden ascendente. Se asignan uno a uno a CASO-1 … CASO-5.

    Returns:
        Lista de 5 dicts en orden CASO-1 … CASO-5. Cada dict es el
        ``datos`` listo para persistir como ``CertificacionCaso``.

    Raises:
        ValueError: si no hay suficientes folios T39.
    """
    if len(folios_t39) < FOLIOS_REQUERIDOS:
        raise ValueError(
            f"Se requieren {FOLIOS_REQUERIDOS} folios T39 para el set de boletas "
            f"pero se proporcionaron {len(folios_t39)}.",
        )

    resultado: list[dict] = []
    for idx, caso_raw in enumerate(_CASOS_RAW):
        items = [
            {
                "nombre": it["nombre"],
                "cantidad": it["cantidad"],
                "precio_unitario": it["precio_unitario"],
                "unidad_medida": it.get("unidad_medida"),
                "exento": it.get("exento", False),
                "descuento_pct": None,
            }
            for it in caso_raw["items"]
        ]
        resultado.append({
            "numero_caso": caso_raw["numero_caso"],
            "tipo_dte": TIPO_BOLETA_AFECTA,
            "folio": folios_t39[idx],
            "items": items,
            # Campos de formato compatibles con _caso_a_factura_request:
            "ind_servicio": None,
            "tipo_despacho": None,
            "ind_traslado": None,
            "descuento_global_pct": None,
            "referencia": None,
        })

    return resultado
