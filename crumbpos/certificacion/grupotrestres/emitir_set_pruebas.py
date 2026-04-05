"""Emite el set de pruebas SII para GRUPO TRESTRES SPA (77829149-5).

Atención: 4757724 (básico)

Uso: python3 grupotrestres/emitir_set_pruebas.py
"""
import requests
import json
import time
import base64
from pathlib import Path

API = "http://localhost:8000/api/facturacion/emitir"
EMPRESA_RUT = "77829149-5"

# Receptor genérico para set de pruebas (puede ser cualquier empresa válida)
RECEPTOR = {
    "receptor_rut": "77051056-2",
    "receptor_razon": "TRESTRES PUBLICIDAD SPA",
    "receptor_giro": "PUBLICIDAD",
    "receptor_dir": "Los Militares 5620 OF 905",
    "receptor_comuna": "Las Condes",
    "receptor_ciudad": "Santiago",
}

# Guardar resultados para referencias cruzadas
resultados = {}
PDF_DIR = Path(__file__).resolve().parent.parent.parent.parent / "grupotrestres" / "pdfs_set"
PDF_DIR.mkdir(exist_ok=True)


def emitir(caso_id: str, payload: dict) -> dict:
    """Emite un DTE y guarda el resultado."""
    payload["empresa_rut"] = EMPRESA_RUT
    payload.update(RECEPTOR)

    print(f"\n{'='*60}")
    print(f"CASO {caso_id}")
    print(f"{'='*60}")
    print(f"  Tipo: {payload['tipo_dte']}")

    resp = requests.post(API, json=payload)
    data = resp.json()

    if data.get("ok"):
        print(f"  OK — Folio: {data['folio']}, Track: {data['track_id']}")
        print(f"  Neto: {data.get('monto_neto')}, Exento: {data.get('monto_exento')}, "
              f"IVA: {data.get('iva')}, Total: {data.get('monto_total')}")
        resultados[caso_id] = data

        # Guardar PDF
        if data.get("pdf_base64"):
            pdf_path = PDF_DIR / f"caso_{caso_id}_T{payload['tipo_dte']}_F{data['folio']}.pdf"
            pdf_path.write_bytes(base64.b64decode(data["pdf_base64"]))
            print(f"  PDF: {pdf_path}")
    else:
        print(f"  ERROR: {data.get('error')}")
        resultados[caso_id] = data

    return data


def main():
    print("=" * 60)
    print("SET DE PRUEBAS SII — GRUPO TRESTRES SPA (77829149-5)")
    print("Atención: 4757724 (básico)")
    print("=" * 60)

    # ══════════════════════════════════════════════════════
    # SET BÁSICO (Atención 4757724)
    # ══════════════════════════════════════════════════════

    # CASO 4757724-1: Factura simple, 2 items afectos
    emitir("4757724-1", {
        "tipo_dte": 33,
        "items": [
            {"nombre": "Cajón AFECTO", "cantidad": 168, "precio_unitario": 3507},
            {"nombre": "Relleno AFECTO", "cantidad": 71, "precio_unitario": 5841},
        ],
    })

    # CASO 4757724-2: Factura con descuento por línea
    emitir("4757724-2", {
        "tipo_dte": 33,
        "items": [
            {"nombre": "Pañuelo AFECTO", "cantidad": 762, "precio_unitario": 5900, "descuento_pct": 10},
            {"nombre": "ITEM 2 AFECTO", "cantidad": 707, "precio_unitario": 4951, "descuento_pct": 23},
        ],
    })

    # CASO 4757724-3: Factura mixta (afectos + exento)
    emitir("4757724-3", {
        "tipo_dte": 33,
        "items": [
            {"nombre": "Pintura B&W AFECTO", "cantidad": 64, "precio_unitario": 6896},
            {"nombre": "ITEM 2 AFECTO", "cantidad": 237, "precio_unitario": 4029},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 1, "precio_unitario": 35296, "exento": True},
        ],
    })

    # CASO 4757724-4: Factura con descuento global 22% sobre afectos
    emitir("4757724-4", {
        "tipo_dte": 33,
        "items": [
            {"nombre": "ITEM 1 AFECTO", "cantidad": 416, "precio_unitario": 5946},
            {"nombre": "ITEM 2 AFECTO", "cantidad": 176, "precio_unitario": 7242},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 2, "precio_unitario": 6833, "exento": True},
        ],
        "descuentos_globales": [
            {
                "tipo": "D",
                "descripcion": "Descuento global 22%",
                "tipo_valor": "%",
                "valor": 22,
            }
        ],
    })

    # CASO 4757724-5: NC que corrige giro del receptor (ref caso 1)
    caso1 = resultados.get("4757724-1", {})
    folio_caso1 = caso1.get("folio", 1)
    emitir("4757724-5", {
        "tipo_dte": 61,
        "items": [
            {"nombre": "Cajón AFECTO", "cantidad": 168, "precio_unitario": 3507},
            {"nombre": "Relleno AFECTO", "cantidad": 71, "precio_unitario": 5841},
        ],
        "referencias": [{
            "tipo_doc": 33,
            "folio": folio_caso1,
            "fecha": time.strftime("%Y-%m-%d"),
            "razon": "CORRIGE GIRO DEL RECEPTOR",
            "codigo": 2,
        }],
    })

    # CASO 4757724-6: NC por devolución parcial (ref caso 2)
    caso2 = resultados.get("4757724-2", {})
    folio_caso2 = caso2.get("folio", 2)
    emitir("4757724-6", {
        "tipo_dte": 61,
        "items": [
            {"nombre": "Pañuelo AFECTO", "cantidad": 280, "precio_unitario": 5900, "descuento_pct": 10},
            {"nombre": "ITEM 2 AFECTO", "cantidad": 479, "precio_unitario": 4951, "descuento_pct": 23},
        ],
        "referencias": [{
            "tipo_doc": 33,
            "folio": folio_caso2,
            "fecha": time.strftime("%Y-%m-%d"),
            "razon": "DEVOLUCION DE MERCADERIAS",
            "codigo": 3,
        }],
    })

    # CASO 4757724-7: NC que anula factura completa (ref caso 3)
    caso3 = resultados.get("4757724-3", {})
    folio_caso3 = caso3.get("folio", 3)
    emitir("4757724-7", {
        "tipo_dte": 61,
        "items": [
            {"nombre": "Pintura B&W AFECTO", "cantidad": 64, "precio_unitario": 6896},
            {"nombre": "ITEM 2 AFECTO", "cantidad": 237, "precio_unitario": 4029},
            {"nombre": "ITEM 3 SERVICIO EXENTO", "cantidad": 1, "precio_unitario": 35296, "exento": True},
        ],
        "referencias": [{
            "tipo_doc": 33,
            "folio": folio_caso3,
            "fecha": time.strftime("%Y-%m-%d"),
            "razon": "ANULA FACTURA",
            "codigo": 1,
        }],
    })

    # CASO 4757724-8: ND que anula la NC del caso 5
    caso5 = resultados.get("4757724-5", {})
    folio_caso5 = caso5.get("folio", 1)
    emitir("4757724-8", {
        "tipo_dte": 56,
        "items": [
            {"nombre": "Cajón AFECTO", "cantidad": 168, "precio_unitario": 3507},
            {"nombre": "Relleno AFECTO", "cantidad": 71, "precio_unitario": 5841},
        ],
        "referencias": [{
            "tipo_doc": 61,
            "folio": folio_caso5,
            "fecha": time.strftime("%Y-%m-%d"),
            "razon": "ANULA NOTA DE CREDITO ELECTRONICA",
            "codigo": 1,
        }],
    })

    # ══════════════════════════════════════════════════════
    # RESUMEN
    # ══════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("RESUMEN")
    print("=" * 60)
    ok_count = sum(1 for r in resultados.values() if r.get("ok"))
    err_count = sum(1 for r in resultados.values() if not r.get("ok"))
    print(f"  Emitidos: {ok_count}/{len(resultados)}")
    if err_count:
        print(f"  Errores: {err_count}")
    print(f"\n  PDFs guardados en: {PDF_DIR}")

    for caso_id, data in sorted(resultados.items()):
        status = "OK" if data.get("ok") else "ERROR"
        folio = data.get("folio", "?")
        total = data.get("monto_total", "?")
        track = data.get("track_id", "?")
        print(f"  {caso_id}: {status} — Folio {folio}, Total ${total}, Track {track}")


if __name__ == "__main__":
    main()
