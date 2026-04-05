"""
Genera Libro de Compras y Libro de Guías para certificación SII.

Libro de Compras: N° Atención 4753444
Libro de Guías: N° Atención 4753445

Usa la librería facturacion_electronica para firmar.
"""
import sys
import base64
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

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


def cargar_firma():
    """Carga la firma electrónica."""
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


# ==================== LIBRO DE COMPRAS (4753444) ====================

def generar_libro_compras():
    """Genera el XML del Libro de Compras según set 4753444.

    Datos del set:
    1. FACTURA 234 - Del giro con derecho a crédito - MntAfecto: 10163
    2. FACTURA ELECTRONICA 32 - Del giro con derecho a crédito - MntExe: 8221, MntAfecto: 4804
    3. FACTURA 781 - IVA uso común (factor 0.60) - MntAfecto: 29651
    4. NOTA DE CREDITO 451 - Descuento a Factura 234 - MntAfecto: 2646
    5. FACTURA ELECTRONICA 67 - Entrega gratuita - MntAfecto: 9291
    6. FACTURA DE COMPRA ELECTRONICA 9 - Retención total IVA - MntAfecto: 9206
    7. NOTA DE CREDITO 211 - Descuento a Factura Electrónica 32 - MntAfecto: 2867
    """
    # Calcular IVAs
    entries = [
        {  # 1. Factura 234 - del giro con crédito
            "TpoDoc": 30, "NroDoc": 234,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 10163,
            "MntIVA": (10163 * 19 + 50) // 100,  # 1931
            "MntTotal": 10163 + (10163 * 19 + 50) // 100,  # 12094
        },
        {  # 2. Factura Electrónica 32 - del giro con crédito
            "TpoDoc": 33, "NroDoc": 32,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntExe": 8221,
            "MntNeto": 4804,
            "MntIVA": (4804 * 19 + 50) // 100,  # 913
            "MntTotal": 8221 + 4804 + (4804 * 19 + 50) // 100,  # 13938
        },
        {  # 3. Factura 781 - IVA uso común
            "TpoDoc": 30, "NroDoc": 781,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 29651,
            "IVAUsoComun": (29651 * 19 + 50) // 100,  # 5634
            "MntTotal": 29651 + (29651 * 19 + 50) // 100,  # 35285
        },
        {  # 4. Nota de Crédito 451 - descuento a Factura 234
            "TpoDoc": 60, "NroDoc": 451,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 2646,
            "MntIVA": (2646 * 19 + 50) // 100,  # 503
            "MntTotal": 2646 + (2646 * 19 + 50) // 100,  # 3149
        },
        {  # 5. Factura Electrónica 67 - entrega gratuita (IVA no recuperable)
            "TpoDoc": 33, "NroDoc": 67,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 9291,
            "IVANoRec": {"CodIVANoRec": 4, "MntIVANoRec": (9291 * 19 + 50) // 100},  # 1765 - Cod 4=Entrega gratuita
            "MntTotal": 9291 + (9291 * 19 + 50) // 100,  # 11056
        },
        {  # 6. Factura de Compra Electrónica 9 - retención total IVA
            # IVA retenido total: OtrosImp CodImp=15 + IVARetTotal
            "TpoDoc": 46, "NroDoc": 9,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 9206,
            "MntIVA": (9206 * 19 + 50) // 100,  # 1749
            "OtrosImp": {"CodImp": 15, "TasaImp": 19, "MntImp": (9206 * 19 + 50) // 100},  # 1749
            "IVARetTotal": (9206 * 19 + 50) // 100,  # 1749
            "MntTotal": 9206,  # Neto + IVA - IVARetTotal
        },
        {  # 7. Nota de Crédito 211 - descuento a Factura Electrónica 32
            "TpoDoc": 60, "NroDoc": 211,
            "TpoImp": 1, "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntNeto": 2867,
            "MntIVA": (2867 * 19 + 50) // 100,  # 545
            "MntTotal": 2867 + (2867 * 19 + 50) // 100,  # 3412
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
        # IVA normal (no uso común, no no recuperable)
        if e.get("MntIVA") and not e.get("IVAUsoComun") and not e.get("IVANoRec"):
            r["TotMntIVA"] += e["MntIVA"]
        # IVA No Recuperable
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
        # IVA Uso Común
        if e.get("IVAUsoComun"):
            r["TotOpIVAUsoComun"] = r.get("TotOpIVAUsoComun", 0) + 1
            r["TotIVAUsoComun"] = r.get("TotIVAUsoComun", 0) + e["IVAUsoComun"]
            r["FctProp"] = 0.60
            r["TotCredIVAUsoComun"] = round(
                r.get("TotIVAUsoComun", 0) * 0.60
            )
        # Otros Impuestos (ej: CodImp 15 = IVA Retenido Total)
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
        # IVA Retenido Total
        if e.get("IVARetTotal"):
            r["TotOpIVARetTotal"] = r.get("TotOpIVARetTotal", 0) + 1
            r["TotIVARetTotal"] = r.get("TotIVARetTotal", 0) + e["IVARetTotal"]
        r["TotMntTotal"] += e["MntTotal"]

    # --- Construir ResumenPeriodo XML ---
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

    # --- Construir EnvioLibro ---
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
<FolioNotificacion>4753444</FolioNotificacion>
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


# ==================== LIBRO DE GUÍAS (4753445) ====================

def generar_libro_guias(folio_g1, folio_g2, folio_g3,
                        monto_neto_g2, iva_g2, total_g2,
                        monto_neto_g3, iva_g3, total_g3):
    """Genera el XML del Libro de Guías según set 4753445.

    Instrucciones del set:
    - Caso 1: Traslado interno (TpoOper=5)
    - Caso 2: Guía que se facturó en el periodo (TpoOper=1, venta)
    - Caso 3: Guía anulada (Anulado=2)
    """
    receptor_interno = {
        "RUTDoc": RUT_EMISOR,
        "RznSoc": "TRESTRES PUBLICIDAD SPA",
    }
    receptor_venta = {
        "RUTDoc": RUT_EMISOR,  # En cert, receptor = emisor
        "RznSoc": "TRESTRES PUBLICIDAD SPA",
    }

    # --- Detalles ---
    detalles_xml = ""

    # Caso 1: Traslado interno (sin montos)
    detalles_xml += f"""<Detalle>
<Folio>{folio_g1}</Folio>
<TpoOper>5</TpoOper>
<FchDoc>{FECHA_HOY}</FchDoc>
<RUTDoc>{receptor_interno['RUTDoc']}</RUTDoc>
<RznSoc>{receptor_interno['RznSoc']}</RznSoc>
<MntTotal>0</MntTotal>
</Detalle>
"""

    # Caso 2: Venta facturada en el periodo
    detalles_xml += f"""<Detalle>
<Folio>{folio_g2}</Folio>
<TpoOper>1</TpoOper>
<FchDoc>{FECHA_HOY}</FchDoc>
<RUTDoc>{receptor_venta['RUTDoc']}</RUTDoc>
<RznSoc>{receptor_venta['RznSoc']}</RznSoc>
<MntNeto>{monto_neto_g2}</MntNeto>
<TasaImp>19</TasaImp>
<IVA>{iva_g2}</IVA>
<MntTotal>{total_g2}</MntTotal>
</Detalle>
"""

    # Caso 3: Guía anulada
    detalles_xml += f"""<Detalle>
<Folio>{folio_g3}</Folio>
<Anulado>2</Anulado>
<TpoOper>2</TpoOper>
<FchDoc>{FECHA_HOY}</FchDoc>
<RUTDoc>{receptor_venta['RUTDoc']}</RUTDoc>
<RznSoc>{receptor_venta['RznSoc']}</RznSoc>
<MntNeto>{monto_neto_g3}</MntNeto>
<TasaImp>19</TasaImp>
<IVA>{iva_g3}</IVA>
<MntTotal>{total_g3}</MntTotal>
</Detalle>
"""

    # --- ResumenPeriodo ---
    # TotGuiaAnulada = 1 (caso 3)
    # TotGuiaVenta = 1 (caso 2, TpoOper 1=venta)
    # TotTraslado: TpoTraslado=5, CantGuia=1, MntGuia=0
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

    # --- Construir EnvioLibro ---
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
<FolioNotificacion>4753445</FolioNotificacion>
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
    output_dir = settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Cargando certificado digital...")
    firma = cargar_firma()
    print(f"  RUT firmante: {firma.rut_firmante}")
    print("  OK\n")

    # ===== LIBRO DE COMPRAS =====
    print("=" * 60)
    print("LIBRO DE COMPRAS - 4753444")
    print("=" * 60)

    xml_compras, id_compras = generar_libro_compras()

    # Guardar sin firmar
    path_sin_firmar = output_dir / "LibroCompras.xml"
    with open(path_sin_firmar, "w", encoding="ISO-8859-1") as f:
        f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_compras)
    print(f"  XML generado: {path_sin_firmar.name}")

    # Firmar
    signed_compras = firma.firmar(xml_compras, id_compras, type="libro")
    if not signed_compras:
        print(f"  ERROR firmando libro compras: {firma.errores}")
        return
    print("  Libro de Compras firmado OK")

    path_firmado = output_dir / "LibroCompras_firmado.xml"
    with open(path_firmado, "w", encoding="ISO-8859-1") as f:
        f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed_compras)
    print(f"  Firmado: {path_firmado.name}")

    # ===== LIBRO DE GUÍAS =====
    print(f"\n{'=' * 60}")
    print("LIBRO DE GUIAS - 4753445")
    print("=" * 60)

    # Datos de las guías procesadas (folios 44, 45, 46)
    # Caso 2: F45 - Neto: 139086, IVA: round(139086*0.19)=26426, Total: 165512
    # Caso 3: F46 - Neto: 168146, IVA: round(168146*0.19)=31948, Total: 200094
    folio_g1 = 47
    folio_g2 = 48
    folio_g3 = 49
    monto_neto_g2 = 139086
    iva_g2 = 26426
    total_g2 = 165512
    monto_neto_g3 = 168146
    iva_g3 = 31948
    total_g3 = 200094

    xml_guias, id_guias = generar_libro_guias(
        folio_g1, folio_g2, folio_g3,
        monto_neto_g2, iva_g2, total_g2,
        monto_neto_g3, iva_g3, total_g3,
    )

    # Guardar sin firmar
    path_sin_firmar = output_dir / "LibroGuias.xml"
    with open(path_sin_firmar, "w", encoding="ISO-8859-1") as f:
        f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_guias)
    print(f"  XML generado: {path_sin_firmar.name}")

    # Firmar
    signed_guias = firma.firmar(xml_guias, id_guias, type="libro_guia")
    if not signed_guias:
        print(f"  ERROR firmando libro guías: {firma.errores}")
        return
    print("  Libro de Guías firmado OK")

    path_firmado = output_dir / "LibroGuias_firmado.xml"
    with open(path_firmado, "w", encoding="ISO-8859-1") as f:
        f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed_guias)
    print(f"  Firmado: {path_firmado.name}")

    # ===== RESUMEN =====
    print(f"\n{'=' * 60}")
    print("RESUMEN")
    print("=" * 60)
    print(f"  Libro de Compras: {output_dir / 'LibroCompras_firmado.xml'}")
    print(f"  Libro de Guías: {output_dir / 'LibroGuias_firmado.xml'}")
    print()
    print("  Para enviar al SII, ejecutar enviar_libros.py")


# ==================== LIBRO DE VENTAS (4753443) ====================

def generar_libro_ventas():
    """Genera el XML del Libro de Ventas según set 4753443.

    Usa los documentos del Set Básico (4753440) ya aceptados:
    - Caso 1: T33 F35 - Factura simple
    - Caso 2: T33 F36 - Factura con dctos por ítem
    - Caso 3: T33 F37 - Factura mixta (afecto + exento)
    - Caso 4: T33 F38 - Factura con dcto global
    - Caso 5: T61 F34 - NC corrige giro (MntTotal=0)
    - Caso 6: T61 F35 - NC devolución de mercaderías
    - Caso 7: T61 F36 - NC anula factura (ref caso 3)
    - Caso 8: T56 F22 - ND anula NC (ref caso 5, MntTotal=0)
    """
    entries = [
        {  # Caso 1: Factura T33 F35
            "TpoDoc": 33, "NroDoc": 35,
            "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntExe": 0,
            "MntNeto": 722506,
            "MntIVA": (722506 * 19 + 50) // 100,  # 137276
            "MntTotal": 722506 + (722506 * 19 + 50) // 100,  # 859782
        },
        {  # Caso 2: Factura T33 F36
            "TpoDoc": 33, "NroDoc": 36,
            "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntExe": 0,
            "MntNeto": 4291654,
            "MntIVA": (4291654 * 19 + 50) // 100,  # 815414
            "MntTotal": 4291654 + (4291654 * 19 + 50) // 100,  # 5107068
        },
        {  # Caso 3: Factura T33 F37 (mixta)
            "TpoDoc": 33, "NroDoc": 37,
            "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntExe": 35119,
            "MntNeto": 1018950,
            "MntIVA": (1018950 * 19 + 50) // 100,  # 193600 o 193601
            "MntTotal": 1018950 + 35119 + (1018950 * 19 + 50) // 100,  # 1247669
        },
        {  # Caso 4: Factura T33 F38 (dcto global)
            "TpoDoc": 33, "NroDoc": 38,
            "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "MntExe": 13628,
            "MntNeto": 1811887,
            "MntIVA": (1811887 * 19 + 50) // 100,  # 344258 o 344259
            "MntTotal": 1811887 + 13628 + (1811887 * 19 + 50) // 100,  # 2169774
        },
        {  # Caso 5: NC T61 F34 - corrige giro (MntTotal=0)
            "TpoDoc": 61, "NroDoc": 34,
            "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "TpoDocRef": 33, "FolioDocRef": 35,
            "MntExe": 0,
            "MntNeto": 0,
            "MntIVA": 0,
            "MntTotal": 0,
        },
        {  # Caso 6: NC T61 F35 - devolución
            "TpoDoc": 61, "NroDoc": 35,
            "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "TpoDocRef": 33, "FolioDocRef": 36,
            "MntExe": 0,
            "MntNeto": 2097085,
            "MntIVA": (2097085 * 19 + 50) // 100,  # 398446
            "MntTotal": 2097085 + (2097085 * 19 + 50) // 100,  # 2495531
        },
        {  # Caso 7: NC T61 F36 - anula factura caso 3
            "TpoDoc": 61, "NroDoc": 36,
            "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "TpoDocRef": 33, "FolioDocRef": 37,
            "MntExe": 35119,
            "MntNeto": 1018950,
            "MntIVA": (1018950 * 19 + 50) // 100,  # 193600
            "MntTotal": 1018950 + 35119 + (1018950 * 19 + 50) // 100,  # 1247669
        },
        {  # Caso 8: ND T56 F22 - anula NC caso 5 (MntTotal=0)
            "TpoDoc": 56, "NroDoc": 22,
            "TasaImp": 19,
            "FchDoc": FECHA_HOY,
            "RUTDoc": RUT_EMISOR,
            "RznSoc": "TRESTRES PUBLICIDAD SPA",
            "TpoDocRef": 61, "FolioDocRef": 34,
            "MntExe": 0,
            "MntNeto": 0,
            "MntIVA": 0,
            "MntTotal": 0,
        },
    ]

    # --- Construir Detalles (orden XSD: TpoDoc, NroDoc, TpoImp, TasaImp, FchDoc, RUTDoc, RznSoc, TpoDocRef, FolioDocRef, MntExe, MntNeto, MntIVA, MntTotal) ---
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

    # --- Calcular ResumenPeriodo (TotalesPeriodo por TpoDoc) ---
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
<FolioNotificacion>4753443</FolioNotificacion>
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


def main_ventas():
    """Genera y firma solo el Libro de Ventas."""
    output_dir = settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Cargando certificado digital...")
    firma = cargar_firma()
    print(f"  RUT firmante: {firma.rut_firmante}")
    print("  OK\n")

    print("=" * 60)
    print("LIBRO DE VENTAS - 4753443")
    print("=" * 60)

    xml_ventas, id_ventas = generar_libro_ventas()

    path_sin_firmar = output_dir / "LibroVentas.xml"
    with open(path_sin_firmar, "w", encoding="ISO-8859-1") as f:
        f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_ventas)
    print(f"  XML generado: {path_sin_firmar.name}")

    signed_ventas = firma.firmar(xml_ventas, id_ventas, type="libro")
    if not signed_ventas:
        print(f"  ERROR firmando libro ventas: {firma.errores}")
        return
    print("  Libro de Ventas firmado OK")

    path_firmado = output_dir / "LibroVentas_firmado.xml"
    with open(path_firmado, "w", encoding="ISO-8859-1") as f:
        f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed_ventas)
    print(f"  Firmado: {path_firmado.name}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "ventas":
        main_ventas()
    else:
        main()
