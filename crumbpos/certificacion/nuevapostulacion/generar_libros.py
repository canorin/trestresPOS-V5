"""
Genera Libro de Ventas, Libro de Compras y Libro de Guias.
Nueva Postulacion trestresPOS.

Numeros de atencion (NUEVO SET - 2026-03-31):
- Libro de Ventas: 4758673
- Libro de Compras: 4758674
- Libro de Guias: 4758676

IMPORTANTE: Ejecutar procesar_set_pruebas.py PRIMERO para generar set_results.json
con los folios y montos reales del set basico y guias.
"""
import json
import sys
import base64
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "nuevapostulacion"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from lxml import etree
from facturacion_electronica.firma import Firma
from crumbpos.config import settings

PFX_PATH = settings.CERT_DIR / "17586255-2.pfx"
PFX_PASSWORD = "2656"

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

PERIODO = datetime.now().strftime("%Y-%m")
FECHA_HOY = datetime.now().strftime("%Y-%m-%d")
TIMESTAMP = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

RUT_EMISOR = "77051056-2"
RUT_ENVIA = "17586255-2"
FCHRESOL = "2026-03-26"
NRORESOL = 0

OUTPUT_DIR = COMPANY_DIR / "output"


def cargar_firma():
    pfx_data = open(PFX_PATH, "rb").read()
    firma = Firma({
        "string_firma": base64.b64encode(pfx_data).decode(),
        "string_password": PFX_PASSWORD,
        "init_signature": True,
        "rut_firmante": RUT_ENVIA,
    })
    if not firma.firma_electronica:
        raise RuntimeError(f"Error cargando certificado: {firma.errores}")
    firma.verify = False
    return firma


def _cargar_set_results():
    """Carga set_results.json generado por procesar_set_pruebas.py."""
    path = OUTPUT_DIR / "set_results.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No se encontro {path}\n"
            "Ejecutar procesar_set_pruebas.py PRIMERO para generar los sets."
        )
    with open(path) as f:
        return json.load(f)


# ==================== LIBRO DE COMPRAS (4758674) ====================

def generar_libro_compras():
    """Genera el XML del Libro de Compras segun set 4758674.

    Datos del set SIISetDePruebas770510562.txt:
    1. FACTURA 234 - Del giro con derecho a credito - MntAfecto: 21636
    2. FACTURA ELECTRONICA 32 - Del giro con derecho a credito - MntExe: 8863, MntAfecto: 6580
    3. FACTURA 781 - IVA uso comun (factor 0.60) - MntAfecto: 29789
    4. NOTA DE CREDITO 451 - Descuento a Factura 234 - MntAfecto: 2721
    5. FACTURA ELECTRONICA 67 - Entrega gratuita - MntAfecto: 10048
    6. FACTURA DE COMPRA ELECTRONICA 9 - Retencion total IVA - MntAfecto: 9586
    7. NOTA DE CREDITO 211 - Descuento a Factura Electronica 32 - MntAfecto: 4514
    """
    entries = [
        {  # 1. Factura 234
            "TpoDoc": 30, "NroDoc": 234,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 21636,
            "MntIVA": (21636 * 19 + 50) // 100,
            "MntTotal": 21636 + (21636 * 19 + 50) // 100,
        },
        {  # 2. Factura Electronica 32
            "TpoDoc": 33, "NroDoc": 32,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntExe": 8863,
            "MntNeto": 6580,
            "MntIVA": (6580 * 19 + 50) // 100,
            "MntTotal": 8863 + 6580 + (6580 * 19 + 50) // 100,
        },
        {  # 3. Factura 781 - IVA uso comun
            "TpoDoc": 30, "NroDoc": 781,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 29789,
            "IVAUsoComun": (29789 * 19 + 50) // 100,
            "MntTotal": 29789 + (29789 * 19 + 50) // 100,
        },
        {  # 4. Nota de Credito 451
            "TpoDoc": 60, "NroDoc": 451,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 2721,
            "MntIVA": (2721 * 19 + 50) // 100,
            "MntTotal": 2721 + (2721 * 19 + 50) // 100,
        },
        {  # 5. Factura Electronica 67 - entrega gratuita
            "TpoDoc": 33, "NroDoc": 67,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 10048,
            "IVANoRec": {"CodIVANoRec": 4, "MntIVANoRec": (10048 * 19 + 50) // 100},
            "MntTotal": 10048 + (10048 * 19 + 50) // 100,
        },
        {  # 6. Factura de Compra Electronica 9
            "TpoDoc": 46, "NroDoc": 9,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 9586,
            "MntIVA": (9586 * 19 + 50) // 100,
            "OtrosImp": {"CodImp": 15, "TasaImp": 19, "MntImp": (9586 * 19 + 50) // 100},
            "IVARetTotal": (9586 * 19 + 50) // 100,
            "MntTotal": 9586,  # Neto + IVA - IVARetTotal = solo neto
        },
        {  # 7. Nota de Credito 211
            "TpoDoc": 60, "NroDoc": 211,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 4514,
            "MntIVA": (4514 * 19 + 50) // 100,
            "MntTotal": 4514 + (4514 * 19 + 50) // 100,
        },
    ]

    # --- Construir Detalles ---
    detalles_xml = ""
    for e in entries:
        det = "<Detalle>"
        det += f"<TpoDoc>{e['TpoDoc']}</TpoDoc>"
        det += f"<NroDoc>{e['NroDoc']}</NroDoc>"
        if e.get("TpoImp"):
            det += f"<TpoImp>{e['TpoImp']}</TpoImp>"
        if e.get("TasaImp"):
            det += f"<TasaImp>{e['TasaImp']}</TasaImp>"
        det += f"<FchDoc>{e['FchDoc']}</FchDoc>"
        det += f"<RUTDoc>{e['RUTDoc']}</RUTDoc>"
        det += f"<RznSoc>{e['RznSoc']}</RznSoc>"
        if e.get("MntExe"):
            det += f"<MntExe>{e['MntExe']}</MntExe>"
        if e.get("MntNeto"):
            det += f"<MntNeto>{e['MntNeto']}</MntNeto>"
        if e.get("MntIVA") and not e.get("IVAUsoComun") and not e.get("IVANoRec"):
            det += f"<MntIVA>{e['MntIVA']}</MntIVA>"
        if e.get("IVANoRec"):
            det += "<IVANoRec>"
            det += f"<CodIVANoRec>{e['IVANoRec']['CodIVANoRec']}</CodIVANoRec>"
            det += f"<MntIVANoRec>{e['IVANoRec']['MntIVANoRec']}</MntIVANoRec>"
            det += "</IVANoRec>"
        if e.get("IVAUsoComun"):
            det += f"<IVAUsoComun>{e['IVAUsoComun']}</IVAUsoComun>"
        if e.get("OtrosImp"):
            oi = e["OtrosImp"]
            det += "<OtrosImp>"
            det += f"<CodImp>{oi['CodImp']}</CodImp>"
            det += f"<TasaImp>{oi['TasaImp']}</TasaImp>"
            det += f"<MntImp>{oi['MntImp']}</MntImp>"
            det += "</OtrosImp>"
        if e.get("IVARetTotal"):
            det += f"<IVARetTotal>{e['IVARetTotal']}</IVARetTotal>"
        det += f"<MntTotal>{e['MntTotal']}</MntTotal>"
        det += "</Detalle>\n"
        detalles_xml += det

    # --- Calcular ResumenPeriodo ---
    resumen_por_tipo = OrderedDict()
    for e in entries:
        tpo = e["TpoDoc"]
        if tpo not in resumen_por_tipo:
            resumen_por_tipo[tpo] = {
                "TpoDoc": tpo, "TotDoc": 0,
                "TotMntExe": 0, "TotMntNeto": 0, "TotMntIVA": 0,
                "TotMntTotal": 0,
            }
        r = resumen_por_tipo[tpo]
        r["TotDoc"] += 1
        r["TotMntExe"] += e.get("MntExe", 0)
        r["TotMntNeto"] += e.get("MntNeto", 0)
        if e.get("MntIVA") and not e.get("IVAUsoComun") and not e.get("IVANoRec"):
            r["TotMntIVA"] += e["MntIVA"]
        if e.get("IVANoRec"):
            if "TotIVANoRec" not in r:
                r["TotIVANoRec"] = []
            found = False
            for nr in r["TotIVANoRec"]:
                if nr["CodIVANoRec"] == e["IVANoRec"]["CodIVANoRec"]:
                    nr["TotOpIVANoRec"] += 1
                    nr["TotMntIVANoRec"] += e["IVANoRec"]["MntIVANoRec"]
                    found = True
            if not found:
                r["TotIVANoRec"].append({
                    "CodIVANoRec": e["IVANoRec"]["CodIVANoRec"],
                    "TotOpIVANoRec": 1,
                    "TotMntIVANoRec": e["IVANoRec"]["MntIVANoRec"],
                })
        if e.get("IVAUsoComun"):
            r["TotOpIVAUsoComun"] = r.get("TotOpIVAUsoComun", 0) + 1
            r["TotIVAUsoComun"] = r.get("TotIVAUsoComun", 0) + e["IVAUsoComun"]
            r["FctProp"] = 0.60
            r["TotCredIVAUsoComun"] = round(r.get("TotIVAUsoComun", 0) * 0.60)
        if e.get("OtrosImp"):
            oi = e["OtrosImp"]
            if "TotOtrosImp" not in r:
                r["TotOtrosImp"] = []
            found = False
            for tot_oi in r["TotOtrosImp"]:
                if tot_oi["CodImp"] == oi["CodImp"]:
                    tot_oi["TotMntImp"] += oi["MntImp"]
                    tot_oi["TotCredImp"] += oi["MntImp"]
                    found = True
            if not found:
                r["TotOtrosImp"].append({
                    "CodImp": oi["CodImp"],
                    "TotMntImp": oi["MntImp"],
                    "TotCredImp": oi["MntImp"],
                })
        if e.get("IVARetTotal"):
            r["TotOpIVARetTotal"] = r.get("TotOpIVARetTotal", 0) + 1
            r["TotIVARetTotal"] = r.get("TotIVARetTotal", 0) + e["IVARetTotal"]
        r["TotMntTotal"] += e["MntTotal"]

    # --- Construir ResumenPeriodo XML ---
    resumen_xml = "<ResumenPeriodo>\n"
    for tpo, r in sorted(resumen_por_tipo.items()):
        resumen_xml += "<TotalesPeriodo>"
        resumen_xml += f"<TpoDoc>{r['TpoDoc']}</TpoDoc>"
        resumen_xml += "<TpoImp>1</TpoImp>"
        resumen_xml += f"<TotDoc>{r['TotDoc']}</TotDoc>"
        resumen_xml += f"<TotMntExe>{r['TotMntExe']}</TotMntExe>"
        resumen_xml += f"<TotMntNeto>{r['TotMntNeto']}</TotMntNeto>"
        resumen_xml += f"<TotMntIVA>{r['TotMntIVA']}</TotMntIVA>"
        if r.get("TotIVANoRec"):
            for nr in r["TotIVANoRec"]:
                resumen_xml += "<TotIVANoRec>"
                resumen_xml += f"<CodIVANoRec>{nr['CodIVANoRec']}</CodIVANoRec>"
                resumen_xml += f"<TotOpIVANoRec>{nr['TotOpIVANoRec']}</TotOpIVANoRec>"
                resumen_xml += f"<TotMntIVANoRec>{nr['TotMntIVANoRec']}</TotMntIVANoRec>"
                resumen_xml += "</TotIVANoRec>"
        if r.get("TotOpIVAUsoComun"):
            resumen_xml += f"<TotOpIVAUsoComun>{r['TotOpIVAUsoComun']}</TotOpIVAUsoComun>"
            resumen_xml += f"<TotIVAUsoComun>{r['TotIVAUsoComun']}</TotIVAUsoComun>"
            resumen_xml += f"<FctProp>{r['FctProp']}</FctProp>"
            resumen_xml += f"<TotCredIVAUsoComun>{r['TotCredIVAUsoComun']}</TotCredIVAUsoComun>"
        if r.get("TotOtrosImp"):
            for oi in r["TotOtrosImp"]:
                resumen_xml += "<TotOtrosImp>"
                resumen_xml += f"<CodImp>{oi['CodImp']}</CodImp>"
                resumen_xml += f"<TotMntImp>{oi['TotMntImp']}</TotMntImp>"
                resumen_xml += f"<TotCredImp>{oi['TotCredImp']}</TotCredImp>"
                resumen_xml += "</TotOtrosImp>"
        if r.get("TotIVARetTotal"):
            resumen_xml += f"<TotOpIVARetTotal>{r.get('TotOpIVARetTotal', 1)}</TotOpIVARetTotal>"
            resumen_xml += f"<TotIVARetTotal>{r['TotIVARetTotal']}</TotIVARetTotal>"
        resumen_xml += f"<TotMntTotal>{r['TotMntTotal']}</TotMntTotal>"
        resumen_xml += "</TotalesPeriodo>\n"
    resumen_xml += "</ResumenPeriodo>"

    libro_id = "COMPRAS_" + PERIODO
    envio_libro = f"""<EnvioLibro ID="{libro_id}">
<Caratula>
<RutEmisorLibro>{RUT_EMISOR}</RutEmisorLibro>
<RutEnvia>{RUT_ENVIA}</RutEnvia>
<PeriodoTributario>{PERIODO}</PeriodoTributario>
<FchResol>{FCHRESOL}</FchResol>
<NroResol>{NRORESOL}</NroResol>
<TipoOperacion>COMPRA</TipoOperacion>
<TipoLibro>ESPECIAL</TipoLibro>
<TipoEnvio>TOTAL</TipoEnvio>
<FolioNotificacion>4758674</FolioNotificacion>
</Caratula>
{resumen_xml}
{detalles_xml}<TmstFirma>{TIMESTAMP}</TmstFirma>
</EnvioLibro>"""

    xml = f'''<LibroCompraVenta xmlns="http://www.sii.cl/SiiDte" \
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" \
xsi:schemaLocation="http://www.sii.cl/SiiDte LibroCV_v10.xsd" \
version="1.0">
{envio_libro}</LibroCompraVenta>'''

    return xml, libro_id


# ==================== LIBRO DE VENTAS (4758673) ====================

def generar_libro_ventas():
    """Genera el XML del Libro de Ventas segun set 4758673.

    Lee los folios y montos reales del Set Basico desde set_results.json.
    """
    sr = _cargar_set_results()
    basico = sr["basico"]
    casos = basico["casos"]
    folios = basico["folios"]

    # Mapeo: caso index -> entry info
    # Caso 1-4: T33 facturas, Caso 5-7: T61 NC, Caso 8: T56 ND
    entries = []
    for i, c in enumerate(casos):
        entry = {
            "TpoDoc": c["tipo_dte"],
            "NroDoc": c["folio"],
            "TasaImp": 19,
            "FchDoc": sr["fecha_emision"],
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntExe": c["monto_exento"],
            "MntNeto": c["monto_neto"],
            "MntIVA": c["iva"],
            "MntTotal": c["monto_total"],
        }
        # NC/ND necesitan referencia al documento original
        if i == 4:  # Caso 5: NC corrige giro -> ref T33 caso 1
            entry["TpoDocRef"] = 33
            entry["FolioDocRef"] = folios["f1"]
        elif i == 5:  # Caso 6: NC devolucion -> ref T33 caso 2
            entry["TpoDocRef"] = 33
            entry["FolioDocRef"] = folios["f2"]
        elif i == 6:  # Caso 7: NC anula -> ref T33 caso 3
            entry["TpoDocRef"] = 33
            entry["FolioDocRef"] = folios["f3"]
        elif i == 7:  # Caso 8: ND anula NC -> ref T61 caso 5
            entry["TpoDocRef"] = 61
            entry["FolioDocRef"] = folios["f5"]
        entries.append(entry)

    detalles_xml = ""
    for e in entries:
        det = "<Detalle>"
        det += f"<TpoDoc>{e['TpoDoc']}</TpoDoc>"
        det += f"<NroDoc>{e['NroDoc']}</NroDoc>"
        if e.get("TasaImp"):
            det += f"<TasaImp>{e['TasaImp']}</TasaImp>"
        det += f"<FchDoc>{e['FchDoc']}</FchDoc>"
        det += f"<RUTDoc>{e['RUTDoc']}</RUTDoc>"
        det += f"<RznSoc>{e['RznSoc']}</RznSoc>"
        if e.get("TpoDocRef") is not None:
            det += f"<TpoDocRef>{e['TpoDocRef']}</TpoDocRef>"
        if e.get("FolioDocRef") is not None:
            det += f"<FolioDocRef>{e['FolioDocRef']}</FolioDocRef>"
        det += f"<MntExe>{e['MntExe']}</MntExe>"
        det += f"<MntNeto>{e['MntNeto']}</MntNeto>"
        det += f"<MntIVA>{e['MntIVA']}</MntIVA>"
        det += f"<MntTotal>{e['MntTotal']}</MntTotal>"
        det += "</Detalle>\n"
        detalles_xml += det

    resumen_por_tipo = OrderedDict()
    for e in entries:
        tpo = e["TpoDoc"]
        if tpo not in resumen_por_tipo:
            resumen_por_tipo[tpo] = {
                "TpoDoc": tpo, "TotDoc": 0,
                "TotMntExe": 0, "TotMntNeto": 0, "TotMntIVA": 0,
                "TotMntTotal": 0,
            }
        r = resumen_por_tipo[tpo]
        r["TotDoc"] += 1
        r["TotMntExe"] += e.get("MntExe", 0)
        r["TotMntNeto"] += e.get("MntNeto", 0)
        r["TotMntIVA"] += e.get("MntIVA", 0)
        r["TotMntTotal"] += e["MntTotal"]

    resumen_xml = "<ResumenPeriodo>\n"
    for tpo, r in sorted(resumen_por_tipo.items()):
        resumen_xml += "<TotalesPeriodo>"
        resumen_xml += f"<TpoDoc>{r['TpoDoc']}</TpoDoc>"
        resumen_xml += f"<TotDoc>{r['TotDoc']}</TotDoc>"
        resumen_xml += f"<TotMntExe>{r['TotMntExe']}</TotMntExe>"
        resumen_xml += f"<TotMntNeto>{r['TotMntNeto']}</TotMntNeto>"
        resumen_xml += f"<TotMntIVA>{r['TotMntIVA']}</TotMntIVA>"
        resumen_xml += f"<TotMntTotal>{r['TotMntTotal']}</TotMntTotal>"
        resumen_xml += "</TotalesPeriodo>\n"
    resumen_xml += "</ResumenPeriodo>"

    libro_id = "VENTAS_" + PERIODO
    envio_libro = f"""<EnvioLibro ID="{libro_id}">
<Caratula>
<RutEmisorLibro>{RUT_EMISOR}</RutEmisorLibro>
<RutEnvia>{RUT_ENVIA}</RutEnvia>
<PeriodoTributario>{PERIODO}</PeriodoTributario>
<FchResol>{FCHRESOL}</FchResol>
<NroResol>{NRORESOL}</NroResol>
<TipoOperacion>VENTA</TipoOperacion>
<TipoLibro>ESPECIAL</TipoLibro>
<TipoEnvio>TOTAL</TipoEnvio>
<FolioNotificacion>4758673</FolioNotificacion>
</Caratula>
{resumen_xml}
{detalles_xml}<TmstFirma>{TIMESTAMP}</TmstFirma>
</EnvioLibro>"""

    xml = f'''<LibroCompraVenta xmlns="http://www.sii.cl/SiiDte" \
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" \
xsi:schemaLocation="http://www.sii.cl/SiiDte LibroCV_v10.xsd" \
version="1.0">
{envio_libro}</LibroCompraVenta>'''

    return xml, libro_id


# ==================== LIBRO DE GUIAS (4758676) ====================

def generar_libro_guias():
    """Genera el XML del Libro de Guias segun set 4758676.

    Lee los folios y montos reales del Set Guias desde set_results.json.

    Reglas del set:
    - Caso 2 corresponde a una guia que se facturo en el periodo (TpoOper=1)
    - Caso 3 corresponde a una guia anulada (Anulado=2)
    """
    sr = _cargar_set_results()
    guias = sr["guias"]
    casos = guias["casos"]
    folios = guias["folios"]

    folio_g1 = folios["g1"]
    folio_g2 = folios["g2"]
    folio_g3 = folios["g3"]

    # Caso 1: Traslado interno (TpoOper=5) - sin montos
    # Caso 2: Venta facturada (TpoOper=1)
    # Caso 3: Guia anulada (Anulado=2, TpoOper=2=ventas por efectuar)
    c2 = casos[1]  # guia venta
    monto_neto_g2 = c2["monto_neto"]
    iva_g2 = c2["iva"]
    total_g2 = c2["monto_total"]

    c3 = casos[2]  # guia anulada
    monto_neto_g3 = c3["monto_neto"]
    iva_g3 = c3["iva"]
    total_g3 = c3["monto_total"]

    detalles_xml = f"""<Detalle>
<Folio>{folio_g1}</Folio>
<TpoOper>5</TpoOper>
<FchDoc>{sr['fecha_emision']}</FchDoc>
<RUTDoc>{RUT_EMISOR}</RUTDoc>
<RznSoc>TRESTRES PUBLICIDAD SPA</RznSoc>
<MntTotal>0</MntTotal>
</Detalle>
<Detalle>
<Folio>{folio_g2}</Folio>
<TpoOper>1</TpoOper>
<FchDoc>{sr['fecha_emision']}</FchDoc>
<RUTDoc>{RUT_EMISOR}</RUTDoc>
<RznSoc>TRESTRES PUBLICIDAD SPA</RznSoc>
<MntNeto>{monto_neto_g2}</MntNeto>
<TasaImp>19</TasaImp>
<IVA>{iva_g2}</IVA>
<MntTotal>{total_g2}</MntTotal>
</Detalle>
<Detalle>
<Folio>{folio_g3}</Folio>
<Anulado>2</Anulado>
<TpoOper>2</TpoOper>
<FchDoc>{sr['fecha_emision']}</FchDoc>
<RUTDoc>{RUT_EMISOR}</RUTDoc>
<RznSoc>TRESTRES PUBLICIDAD SPA</RznSoc>
<MntNeto>{monto_neto_g3}</MntNeto>
<TasaImp>19</TasaImp>
<IVA>{iva_g3}</IVA>
<MntTotal>{total_g3}</MntTotal>
</Detalle>
"""

    resumen_xml = f"""<ResumenPeriodo>
<TotGuiaAnulada>1</TotGuiaAnulada>
<TotGuiaVenta>1</TotGuiaVenta>
<TotMntGuiaVta>{total_g2}</TotMntGuiaVta>
<TotTraslado>
<TpoTraslado>5</TpoTraslado>
<CantGuia>1</CantGuia>
<MntGuia>0</MntGuia>
</TotTraslado>
</ResumenPeriodo>"""

    libro_id = "GUIAS_" + PERIODO
    envio_libro = f"""<EnvioLibro ID="{libro_id}">
<Caratula>
<RutEmisorLibro>{RUT_EMISOR}</RutEmisorLibro>
<RutEnvia>{RUT_ENVIA}</RutEnvia>
<PeriodoTributario>{PERIODO}</PeriodoTributario>
<FchResol>{FCHRESOL}</FchResol>
<NroResol>{NRORESOL}</NroResol>
<TipoLibro>ESPECIAL</TipoLibro>
<TipoEnvio>TOTAL</TipoEnvio>
<FolioNotificacion>4758676</FolioNotificacion>
</Caratula>
{resumen_xml}
{detalles_xml}<TmstFirma>{TIMESTAMP}</TmstFirma>
</EnvioLibro>"""

    xml = f'''<LibroGuia xmlns="http://www.sii.cl/SiiDte" \
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" \
xsi:schemaLocation="http://www.sii.cl/SiiDte LibroGuia_v10.xsd" \
version="1.0">
{envio_libro}</LibroGuia>'''

    return xml, libro_id


# ==================== MAIN ====================

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Cargando certificado digital...")
    firma = cargar_firma()
    print(f"  RUT firmante: {firma.rut_firmante}")
    print("  OK\n")

    for nombre, generador, tipo_firma in [
        ("LIBRO DE VENTAS - 4758673", generar_libro_ventas, "libro"),
        ("LIBRO DE COMPRAS - 4758674", generar_libro_compras, "libro"),
        ("LIBRO DE GUIAS - 4758676", generar_libro_guias, "libro_guia"),
    ]:
        print("=" * 60)
        print(nombre)
        print("=" * 60)

        xml_str, libro_id = generador()
        base_name = nombre.split(" - ")[0].replace(" ", "")

        # Guardar sin firmar
        path_sf = OUTPUT_DIR / f"{base_name}.xml"
        with open(path_sf, "w", encoding="ISO-8859-1") as f:
            f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_str)
        print(f"  XML generado: {path_sf.name}")

        # Firmar
        signed = firma.firmar(xml_str, libro_id, type=tipo_firma)
        if not signed:
            print(f"  ERROR firmando: {firma.errores}")
            continue
        print(f"  Firmado OK")

        path_f = OUTPUT_DIR / f"{base_name}_firmado.xml"
        with open(path_f, "w", encoding="ISO-8859-1") as f:
            f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed)
        print(f"  -> {path_f.name}\n")

    print("=" * 60)
    print("TODOS LOS LIBROS GENERADOS Y FIRMADOS")
    print("=" * 60)


if __name__ == "__main__":
    main()
