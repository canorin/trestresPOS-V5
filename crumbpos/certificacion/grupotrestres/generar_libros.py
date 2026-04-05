"""
Genera Libro de Ventas y Libro de Compras — GRUPO TRESTRES SPA (77829149-5).

- Libro de Ventas: 4757725  (lee automáticamente los DTEs generados)
- Libro de Compras: 4757726 (datos fijos del set del SII)

Ejecutar DESPUÉS de procesar_set_pruebas.py.
"""
import sys
import base64
from datetime import datetime
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "grupotrestres"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from lxml import etree
from crumbpos.core.firma.sii_firma import Firma

PFX_PATH = COMPANY_DIR / "certificado" / "17586255-2.pfx"
PFX_PASSWORD = "2656"

SII_NS = "http://www.sii.cl/SiiDte"

PERIODO = datetime.now().strftime("%Y-%m")
FECHA_HOY = datetime.now().strftime("%Y-%m-%d")
TIMESTAMP = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

RUT_EMISOR = "77829149-5"
RUT_ENVIA = "17586255-2"
FCHRESOL = "2026-03-30"
NRORESOL = 0

OUTPUT_DIR = COMPANY_DIR / "output"
BASICO_DIR = OUTPUT_DIR / "basico"


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


def _int(val):
    """Convierte texto de nodo XML a int, 0 si vacío."""
    return int(val) if val and val.strip() else 0


def _leer_dtes_basico():
    """Lee todos los DTEs del set básico y extrae sus datos para el libro de ventas."""
    ns = SII_NS
    dtes = []
    for xml_path in sorted(BASICO_DIR.glob("DTE_T*.xml")):
        tree = etree.parse(str(xml_path))
        root = tree.getroot()

        # Navegar: DTE/Documento/Encabezado
        doc = root.find(f"{{{ns}}}Documento") or root.find("Documento")
        if doc is None:
            # puede estar dentro de DTE
            dte_el = root.find(f"{{{ns}}}DTE") or root
            doc = dte_el.find(f"{{{ns}}}Documento") or dte_el.find("Documento")
        if doc is None:
            print(f"  WARN: no se encontró Documento en {xml_path.name}")
            continue

        enc = doc.find(f"{{{ns}}}Encabezado") or doc.find("Encabezado")
        id_doc = enc.find(f"{{{ns}}}IdDoc") or enc.find("IdDoc")
        totales = enc.find(f"{{{ns}}}Totales") or enc.find("Totales")

        def txt(el, tag):
            node = el.find(f"{{{ns}}}{tag}") or el.find(tag)
            return node.text if node is not None else None

        tipo_dte = int(txt(id_doc, "TipoDTE"))
        folio = int(txt(id_doc, "Folio"))

        mnt_neto = _int(txt(totales, "MntNeto"))
        mnt_exe = _int(txt(totales, "MntExe"))
        iva = _int(txt(totales, "IVA"))
        mnt_total = _int(txt(totales, "MntTotal"))

        # Referencia al documento origen (para NC y ND)
        refs = doc.findall(f"{{{ns}}}Referencia") or doc.findall("Referencia")
        tpo_doc_ref = None
        folio_doc_ref = None
        for ref in refs:
            tdr = txt(ref, "TpoDocRef")
            fdr = txt(ref, "FolioRef")
            if tdr and tdr.isdigit():
                tpo_doc_ref = int(tdr)
                folio_doc_ref = int(fdr) if fdr else None
                break

        dtes.append({
            "TpoDoc": tipo_dte,
            "NroDoc": folio,
            "MntExe": mnt_exe,
            "MntNeto": mnt_neto,
            "MntIVA": iva,
            "MntTotal": mnt_total,
            "TpoDocRef": tpo_doc_ref,
            "FolioDocRef": folio_doc_ref,
        })
        print(f"    {xml_path.name}: T{tipo_dte} F{folio} Neto={mnt_neto} Exe={mnt_exe} Total={mnt_total}")

    dtes.sort(key=lambda d: (d["TpoDoc"], d["NroDoc"]))
    return dtes


# ==================== LIBRO DE VENTAS (4757725) ====================

def generar_libro_ventas():
    """Genera el XML del Libro de Ventas (4757725).

    Usa los documentos del Set Básico (4757724):
    - Caso 1: T33 F9  - Factura simple      - Neto: 1003887
    - Caso 2: T33 F14 - Factura con dctos   - Neto: 6741495
    - Caso 3: T33 F15 - Factura mixta       - Neto: 1396217, Exe: 35296
    - Caso 4: T33 F16 - Factura dcto global - Neto: 2923540, Exe: 13666
    - Caso 5: T61 F24 - NC corrige giro (MntTotal=0)
    - Caso 6: T61 F25 - NC devolucion       - Neto: 3312877
    - Caso 7: T61 F26 - NC anula factura    - Neto: 1396217, Exe: 35296
    - Caso 8: T56 F8  - ND anula NC (MntTotal=0)
    """
    def iva(neto):
        return (neto * 19 + 50) // 100

    entries = [
        {  # Caso 1: T33 F9 - Factura simple
            "TpoDoc": 33, "NroDoc": 9,
            "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntExe": 0, "MntNeto": 1003887,
            "MntIVA": iva(1003887),
            "MntTotal": 1003887 + iva(1003887),
        },
        {  # Caso 2: T33 F14 - Factura con descuentos
            "TpoDoc": 33, "NroDoc": 14,
            "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntExe": 0, "MntNeto": 6741495,
            "MntIVA": iva(6741495),
            "MntTotal": 6741495 + iva(6741495),
        },
        {  # Caso 3: T33 F15 - Factura mixta (afecto + exento)
            "TpoDoc": 33, "NroDoc": 15,
            "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntExe": 35296, "MntNeto": 1396217,
            "MntIVA": iva(1396217),
            "MntTotal": 1396217 + 35296 + iva(1396217),
        },
        {  # Caso 4: T33 F16 - Factura descuento global
            "TpoDoc": 33, "NroDoc": 16,
            "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntExe": 13666, "MntNeto": 2923540,
            "MntIVA": iva(2923540),
            "MntTotal": 2923540 + 13666 + iva(2923540),
        },
        {  # Caso 5: T61 F24 - NC corrige giro
            "TpoDoc": 61, "NroDoc": 24,
            "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "TpoDocRef": 33, "FolioDocRef": 9,
            "MntExe": 0, "MntNeto": 0, "MntIVA": 0, "MntTotal": 0,
        },
        {  # Caso 6: T61 F25 - NC devolucion parcial
            "TpoDoc": 61, "NroDoc": 25,
            "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "TpoDocRef": 33, "FolioDocRef": 14,
            "MntExe": 0, "MntNeto": 3312877,
            "MntIVA": iva(3312877),
            "MntTotal": 3312877 + iva(3312877),
        },
        {  # Caso 7: T61 F26 - NC anula factura
            "TpoDoc": 61, "NroDoc": 26,
            "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "TpoDocRef": 33, "FolioDocRef": 15,
            "MntExe": 35296, "MntNeto": 1396217,
            "MntIVA": iva(1396217),
            "MntTotal": 1396217 + 35296 + iva(1396217),
        },
        {  # Caso 8: T56 F8 - ND anula NC
            "TpoDoc": 56, "NroDoc": 8,
            "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "TpoDocRef": 61, "FolioDocRef": 24,
            "MntExe": 0, "MntNeto": 0, "MntIVA": 0, "MntTotal": 0,
        },
    ]

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
        if e.get("TpoDocRef"):
            det += f"<TpoDocRef>{e['TpoDocRef']}</TpoDocRef>"
        if e.get("FolioDocRef"):
            det += f"<FolioDocRef>{e['FolioDocRef']}</FolioDocRef>"
        det += f"<MntExe>{e['MntExe']}</MntExe>"
        det += f"<MntNeto>{e['MntNeto']}</MntNeto>"
        det += f"<MntIVA>{e['MntIVA']}</MntIVA>"
        det += f"<MntTotal>{e['MntTotal']}</MntTotal>"
        det += "</Detalle>\n"
        detalles_xml += det

    from collections import OrderedDict
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
<FolioNotificacion>4757725</FolioNotificacion>
</Caratula>
{resumen_xml}
{detalles_xml}<TmstFirma>{TIMESTAMP}</TmstFirma>
</EnvioLibro>"""

    xml = (
        f'<LibroCompraVenta xmlns="{SII_NS}" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="{SII_NS} LibroCV_v10.xsd" '
        f'version="1.0">\n{envio_libro}</LibroCompraVenta>'
    )
    return xml, libro_id


# ==================== LIBRO DE COMPRAS (4757726) ====================

def generar_libro_compras():
    """Genera el XML del Libro de Compras según set 4757726.

    Datos exactos del set SII:
    1. FACTURA 234               - Giro con derecho a crédito     - MntAfecto: 12294
    2. FACTURA ELECTRONICA 32    - Giro con derecho a crédito     - MntExe: 8340, MntAfecto: 5134
    3. FACTURA 781               - IVA uso común (factor 0.60)    - MntAfecto: 29677
    4. NOTA DE CREDITO 451       - Descuento a Factura 234        - MntAfecto: 2660
    5. FACTURA ELECTRONICA 67    - Entrega gratuita del proveedor - MntAfecto: 9431
    6. FACTURA DE COMPRA ELEC. 9 - Retención total IVA            - MntAfecto: 9277
    7. NOTA DE CREDITO 211       - Descuento a Factura Elec. 32   - MntAfecto: 3173
    """
    def iva(neto):
        return (neto * 19 + 50) // 100

    entries = [
        {  # 1. Factura 234 — del giro con derecho a crédito
            "TpoDoc": 30, "NroDoc": 234,
            "TpoImp": 1, "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntNeto": 12294,
            "MntIVA": iva(12294),
            "MntTotal": 12294 + iva(12294),
        },
        {  # 2. Factura Electrónica 32 — del giro con derecho a crédito
            "TpoDoc": 33, "NroDoc": 32,
            "TpoImp": 1, "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntExe": 8340, "MntNeto": 5134,
            "MntIVA": iva(5134),
            "MntTotal": 8340 + 5134 + iva(5134),
        },
        {  # 3. Factura 781 — IVA uso común (factor 0.60)
            "TpoDoc": 30, "NroDoc": 781,
            "TpoImp": 1, "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntNeto": 29677,
            "IVAUsoComun": iva(29677),
            "MntTotal": 29677 + iva(29677),
        },
        {  # 4. Nota de Crédito 451 — descuento a Factura 234
            "TpoDoc": 60, "NroDoc": 451,
            "TpoImp": 1, "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntNeto": 2660,
            "MntIVA": iva(2660),
            "MntTotal": 2660 + iva(2660),
        },
        {  # 5. Factura Electrónica 67 — entrega gratuita
            "TpoDoc": 33, "NroDoc": 67,
            "TpoImp": 1, "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntNeto": 9431,
            "IVANoRec": {"CodIVANoRec": 4, "MntIVANoRec": iva(9431)},
            "MntTotal": 9431 + iva(9431),
        },
        {  # 6. Factura de Compra Electrónica 9 — retención total IVA
            "TpoDoc": 46, "NroDoc": 9,
            "TpoImp": 1, "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntNeto": 9277,
            "MntIVA": iva(9277),
            "OtrosImp": {"CodImp": 15, "TasaImp": 19, "MntImp": iva(9277)},
            "IVARetTotal": iva(9277),
            "MntTotal": 9277,  # Neto + IVA - IVARetTotal = solo neto
        },
        {  # 7. Nota de Crédito 211 — descuento a Factura Electrónica 32
            "TpoDoc": 60, "NroDoc": 211,
            "TpoImp": 1, "TasaImp": 19, "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR, "RznSoc": "GRUPO TRESTRES SPA",
            "MntNeto": 3173,
            "MntIVA": iva(3173),
            "MntTotal": 3173 + iva(3173),
        },
    ]

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

    from collections import OrderedDict
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

    resumen_xml = "<ResumenPeriodo>\n"
    for tpo, r in sorted(resumen_por_tipo.items()):
        resumen_xml += "<TotalesPeriodo>"
        resumen_xml += f"<TpoDoc>{r['TpoDoc']}</TpoDoc>"
        resumen_xml += f"<TpoImp>1</TpoImp>"
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
<FolioNotificacion>4757726</FolioNotificacion>
</Caratula>
{resumen_xml}
{detalles_xml}<TmstFirma>{TIMESTAMP}</TmstFirma>
</EnvioLibro>"""

    xml = (
        f'<LibroCompraVenta xmlns="{SII_NS}" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="{SII_NS} LibroCV_v10.xsd" '
        f'version="1.0">\n{envio_libro}</LibroCompraVenta>'
    )
    return xml, libro_id


# ==================== MAIN ====================

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Cargando certificado digital...")
    firma = cargar_firma()
    print(f"  RUT firmante: {firma.rut_firmante}")
    print("  OK\n")

    for nombre, generador, tipo_firma in [
        ("LIBRODEVENTAS", generar_libro_ventas, "libro"),
        ("LIBRODECOMPRAS", generar_libro_compras, "libro"),
    ]:
        print("=" * 60)
        print(nombre)
        print("=" * 60)

        xml_str, libro_id = generador()

        path_sf = OUTPUT_DIR / f"{nombre}.xml"
        with open(path_sf, "w", encoding="ISO-8859-1") as f:
            f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_str)
        print(f"  XML generado: {path_sf.name}")

        signed = firma.firmar(xml_str, libro_id, type=tipo_firma)
        if not signed:
            print(f"  ERROR firmando: {firma.errores}")
            continue
        print(f"  Firmado OK")

        path_f = OUTPUT_DIR / f"{nombre}_firmado.xml"
        with open(path_f, "w", encoding="ISO-8859-1") as f:
            f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed)
        print(f"  -> {path_f.name}\n")

    print("=" * 60)
    print("LIBROS GENERADOS Y FIRMADOS")
    print("=" * 60)
    print("Siguiente paso: ejecutar enviar_sii.py todo")


if __name__ == "__main__":
    main()
