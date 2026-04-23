"""Generador de XML para DTEs según formato SII Chile."""
import logging
from datetime import datetime
from lxml import etree

from crumbpos.models.dte_models import DTE
from crumbpos.core.caf.caf_manager import CAF
from crumbpos.core.firma.timbre import generar_ted

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

logger = logging.getLogger(__name__)

# Límites de longitud del XSD SII (DTE_v10.xsd + SiiTypes_v10.xsd).
# El SII rechaza con STATUS=7 (esquema inválido) cualquier valor que
# exceda estos límites. Truncar es la política defensiva: preservamos
# información crítica (RUTs, montos, folios) y sólo acortamos textos
# descriptivos que a veces vienen largos desde bases externas.
_XSD_MAXLEN = {
    # Emisor
    "RznSoc": 100,          # RznSocLargaType
    "RznSocEmisor": 100,    # RznSocLargaType (boletas)
    "GiroEmis": 80,
    "GiroEmisor": 80,       # boletas
    "DirOrigen": 70,
    "CmnaOrigen": 20,       # ComunaType
    "CiudadOrigen": 20,     # CiudadType
    # Receptor
    "RznSocRecep": 100,     # RznSocLargaType
    "GiroRecep": 40,        # ¡40!, no 80 como el emisor
    "DirRecep": 70,
    "CmnaRecep": 20,
    "CiudadRecep": 20,
}


def _set_text_truncado(parent: etree._Element, tag: str, value) -> etree._Element:
    """Crea un SubElement con el texto truncado al maxLength del XSD.

    Si ``tag`` no está en ``_XSD_MAXLEN``, se asigna tal cual. Si el valor
    excede el límite, se trunca y se emite un warning con el tag y la
    longitud — útil para detectar bases con datos mal cargados.
    """
    el = etree.SubElement(parent, tag)
    text = "" if value is None else str(value)
    maxlen = _XSD_MAXLEN.get(tag)
    if maxlen is not None and len(text) > maxlen:
        logger.warning(
            "Truncando %s: %d chars -> %d (XSD SII). Valor original: %r",
            tag, len(text), maxlen, text,
        )
        text = text[:maxlen]
    el.text = text
    return el


def generar_documento_xml(dte: DTE, caf: CAF, timestamp: str | None = None) -> etree._Element:
    """Genera el XML completo de un DTE (sin firma XMLDSig)."""
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    es_boleta = dte.tipo_dte in (39, 41)
    doc_id = f"F{dte.folio}T{dte.tipo_dte}"

    documento = etree.Element("Documento", ID=doc_id)

    # --- ENCABEZADO ---
    encabezado = etree.SubElement(documento, "Encabezado")

    # IdDoc
    id_doc = etree.SubElement(encabezado, "IdDoc")
    etree.SubElement(id_doc, "TipoDTE").text = str(dte.tipo_dte)
    etree.SubElement(id_doc, "Folio").text = str(dte.folio)
    etree.SubElement(id_doc, "FchEmis").text = dte.fecha_emision

    if dte.tipo_dte == 52:
        # Schema DTE_v10.xsd: TipoDespacho ANTES de IndTraslado
        if dte.tipo_despacho is not None:
            etree.SubElement(id_doc, "TipoDespacho").text = str(dte.tipo_despacho)
        if dte.tipo_traslado is not None:
            etree.SubElement(id_doc, "IndTraslado").text = str(dte.tipo_traslado)

    if dte.indicador_servicio is not None:
        etree.SubElement(id_doc, "IndServicio").text = str(dte.indicador_servicio)
    elif es_boleta:
        # IndServicio es obligatorio para boletas
        etree.SubElement(id_doc, "IndServicio").text = "3"

    # Boletas NO llevan IndMntBruto — son bruto por defecto en EnvioBOLETA_v11

    # Forma de pago (1=Contado, 2=Crédito, 3=Sin costo) — después de IndServicio
    if dte.fma_pago is not None:
        etree.SubElement(id_doc, "FmaPago").text = str(dte.fma_pago)

    # MntPagos (para ventas a crédito) — dentro de IdDoc, después de FmaPago
    if dte.fecha_pago and dte.monto_pago is not None:
        mnt_pagos = etree.SubElement(id_doc, "MntPagos")
        etree.SubElement(mnt_pagos, "FchPago").text = dte.fecha_pago
        etree.SubElement(mnt_pagos, "MntPago").text = str(dte.monto_pago)

    # Fecha de vencimiento de pago — último elemento de IdDoc
    if dte.fecha_vencimiento:
        etree.SubElement(id_doc, "FchVenc").text = dte.fecha_vencimiento

    # Emisor
    emisor = etree.SubElement(encabezado, "Emisor")
    etree.SubElement(emisor, "RUTEmisor").text = dte.emisor["RUTEmisor"]
    if es_boleta:
        _set_text_truncado(emisor, "RznSocEmisor", dte.emisor["RznSoc"])
        _set_text_truncado(emisor, "GiroEmisor", dte.emisor["GiroEmis"])
        # Boletas NO llevan Acteco pero SÍ llevan CdgSIISucur (opcional)
    else:
        _set_text_truncado(emisor, "RznSoc", dte.emisor["RznSoc"])
        _set_text_truncado(emisor, "GiroEmis", dte.emisor["GiroEmis"])
        etree.SubElement(emisor, "Acteco").text = str(dte.emisor["Acteco"])
    # CdgSIISucur: código sucursal SII (opcional, antes de DirOrigen según XSD)
    if dte.emisor.get("SucDeSII"):
        etree.SubElement(emisor, "CdgSIISucur").text = str(dte.emisor["SucDeSII"])
    _set_text_truncado(emisor, "DirOrigen", dte.emisor["DirOrigen"])
    _set_text_truncado(emisor, "CmnaOrigen", dte.emisor["CmnaOrigen"])
    _set_text_truncado(emisor, "CiudadOrigen", dte.emisor["CiudadOrigen"])

    # Receptor
    receptor = etree.SubElement(encabezado, "Receptor")
    etree.SubElement(receptor, "RUTRecep").text = dte.receptor["RUTRecep"]
    if not es_boleta:
        _set_text_truncado(receptor, "RznSocRecep", dte.receptor["RznSocRecep"])
        _set_text_truncado(receptor, "GiroRecep", dte.receptor["GiroRecep"])
        _set_text_truncado(receptor, "DirRecep", dte.receptor["DirRecep"])
        _set_text_truncado(receptor, "CmnaRecep", dte.receptor["CmnaRecep"])
        if dte.receptor.get("CiudadRecep"):
            _set_text_truncado(receptor, "CiudadRecep", dte.receptor["CiudadRecep"])

    # Totales
    totales = etree.SubElement(encabezado, "Totales")
    if dte.monto_neto is not None:
        etree.SubElement(totales, "MntNeto").text = str(dte.monto_neto)
    if dte.monto_exento is not None:
        etree.SubElement(totales, "MntExe").text = str(dte.monto_exento)
    if not es_boleta and dte.tasa_iva is not None:
        etree.SubElement(totales, "TasaIVA").text = str(dte.tasa_iva)
    if dte.iva is not None:
        etree.SubElement(totales, "IVA").text = str(dte.iva)
    etree.SubElement(totales, "MntTotal").text = str(dte.monto_total)

    # --- DETALLE ---
    for item in dte.items:
        detalle = etree.SubElement(documento, "Detalle")
        etree.SubElement(detalle, "NroLinDet").text = str(item.nro_linea)

        # IndExe solo para tipos que NO son exentos por definición.
        # T34 (Factura Exenta) y T41 (Boleta Exenta): todos los items son
        # exentos implícitamente — agregar IndExe es redundante y puede
        # interferir con la validación CodRef=3 del SII.
        if item.exento and dte.tipo_dte not in (34, 41):
            etree.SubElement(detalle, "IndExe").text = "1"

        etree.SubElement(detalle, "NmbItem").text = item.nombre

        # Orden schema: QtyItem, UnmdItem, PrcItem
        # SII schema: QtyItem Dec12_6Type (hasta 6 decimales), PrcItem Dec12_6Type
        # UnmdItem y PrcItem SOLO se incluyen cuando hay QtyItem válido
        has_qty = item.cantidad is not None and item.cantidad != 0
        if has_qty:
            qty_str = f"{item.cantidad:.6f}".rstrip("0").rstrip(".")
            etree.SubElement(detalle, "QtyItem").text = qty_str

            if item.unidad_medida:
                etree.SubElement(detalle, "UnmdItem").text = item.unidad_medida

        if item.precio_unitario is not None and item.precio_unitario != 0:
            prc_str = f"{item.precio_unitario:.6f}".rstrip("0").rstrip(".")
            etree.SubElement(detalle, "PrcItem").text = prc_str

        if item.descuento_pct is not None:
            etree.SubElement(detalle, "DescuentoPct").text = str(item.descuento_pct)
        if item.descuento_monto is not None:
            etree.SubElement(detalle, "DescuentoMonto").text = str(item.descuento_monto)

        # MontoItem es obligatorio en DTE
        monto = item.monto_item if item.monto_item is not None else 0
        etree.SubElement(detalle, "MontoItem").text = str(monto)

    # --- DESCUENTOS/RECARGOS GLOBALES ---
    for desc in dte.descuentos_globales:
        dscrcg = etree.SubElement(documento, "DscRcgGlobal")
        etree.SubElement(dscrcg, "NroLinDR").text = str(desc.nro_linea)
        etree.SubElement(dscrcg, "TpoMov").text = desc.tipo
        etree.SubElement(dscrcg, "GlosaDR").text = desc.descripcion
        etree.SubElement(dscrcg, "TpoValor").text = desc.tipo_valor
        etree.SubElement(dscrcg, "ValorDR").text = str(desc.valor)
        if desc.indicador_exento is not None:
            etree.SubElement(dscrcg, "IndExeDR").text = str(desc.indicador_exento)

    # --- REFERENCIAS ---
    for ref in dte.referencias:
        referencia = etree.SubElement(documento, "Referencia")
        etree.SubElement(referencia, "NroLinRef").text = str(ref.nro_linea)
        # Boletas: schema solo permite NroLinRef, CodRef, RazonRef
        if not es_boleta:
            etree.SubElement(referencia, "TpoDocRef").text = str(ref.tipo_doc_ref)
            if ref.folio_ref:
                etree.SubElement(referencia, "FolioRef").text = str(ref.folio_ref)
            # FchRef: obligatorio según XSD (sin minOccurs="0"), siempre emitir
            if ref.fecha_ref:
                etree.SubElement(referencia, "FchRef").text = ref.fecha_ref
            else:
                # Fallback: usar fecha emisión del DTE actual
                etree.SubElement(referencia, "FchRef").text = dte.fecha_emision
        if ref.codigo_ref is not None:
            etree.SubElement(referencia, "CodRef").text = str(ref.codigo_ref)
        if ref.razon_ref:
            etree.SubElement(referencia, "RazonRef").text = ref.razon_ref

    # --- TED (Timbre Electrónico) ---
    ted = generar_ted(
        rut_emisor=dte.emisor["RUTEmisor"],
        tipo_dte=dte.tipo_dte,
        folio=dte.folio,
        fecha_emision=dte.fecha_emision,
        rut_receptor=dte.receptor["RUTRecep"],
        razon_social_receptor=dte.receptor.get("RznSocRecep", dte.receptor.get("RUTRecep", "")),
        monto_total=dte.monto_total,
        nombre_primer_item=dte.items[0].nombre if dte.items else "",
        caf=caf,
        timestamp=timestamp,
    )
    documento.append(ted)

    etree.SubElement(documento, "TmstFirma").text = timestamp

    return documento


def generar_dte_xml(documento: etree._Element) -> etree._Element:
    """Envuelve un Documento en el tag DTE con versión."""
    dte = etree.Element("DTE", version="1.0")
    dte.append(documento)
    return dte


def generar_envio_dte(
    dtes: list[etree._Element],
    rut_emisor: str,
    rut_envia: str,
    rut_receptor: str,
    fecha_resolucion: str,
    nro_resolucion: int,
    timestamp: str | None = None,
) -> etree._Element:
    """Genera el EnvioDTE (sobre)."""
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    nsmap = {None: SII_NS, "xsi": XSI_NS}
    envio = etree.Element("EnvioDTE", nsmap=nsmap, version="1.0")
    envio.set(f"{{{XSI_NS}}}schemaLocation", f"{SII_NS} EnvioDTE_v10.xsd")
    set_dte = etree.SubElement(envio, "SetDTE", ID="SetDoc")

    caratula = etree.SubElement(set_dte, "Caratula", version="1.0")
    etree.SubElement(caratula, "RutEmisor").text = rut_emisor
    etree.SubElement(caratula, "RutEnvia").text = rut_envia
    etree.SubElement(caratula, "RutReceptor").text = rut_receptor
    etree.SubElement(caratula, "FchResol").text = fecha_resolucion
    etree.SubElement(caratula, "NroResol").text = str(nro_resolucion)
    etree.SubElement(caratula, "TmstFirmaEnv").text = timestamp

    conteo = _contar_dtes(dtes)
    for tipo, nro in sorted(conteo.items()):
        sub = etree.SubElement(caratula, "SubTotDTE")
        etree.SubElement(sub, "TpoDTE").text = tipo
        etree.SubElement(sub, "NroDTE").text = str(nro)

    for dte in dtes:
        set_dte.append(dte)

    return envio


def generar_envio_boleta(
    dtes: list[etree._Element],
    rut_emisor: str,
    rut_envia: str,
    fecha_resolucion: str,
    nro_resolucion: int,
    timestamp: str | None = None,
) -> etree._Element:
    """Genera el EnvioBOLETA."""
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    nsmap = {None: SII_NS, "xsi": XSI_NS}
    envio = etree.Element("EnvioBOLETA", nsmap=nsmap, version="1.0")
    envio.set(f"{{{XSI_NS}}}schemaLocation", f"{SII_NS} EnvioBOLETA_v11.xsd")
    set_dte = etree.SubElement(envio, "SetDTE", ID="SetDoc")

    caratula = etree.SubElement(set_dte, "Caratula", version="1.0")
    etree.SubElement(caratula, "RutEmisor").text = rut_emisor
    etree.SubElement(caratula, "RutEnvia").text = rut_envia
    etree.SubElement(caratula, "RutReceptor").text = "60803000-K"
    etree.SubElement(caratula, "FchResol").text = fecha_resolucion
    etree.SubElement(caratula, "NroResol").text = str(nro_resolucion)
    etree.SubElement(caratula, "TmstFirmaEnv").text = timestamp

    conteo = _contar_dtes(dtes)
    for tipo, nro in sorted(conteo.items()):
        sub = etree.SubElement(caratula, "SubTotDTE")
        etree.SubElement(sub, "TpoDTE").text = tipo
        etree.SubElement(sub, "NroDTE").text = str(nro)

    for dte in dtes:
        set_dte.append(dte)

    return envio


def _contar_dtes(dtes: list[etree._Element]) -> dict[str, int]:
    """Cuenta DTEs por tipo."""
    conteo = {}
    for dte in dtes:
        doc = dte.find("Documento")
        if doc is not None:
            enc = doc.find("Encabezado")
            if enc is not None:
                id_doc = enc.find("IdDoc")
                if id_doc is not None:
                    tipo = id_doc.findtext("TipoDTE")
                    if tipo:
                        conteo[tipo] = conteo.get(tipo, 0) + 1
    return conteo


def xml_to_string(element: etree._Element, xml_declaration: bool = True) -> bytes:
    """Serializa un elemento XML a bytes con encoding ISO-8859-1.

    """
    xml_bytes = etree.tostring(
        element,
        xml_declaration=xml_declaration,
        encoding="ISO-8859-1",
    )
    xml_bytes = xml_bytes.replace(
        b"<?xml version='1.0' encoding='ISO-8859-1'?>",
        b'<?xml version="1.0" encoding="ISO-8859-1"?>',
    )
    return xml_bytes
