"""Generador de Libros de Compras y Ventas Electrónicos (IECV).

Genera el XML del LibroCompraVenta según formato SII Chile.

Reglas SII críticas:
1. ALL document types in the period must appear in both Detalle AND ResumenPeriodo
2. TotalesPeriodo must include ALL fields (TotMntExe, TotMntNeto, TotMntIVA, TotMntTotal) even when 0
3. Detalle entries must include MntExe, MntNeto, MntIVA, MntTotal even when 0
4. NC (T61) and ND (T56) with references must include TpoDocRef/FolioDocRef
5. IVA = (neto * 19 + 50) // 100
"""
import re
from collections import OrderedDict
from datetime import datetime

from lxml import etree

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

# Tipos DTE afectos a IVA (llevan TasaImp=19)
TIPOS_AFECTOS = {33, 56, 61, 52, 46, 30, 60}

# Tipos que pueden tener referencias a documentos originales
TIPOS_CON_REFERENCIA = {56, 61}  # ND y NC electrónicas

# Valores permitidos para <TipoEnvio> según los XSDs oficiales del SII
# (LibroCV_v10.xsd línea ~85, LibroGuia_v10.xsd línea ~65).
#
# Semántica:
#   - TOTAL  : único envío que compone el libro (primer envío).
#   - PARCIAL: envío parcial, faltan otros.
#   - FINAL  : último envío parcial, completa el libro.
#   - AJUSTE : envío para corregir o complementar un libro previamente
#              enviado. **Es el valor obligatorio para re-envíos** cuando
#              el SII ya aceptó el libro original — enviar TOTAL de nuevo
#              produce rechazo LNC ("Tipo de Envío de Libro No Corresponde").
TIPOS_ENVIO_VALIDOS = frozenset({"TOTAL", "PARCIAL", "FINAL", "AJUSTE"})


def _validar_tipo_envio(tipo_envio: str) -> None:
    """Valida que ``tipo_envio`` esté en el conjunto permitido por el XSD SII.

    Fail fast: lanzamos ValueError antes de armar el XML para que el
    error explote en el generador (donde el contexto es claro) y no
    durante la validación XSD posterior o en la respuesta del SII.
    """
    if tipo_envio not in TIPOS_ENVIO_VALIDOS:
        permitidos = ", ".join(sorted(TIPOS_ENVIO_VALIDOS))
        raise ValueError(
            f"TipoEnvio inválido: {tipo_envio!r}. "
            f"Valores permitidos por el SII: {permitidos}."
        )


def _extraer_referencia_desde_xml(xml_firmado_b64: str | None) -> tuple[int | None, int | None]:
    """Extrae TpoDocRef y FolioRef del XML firmado del DTE.

    El xml_firmado está almacenado como base64 en la DB.
    Returns (tipo_doc_ref, folio_doc_ref) or (None, None).
    """
    if not xml_firmado_b64:
        return None, None

    try:
        import base64
        xml_bytes = base64.b64decode(xml_firmado_b64)
        xml_str = xml_bytes.decode("ISO-8859-1", errors="replace")

        # Try parsing with lxml
        try:
            root = etree.fromstring(xml_bytes)
            ns = SII_NS
            # Search for Referencia elements anywhere in the tree
            refs = root.findall(f".//{{{ns}}}Referencia")
            if not refs:
                refs = root.findall(".//Referencia")
            for ref in refs:
                # Use 'is not None' — lxml elements with no children are falsy,
                # so 'element or fallback' fails silently (FutureWarning).
                tdr_el = ref.find(f"{{{ns}}}TpoDocRef")
                if tdr_el is None:
                    tdr_el = ref.find("TpoDocRef")
                fdr_el = ref.find(f"{{{ns}}}FolioRef")
                if fdr_el is None:
                    fdr_el = ref.find("FolioRef")
                if tdr_el is not None and tdr_el.text and tdr_el.text.strip().isdigit():
                    tpo = int(tdr_el.text.strip())
                    folio = int(fdr_el.text.strip()) if fdr_el is not None and fdr_el.text else None
                    return tpo, folio
        except etree.XMLSyntaxError:
            pass

        # Fallback: regex — search within each <Referencia> block to avoid
        # matching TpoDocRef and FolioRef from different references.
        for ref_block in re.finditer(r'<Referencia>(.*?)</Referencia>', xml_str, re.DOTALL):
            block = ref_block.group(1)
            tdr_match = re.search(r'<TpoDocRef>(\d+)</TpoDocRef>', block)
            if tdr_match:
                tpo = int(tdr_match.group(1))
                fdr_match = re.search(r'<FolioRef>(\d+)</FolioRef>', block)
                folio = int(fdr_match.group(1)) if fdr_match else None
                return tpo, folio

    except Exception:
        pass

    return None, None


def generar_libro_ventas(
    dtes: list,
    empresa,
    periodo: str,
    rut_envia: str,
    folio_notificacion: int = 0,
    tipo_envio: str = "TOTAL",
) -> tuple[str, str]:
    """Genera el XML del Libro de Ventas.

    Args:
        dtes: list of DteEmitido records from DB
        empresa: Empresa model instance
        periodo: "YYYY-MM"
        rut_envia: RUT of person sending (from cert)
        folio_notificacion: 0 for production (MENSUAL), >0 for certification (ESPECIAL)
        tipo_envio: ``TOTAL`` (primer envío, default), ``PARCIAL``, ``FINAL``
            o ``AJUSTE`` (re-envío correctivo sobre un libro ya recibido
            por el SII). Ver ``TIPOS_ENVIO_VALIDOS``.

    Returns:
        (xml_string, libro_id) — xml_string is unsigned, caller must sign it.
    """
    _validar_tipo_envio(tipo_envio)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Sort DTEs by tipo_dte, then folio
    dtes_sorted = sorted(dtes, key=lambda d: (d.tipo_dte, d.folio))

    # Build Detalle entries
    entries = []
    for dte in dtes_sorted:
        mnt_exe = dte.monto_exento or 0
        mnt_neto = dte.monto_neto or 0
        mnt_iva = dte.iva or 0
        mnt_total = dte.monto_total or 0

        entry = {
            "TpoDoc": dte.tipo_dte,
            "NroDoc": dte.folio,
            "FchDoc": dte.fecha_emision.strftime("%Y-%m-%d") if hasattr(dte.fecha_emision, 'strftime') else str(dte.fecha_emision),
            "RUTDoc": dte.receptor_rut or "66666666-6",
            "RznSoc": dte.receptor_razon or "SIN RAZON SOCIAL",
            "MntExe": mnt_exe,
            "MntNeto": mnt_neto,
            "MntIVA": mnt_iva,
            "MntTotal": mnt_total,
        }

        # TasaImp for afecto types
        if dte.tipo_dte in TIPOS_AFECTOS:
            entry["TasaImp"] = 19

        # Extract references for NC/ND
        if dte.tipo_dte in TIPOS_CON_REFERENCIA:
            tpo_ref, folio_ref = _extraer_referencia_desde_xml(dte.xml_firmado)
            if tpo_ref is not None:
                entry["TpoDocRef"] = tpo_ref
            if folio_ref is not None and folio_ref > 0:
                entry["FolioDocRef"] = folio_ref

        entries.append(entry)

    # Build Detalle XML
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

    # Build ResumenPeriodo
    resumen_por_tipo = OrderedDict()
    for e in entries:
        tpo = e["TpoDoc"]
        if tpo not in resumen_por_tipo:
            resumen_por_tipo[tpo] = {
                "TpoDoc": tpo,
                "TotDoc": 0,
                "TotMntExe": 0,
                "TotMntNeto": 0,
                "TotMntIVA": 0,
                "TotMntTotal": 0,
            }
        r = resumen_por_tipo[tpo]
        r["TotDoc"] += 1
        r["TotMntExe"] += e["MntExe"]
        r["TotMntNeto"] += e["MntNeto"]
        r["TotMntIVA"] += e["MntIVA"]
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

    # Determine TipoLibro
    tipo_libro = "ESPECIAL" if folio_notificacion > 0 else "MENSUAL"

    # Build Caratula
    caratula = "<Caratula>\n"
    caratula += f"<RutEmisorLibro>{empresa.rut}</RutEmisorLibro>\n"
    caratula += f"<RutEnvia>{rut_envia}</RutEnvia>\n"
    caratula += f"<PeriodoTributario>{periodo}</PeriodoTributario>\n"
    caratula += f"<FchResol>{empresa.fecha_resolucion}</FchResol>\n"
    caratula += f"<NroResol>{empresa.numero_resolucion}</NroResol>\n"
    caratula += "<TipoOperacion>VENTA</TipoOperacion>\n"
    caratula += f"<TipoLibro>{tipo_libro}</TipoLibro>\n"
    caratula += f"<TipoEnvio>{tipo_envio}</TipoEnvio>\n"
    if tipo_libro == "ESPECIAL":
        caratula += f"<FolioNotificacion>{folio_notificacion}</FolioNotificacion>\n"
    caratula += "</Caratula>"

    # Build EnvioLibro
    libro_id = f"VENTAS_{periodo}"
    envio_libro = f'<EnvioLibro ID="{libro_id}">\n'
    envio_libro += caratula + "\n"
    envio_libro += resumen_xml + "\n"
    envio_libro += detalles_xml
    envio_libro += f"<TmstFirma>{timestamp}</TmstFirma>\n"
    envio_libro += "</EnvioLibro>"

    # Build full LibroCompraVenta
    xml = (
        f'<LibroCompraVenta xmlns="{SII_NS}" '
        f'xmlns:xsi="{XSI_NS}" '
        f'xsi:schemaLocation="{SII_NS} LibroCV_v10.xsd" '
        f'version="1.0">\n{envio_libro}</LibroCompraVenta>'
    )

    return xml, libro_id


# ── Mapping IndTraslado → TpoOper para Libro de Guías ──
_IND_TRASLADO_TO_TPO_OPER = {
    1: 1,  # operación constituye venta
    2: 2,  # ventas por efectuar
    3: 3,  # consignaciones
    4: 4,  # entrega gratuita
    5: 5,  # traslado interno
    6: 6,  # otros traslados no venta
    7: 7,  # guía de devolución
    8: 8,  # traslado para exportación
    9: 9,  # venta para exportación
}


def _extraer_ind_traslado_desde_xml(xml_firmado_b64: str | None) -> int | None:
    """Extrae IndTraslado del XML firmado de una Guía de Despacho.

    Returns el valor de IndTraslado como int, o None si no se encuentra.
    """
    if not xml_firmado_b64:
        return None

    try:
        import base64
        xml_bytes = base64.b64decode(xml_firmado_b64)
        xml_str = xml_bytes.decode("ISO-8859-1", errors="replace")

        # Try parsing with lxml
        try:
            root = etree.fromstring(xml_bytes)
            ns = SII_NS
            el = root.find(f".//{{{ns}}}IndTraslado")
            if el is None:
                el = root.find(".//IndTraslado")
            if el is not None and el.text and el.text.strip().isdigit():
                return int(el.text.strip())
        except etree.XMLSyntaxError:
            pass

        # Fallback: regex
        match = re.search(r'<IndTraslado>(\d+)</IndTraslado>', xml_str)
        if match:
            return int(match.group(1))

    except Exception:
        pass

    return None


def _extraer_referencia_guia_desde_xml(xml_firmado_b64: str | None) -> tuple[int | None, int | None, str | None]:
    """Extrae TpoDocRef, FolioDocRef y FchDocRef del XML firmado de una Guía.

    Returns (tipo_doc_ref, folio_doc_ref, fch_doc_ref) or (None, None, None).
    """
    if not xml_firmado_b64:
        return None, None, None

    try:
        import base64
        xml_bytes = base64.b64decode(xml_firmado_b64)
        xml_str = xml_bytes.decode("ISO-8859-1", errors="replace")

        try:
            root = etree.fromstring(xml_bytes)
            ns = SII_NS
            refs = root.findall(f".//{{{ns}}}Referencia")
            if not refs:
                refs = root.findall(".//Referencia")
            for ref in refs:
                tdr_el = ref.find(f"{{{ns}}}TpoDocRef") or ref.find("TpoDocRef")
                fdr_el = ref.find(f"{{{ns}}}FolioRef") or ref.find("FolioRef")
                fch_el = ref.find(f"{{{ns}}}FchRef") or ref.find("FchRef")
                tpo = int(tdr_el.text.strip()) if tdr_el is not None and tdr_el.text and tdr_el.text.strip().isdigit() else None
                folio = int(fdr_el.text.strip()) if fdr_el is not None and fdr_el.text and fdr_el.text.strip().isdigit() else None
                fch = fch_el.text.strip() if fch_el is not None and fch_el.text else None
                if tpo is not None:
                    return tpo, folio, fch
        except etree.XMLSyntaxError:
            pass

        # Fallback: regex
        tdr_match = re.search(r'<TpoDocRef>(\d+)</TpoDocRef>', xml_str)
        fdr_match = re.search(r'<FolioRef>(\d+)</FolioRef>', xml_str)
        fch_match = re.search(r'<FchRef>(\d{4}-\d{2}-\d{2})</FchRef>', xml_str)
        if tdr_match:
            tpo = int(tdr_match.group(1))
            folio = int(fdr_match.group(1)) if fdr_match else None
            fch = fch_match.group(1) if fch_match else None
            return tpo, folio, fch

    except Exception:
        pass

    return None, None, None


def generar_libro_guias(
    dtes: list,
    empresa,
    periodo: str,
    rut_envia: str,
    folio_notificacion: int = 0,
    folios_anulados: set[int] | None = None,
    tipo_envio: str = "TOTAL",
) -> tuple[str, str]:
    """Genera el XML del Libro de Guías de Despacho.

    El LibroGuia tiene estructura DIFERENTE a LibroCompraVenta:
    - No tiene ResumenPeriodo/TotalesPeriodo
    - Usa <Detalle> con Folio (no NroDoc), TpoOper, y campos propios
    - Root element es <LibroGuia> (no <LibroCompraVenta>)

    Args:
        dtes: list of DteEmitido records (tipo_dte=52) from DB
        empresa: Empresa model instance
        periodo: "YYYY-MM"
        rut_envia: RUT of person sending (from cert)
        folio_notificacion: 0 for production (MENSUAL), >0 for certification (ESPECIAL)
        folios_anulados: set opcional de folios que deben marcarse con
            ``<Anulado>2</Anulado>`` y contarse en ``TotGuiaAnulada``
            en vez de los totales de venta. Útil cuando el set de
            pruebas SII instruye "EL CASO N CORRESPONDE A UNA GUIA
            ANULADA". Override directo sobre el flag ``anulado`` del
            modelo — se tiene en cuenta cuando el DTE en sí está
            aprobado (no tenemos anulación real en BD).
        tipo_envio: ``TOTAL`` (primer envío, default), ``PARCIAL``, ``FINAL``
            o ``AJUSTE`` (re-envío correctivo sobre un libro ya recibido
            por el SII). Ver ``TIPOS_ENVIO_VALIDOS``.

    Returns:
        (xml_string, libro_id) -- xml_string is unsigned, caller must sign it.
    """
    _validar_tipo_envio(tipo_envio)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    folios_anulados = folios_anulados or set()

    # Sort by folio
    dtes_sorted = sorted(dtes, key=lambda d: d.folio)

    # Determine TipoLibro
    tipo_libro = "ESPECIAL" if folio_notificacion > 0 else "MENSUAL"

    # Build Caratula (LibroGuia has different fields than LibroCompraVenta)
    # IMPORTANTE — ``TipoOperacion`` NO va en la carátula del LibroGuia.
    # El XSD oficial ``LibroGuia_v10.xsd`` define la sequence:
    #     RutEmisorLibro → RutEnvia → PeriodoTributario → FchResol →
    #     NroResol → TipoLibro → TipoEnvio? → NroSegmento? →
    #     FolioNotificacion
    # No incluye ``TipoOperacion`` (sí está en ``LibroCV_v10.xsd`` con
    # enum COMPRA/VENTA, pero los libros son schemas distintos).
    # Emitirlo en LibroGuia produce rechazo schema-level del SII
    # (``cvc-complex-type.2.4.a``) sin devolver trackid — el N° de
    # Atención queda comprometido. Protegido por
    # ``tests/test_caratula_orden_xsd.py::TestOrdenCaratulaLibroGuia``.
    caratula = "<Caratula>\n"
    caratula += f"<RutEmisorLibro>{empresa.rut}</RutEmisorLibro>\n"
    caratula += f"<RutEnvia>{rut_envia}</RutEnvia>\n"
    caratula += f"<PeriodoTributario>{periodo}</PeriodoTributario>\n"
    caratula += f"<FchResol>{empresa.fecha_resolucion}</FchResol>\n"
    caratula += f"<NroResol>{empresa.numero_resolucion}</NroResol>\n"
    caratula += f"<TipoLibro>{tipo_libro}</TipoLibro>\n"
    caratula += f"<TipoEnvio>{tipo_envio}</TipoEnvio>\n"
    if tipo_libro == "ESPECIAL":
        caratula += f"<FolioNotificacion>{folio_notificacion}</FolioNotificacion>\n"
    caratula += "</Caratula>"

    # Build Detalle entries and collect data for ResumenPeriodo
    detalles_xml = ""
    tot_fol_anulado = 0  # Anulado=1: folio no utilizado (previo a envío al SII)
    tot_guia_anulada = 0  # Anulado=2: guía emitida y anulada (posterior a envío al SII)
    tot_guia = 0
    tot_mnt_neto = 0
    tot_mnt_iva = 0
    tot_mnt_total = 0
    tot_mnt_exe = 0
    traslado_data = {}  # tpo_oper -> {cant, neto, iva, total}

    for dte in dtes_sorted:
        mnt_exe = dte.monto_exento or 0
        mnt_neto = dte.monto_neto or 0
        mnt_iva = dte.iva or 0
        mnt_total = dte.monto_total or 0

        # Determine TpoOper from IndTraslado
        ind_traslado = _extraer_ind_traslado_desde_xml(dte.xml_firmado)
        tpo_oper = _IND_TRASLADO_TO_TPO_OPER.get(ind_traslado, 1)  # default: 1 (venta)

        # Check if voided — distinguish between anulada (2) and folio no usado (1).
        # Prioridad:
        # 1. Override desde el set SII (folios_anulados) — instrucción literal
        #    "EL CASO N CORRESPONDE A UNA GUIA ANULADA" del libro de guías.
        # 2. Flags del modelo DteEmitido (en producción).
        anulado = (
            dte.folio in folios_anulados
            or getattr(dte, 'anulado', False)
            or getattr(dte, 'estado', '') == 'anulado'
        )
        folio_no_usado = getattr(dte, 'folio_no_usado', False) or getattr(dte, 'estado', '') == 'folio_no_usado'

        det = "<Detalle>\n"
        det += f"<Folio>{dte.folio}</Folio>\n"

        if folio_no_usado:
            # Anulado=1 (folio no usado, previo a envío): schema solo permite Folio + Anulado
            det += "<Anulado>1</Anulado>\n"
            tot_fol_anulado += 1
            det += "</Detalle>\n"
            detalles_xml += det
            continue

        if anulado:
            # Anulado=2 (guía emitida y anulada, posterior a envío): lleva detalle completo
            det += "<Anulado>2</Anulado>\n"
            tot_guia_anulada += 1
        else:
            # Guía activa: contar en totales
            tot_guia += 1
            tot_mnt_neto += mnt_neto
            tot_mnt_iva += mnt_iva
            tot_mnt_total += mnt_total
            tot_mnt_exe += mnt_exe
            if tpo_oper not in traslado_data:
                traslado_data[tpo_oper] = {"cant": 0, "neto": 0, "iva": 0, "total": 0}
            traslado_data[tpo_oper]["cant"] += 1
            traslado_data[tpo_oper]["neto"] += mnt_neto
            traslado_data[tpo_oper]["iva"] += mnt_iva
            traslado_data[tpo_oper]["total"] += mnt_total

        det += f"<TpoOper>{tpo_oper}</TpoOper>\n"
        fecha_str = dte.fecha_emision.strftime('%Y-%m-%d') if hasattr(dte.fecha_emision, 'strftime') else str(dte.fecha_emision)
        det += f"<FchDoc>{fecha_str}</FchDoc>\n"
        det += f"<RUTDoc>{dte.receptor_rut or '66666666-6'}</RUTDoc>\n"
        det += f"<RznSoc>{dte.receptor_razon or 'SIN RAZON SOCIAL'}</RznSoc>\n"
        if mnt_neto:
            det += f"<MntNeto>{mnt_neto}</MntNeto>\n"
        if mnt_iva or mnt_neto:
            det += "<TasaImp>19</TasaImp>\n"
            det += f"<IVA>{mnt_iva}</IVA>\n"
        det += f"<MntTotal>{mnt_total}</MntTotal>\n"
        # NOTA: MntExe NO existe en LibroGuia_v10.xsd Detalle
        # Los montos exentos van incluidos en MntTotal

        # Optional: document references
        tpo_ref, folio_ref, fch_ref = _extraer_referencia_guia_desde_xml(dte.xml_firmado)
        if tpo_ref is not None:
            det += f"<TpoDocRef>{tpo_ref}</TpoDocRef>\n"
        if folio_ref is not None:
            det += f"<FolioDocRef>{folio_ref}</FolioDocRef>\n"
        if fch_ref is not None:
            det += f"<FchDocRef>{fch_ref}</FchDocRef>\n"

        det += "</Detalle>\n"
        detalles_xml += det

    # Build ResumenPeriodo for LibroGuia
    # Schema: TotFolAnulado?, TotGuiaAnulada?, TotGuiaVenta?, TotMntGuiaVta?,
    #   TotTraslado* (TpoTraslado: 2-9, CantGuia, MntGuia)
    # IndTraslado=1 (venta) → TotGuiaVenta/TotMntGuiaVta (NOT TotTraslado)
    # IndTraslado 2-9 → TotTraslado entries
    resumen_xml = "<ResumenPeriodo>\n"
    if tot_fol_anulado > 0:
        resumen_xml += f"<TotFolAnulado>{tot_fol_anulado}</TotFolAnulado>\n"
    if tot_guia_anulada > 0:
        resumen_xml += f"<TotGuiaAnulada>{tot_guia_anulada}</TotGuiaAnulada>\n"
    # Venta guías (IndTraslado=1 → TpoOper=1)
    if 1 in traslado_data:
        td_venta = traslado_data[1]
        resumen_xml += f"<TotGuiaVenta>{td_venta['cant']}</TotGuiaVenta>\n"
        resumen_xml += f"<TotMntGuiaVta>{td_venta['total']}</TotMntGuiaVta>\n"
    # Traslado entries (TpoTraslado 2-9 only)
    for tpo in sorted(traslado_data.keys()):
        if tpo == 1:
            continue  # venta already handled above
        td = traslado_data[tpo]
        resumen_xml += "<TotTraslado>\n"
        resumen_xml += f"<TpoTraslado>{tpo}</TpoTraslado>\n"
        resumen_xml += f"<CantGuia>{td['cant']}</CantGuia>\n"
        resumen_xml += f"<MntGuia>{td['total']}</MntGuia>\n"
        resumen_xml += "</TotTraslado>\n"
    resumen_xml += "</ResumenPeriodo>\n"

    # Build EnvioLibro
    libro_id = f"LibroGuia_{periodo}"
    envio_libro = f'<EnvioLibro ID="{libro_id}">\n'
    envio_libro += caratula + "\n"
    envio_libro += resumen_xml
    envio_libro += detalles_xml
    envio_libro += f"<TmstFirma>{timestamp}</TmstFirma>\n"
    envio_libro += "</EnvioLibro>"

    # Build full LibroGuia (different root element than LibroCompraVenta)
    xml = (
        f'<LibroGuia xmlns="{SII_NS}" '
        f'xmlns:xsi="{XSI_NS}" '
        f'xsi:schemaLocation="{SII_NS} LibroGuia_v10.xsd" '
        f'version="1.0">\n{envio_libro}</LibroGuia>'
    )

    return xml, libro_id


def generar_libro_compras(
    dtes: list,
    empresa,
    periodo: str,
    rut_envia: str,
    folio_notificacion: int = 0,
    tipo_envio: str = "TOTAL",
) -> tuple[str, str]:
    """Genera el XML del Libro de Compras.

    Args:
        dtes: list of dicts with purchase document data. Each dict should contain:
            TpoDoc, NroDoc, FchDoc, RUTDoc, RznSoc, MntExe, MntNeto, MntIVA, MntTotal
            Optional: TpoImp, TasaImp, IVANoRec, IVAUsoComun, OtrosImp, IVARetTotal
        empresa: Empresa model instance
        periodo: "YYYY-MM"
        rut_envia: RUT of person sending (from cert)
        folio_notificacion: 0 for production (MENSUAL), >0 for certification (ESPECIAL)
        tipo_envio: ``TOTAL`` (primer envío, default), ``PARCIAL``, ``FINAL``
            o ``AJUSTE`` (re-envío correctivo sobre un libro ya recibido
            por el SII). Ver ``TIPOS_ENVIO_VALIDOS``.

    Returns:
        (xml_string, libro_id) — xml_string is unsigned, caller must sign it.
    """
    _validar_tipo_envio(tipo_envio)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Build Detalle entries
    detalles_xml = ""
    for e in dtes:
        det = "<Detalle>"
        det += f"<TpoDoc>{e['TpoDoc']}</TpoDoc>"
        det += f"<NroDoc>{e['NroDoc']}</NroDoc>"
        # TpoImp: always 1 (IVA) for libro de compras
        det += f"<TpoImp>{e.get('TpoImp') or 1}</TpoImp>"
        tasa_imp = e.get("TasaImp") or 19
        det += f"<TasaImp>{tasa_imp}</TasaImp>"
        det += f"<FchDoc>{e['FchDoc']}</FchDoc>"
        det += f"<RUTDoc>{e['RUTDoc']}</RUTDoc>"
        det += f"<RznSoc>{e['RznSoc']}</RznSoc>"
        # MntExe: only emit when non-zero (per SII reference)
        if e.get("MntExe"):
            det += f"<MntExe>{e['MntExe']}</MntExe>"
        if e.get("MntNeto") is not None and e["MntNeto"] != "":
            det += f"<MntNeto>{e['MntNeto']}</MntNeto>"
        # MntIVA: emit only when non-zero.
        # IVANoRec/IVAUsoComun: su IVA NO va en <MntIVA> del detalle,
        # se reporta exclusivamente en <IVANoRec>/<IVAUsoComun>.
        # For IVARetTotal entries, calculate IVA if not explicit.
        mnt_iva = e.get("MntIVA", 0)
        if e.get("IVANoRec") or e.get("IVAUsoComun"):
            mnt_iva = 0
        elif not mnt_iva and e.get("IVARetTotal") and e.get("MntNeto") and e.get("TasaImp"):
            mnt_iva = (e["MntNeto"] * e["TasaImp"] + 50) // 100
        if mnt_iva:
            det += f"<MntIVA>{mnt_iva}</MntIVA>"
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

    # Build ResumenPeriodo
    resumen_por_tipo = OrderedDict()
    for e in dtes:
        tpo = e["TpoDoc"]
        if tpo not in resumen_por_tipo:
            resumen_por_tipo[tpo] = {
                "TpoDoc": tpo,
                "TotDoc": 0,
                "TotMntExe": 0,
                "TotMntNeto": 0,
                "TotMntIVA": 0,
                "TotMntTotal": 0,
            }
        r = resumen_por_tipo[tpo]
        r["TotDoc"] += 1
        r["TotMntExe"] += e.get("MntExe", 0)
        r["TotMntNeto"] += e.get("MntNeto", 0)
        # TotMntIVA: IVANoRec/IVAUsoComun se reportan en sus propios totales,
        # NO deben sumarse a TotMntIVA (solo IVA con derecho a crédito normal).
        entry_iva = e.get("MntIVA", 0)
        if e.get("IVANoRec") or e.get("IVAUsoComun"):
            entry_iva = 0
        elif not entry_iva and e.get("IVARetTotal") and e.get("MntNeto") and e.get("TasaImp"):
            entry_iva = (e["MntNeto"] * e["TasaImp"] + 50) // 100
        if entry_iva:
            r["TotMntIVA"] += entry_iva
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
            fct = e.get("FctProp", 0.60)
            r["FctProp"] = fct
            r["TotCredIVAUsoComun"] = round(r.get("TotIVAUsoComun", 0) * fct)
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
            # XSD: TotOpIVARetTotal (nro operaciones) ANTES de TotIVARetTotal (monto)
            if r.get("TotOpIVARetTotal"):
                resumen_xml += f"<TotOpIVARetTotal>{r['TotOpIVARetTotal']}</TotOpIVARetTotal>"
            resumen_xml += f"<TotIVARetTotal>{r['TotIVARetTotal']}</TotIVARetTotal>"
        resumen_xml += f"<TotMntTotal>{r['TotMntTotal']}</TotMntTotal>"
        resumen_xml += "</TotalesPeriodo>\n"
    resumen_xml += "</ResumenPeriodo>"

    # Determine TipoLibro
    tipo_libro = "ESPECIAL" if folio_notificacion > 0 else "MENSUAL"

    # Build Caratula
    caratula = "<Caratula>\n"
    caratula += f"<RutEmisorLibro>{empresa.rut}</RutEmisorLibro>\n"
    caratula += f"<RutEnvia>{rut_envia}</RutEnvia>\n"
    caratula += f"<PeriodoTributario>{periodo}</PeriodoTributario>\n"
    caratula += f"<FchResol>{empresa.fecha_resolucion}</FchResol>\n"
    caratula += f"<NroResol>{empresa.numero_resolucion}</NroResol>\n"
    caratula += "<TipoOperacion>COMPRA</TipoOperacion>\n"
    caratula += f"<TipoLibro>{tipo_libro}</TipoLibro>\n"
    caratula += f"<TipoEnvio>{tipo_envio}</TipoEnvio>\n"
    if tipo_libro == "ESPECIAL":
        caratula += f"<FolioNotificacion>{folio_notificacion}</FolioNotificacion>\n"
    caratula += "</Caratula>"

    # Build EnvioLibro
    libro_id = f"COMPRAS_{periodo}"
    envio_libro = f'<EnvioLibro ID="{libro_id}">\n'
    envio_libro += caratula + "\n"
    envio_libro += resumen_xml + "\n"
    envio_libro += detalles_xml
    envio_libro += f"<TmstFirma>{timestamp}</TmstFirma>\n"
    envio_libro += "</EnvioLibro>"

    # Build full LibroCompraVenta
    xml = (
        f'<LibroCompraVenta xmlns="{SII_NS}" '
        f'xmlns:xsi="{XSI_NS}" '
        f'xsi:schemaLocation="{SII_NS} LibroCV_v10.xsd" '
        f'version="1.0">\n{envio_libro}</LibroCompraVenta>'
    )

    return xml, libro_id
