"""Consulta de estado de DTEs en el SII."""
import requests
from lxml import etree
from crumbpos.config.settings import get_sii_url


def consultar_estado_dte(
    token: str,
    rut_emisor: str,
    tipo_dte: int,
    folio: int,
    fecha_emision: str,
    monto_total: int,
    rut_receptor: str,
    ambiente: str,
) -> dict:
    """Consulta el estado de un DTE específico en el SII via SOAP.

    Args:
        token: Token SOAP vigente.
        rut_emisor: RUT de la empresa emisora (sin puntos, con guión).
        tipo_dte: Código del tipo de DTE (33, 34, 39, 41, 52, 56, 61).
        folio: Folio del DTE consultado.
        fecha_emision: Fecha de emisión en formato YYYY-MM-DD.
        monto_total: Monto total del DTE.
        rut_receptor: RUT del receptor del DTE.
        ambiente: "certificacion" o "produccion" — resuelve maullin vs palena.
    """
    rut_e_num, rut_e_dv = rut_emisor.split("-")
    rut_r_num, rut_r_dv = rut_receptor.split("-")
    url = get_sii_url("estado_dte", ambiente)

    soap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<soapenv:Body>
<getEstDte xmlns="{url}" soapenv:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<RutConsultante xsi:type="xsd:string">{rut_e_num}</RutConsultante>
<DvConsultante xsi:type="xsd:string">{rut_e_dv}</DvConsultante>
<RutCompania xsi:type="xsd:string">{rut_e_num}</RutCompania>
<DvCompania xsi:type="xsd:string">{rut_e_dv}</DvCompania>
<RutReceptor xsi:type="xsd:string">{rut_r_num}</RutReceptor>
<DvReceptor xsi:type="xsd:string">{rut_r_dv}</DvReceptor>
<TipoDte xsi:type="xsd:string">{tipo_dte}</TipoDte>
<FolioDte xsi:type="xsd:string">{folio}</FolioDte>
<FechaEmisionDte xsi:type="xsd:string">{fecha_emision}</FechaEmisionDte>
<MontoDte xsi:type="xsd:string">{monto_total}</MontoDte>
<Token xsi:type="xsd:string">{token}</Token>
</getEstDte>
</soapenv:Body>
</soapenv:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=UTF-8",
        "SOAPAction": '""',
    }

    response = requests.post(url, data=soap_body.encode("utf-8"), headers=headers, timeout=30)
    response.raise_for_status()

    text = response.text

    # Extract return value from SOAP response (may have namespace prefix)
    import re
    import html as html_mod
    match = re.search(r'getEstDteReturn[^>]*>([\s\S]*?)</\w*:?getEstDteReturn', text)
    if match:
        result_xml = html_mod.unescape(match.group(1))
        return {"raw": result_xml, "soap_raw": text}

    return {"raw": text, "soap_raw": text}
