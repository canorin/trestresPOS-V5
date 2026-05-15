"""Autenticación con el SII para envío de DTEs.

Proceso:
1. Obtener semilla (seed) del web service CrSeed (SOAP)
2. Firmar la semilla con el certificado digital
3. Enviar semilla firmada a GetTokenFromSeed para obtener token
4. Usar token en el upload de DTEs
"""
import base64
import hashlib
import logging
import requests
from lxml import etree
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from crumbpos.config.settings import get_sii_url

logger = logging.getLogger(__name__)

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SII_NS = "http://www.sii.cl/XMLSchema"


# ══════════════════════════════════════════════════════════════
# Excepciones diferenciadas por canal de autenticación
# ══════════════════════════════════════════════════════════════
#
# Ambos canales del SII (SOAP factura y REST boleta) reportan errores con
# distinta semántica. Tener clases dedicadas permite:
#   - Distinguir en logs/dashboards qué canal está fallando.
#   - Reintentos diferenciados (REST puede recuperarse con backoff distinto).
#   - Detección de errores transitorios vs. permanentes por canal.


class SIIAuthError(ValueError):
    """Error genérico de autenticación contra el SII."""
    pass


class SIIAuthSOAPError(SIIAuthError):
    """Error obteniendo token SOAP (DTEUpload — facturas, NC, ND, guías, libros, RCOF).

    Indica falla del flujo getSeed→firmar→getToken en maullin/palena.
    Casos típicos: ESTADO != 00 en respuesta, semilla inválida, certificado vencido.
    """
    pass


class SIIAuthRESTError(SIIAuthError):
    """Error obteniendo token REST (boleta.electronica — solo T39/T41).

    Indica falla del flujo getSeed boleta → firmar → getToken boleta en apicert/api.
    Casos típicos: HTTP 4xx/5xx, JSON malformado, certificado no enrolado en boletas REST.
    """
    pass


def _soap_call(url: str, action: str) -> str:
    """Hace una llamada SOAP al SII y retorna el XML interno (escapado en la respuesta)."""
    soap_body = f'''<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:def="http://DefaultNamespace">
   <soapenv:Header/>
   <soapenv:Body>
      <def:{action}/>
   </soapenv:Body>
</soapenv:Envelope>'''

    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "",
    }
    response = requests.post(url, data=soap_body, headers=headers, timeout=30)
    response.raise_for_status()

    # Parsear SOAP envelope
    root = etree.fromstring(response.content)
    # El contenido está escapado como string dentro del SOAP response
    # Buscar el elemento *Return que contiene el XML escapado
    for elem in root.iter():
        if elem.text and "RESP" in (elem.text or ""):
            return elem.text
        if elem.tag.endswith("Return") and elem.text:
            return elem.text

    raise SIIAuthSOAPError(
        f"SOAP {url}: no se pudo parsear respuesta. Primer fragmento: {response.text[:500]}"
    )


def _soap_call_with_body(url: str, action: str, body_xml: str) -> str:
    """Hace una llamada SOAP con cuerpo XML personalizado.

    Raises:
        SIIAuthSOAPError: para SOAP Faults, HTTP 500, respuestas inválidas
            o cualquier otro problema en el flujo SOAP.
    """
    from xml.sax.saxutils import escape
    # El XML firmado debe ir escapado como string dentro del SOAP
    escaped_xml = escape(body_xml)

    soap_body = f'''<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:def="http://DefaultNamespace">
   <soapenv:Header/>
   <soapenv:Body>
      <def:{action}>
         <pszXml xsi:type="xsd:string" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">{escaped_xml}</pszXml>
      </def:{action}>
   </soapenv:Body>
</soapenv:Envelope>'''

    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "",
    }
    response = requests.post(url, data=soap_body.encode("utf-8"), headers=headers, timeout=30)

    if response.status_code == 500:
        # Intentar parsear error del SII
        try:
            root = etree.fromstring(response.content)
            fault = root.findtext(".//{http://schemas.xmlsoap.org/soap/envelope/}faultstring")
            if fault:
                raise SIIAuthSOAPError(f"SOAP Fault del SII ({action}): {fault}")
        except etree.XMLSyntaxError:
            pass
        raise SIIAuthSOAPError(
            f"HTTP 500 del SII en {action}: {response.text[:500]}"
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise SIIAuthSOAPError(
            f"HTTP {response.status_code} del SII en {action}: {exc}"
        ) from exc

    root = etree.fromstring(response.content)
    for elem in root.iter():
        if elem.tag.endswith("Return") and elem.text:
            return elem.text

    raise SIIAuthSOAPError(
        f"SOAP {action}: respuesta sin elemento *Return. Primer fragmento: {response.text[:500]}"
    )


def obtener_semilla(ambiente: str) -> str:
    """Obtiene una semilla del SII vía SOAP.

    Args:
        ambiente: "certificacion" o "produccion" — resuelve el host SII
            (maullin vs palena).

    Raises:
        SIIAuthSOAPError: si la respuesta del SII no contiene SEMILLA o
            si el ESTADO retornado no es "00".
    """
    url = get_sii_url("seed", ambiente)
    try:
        xml_interno = _soap_call(url, "getSeed")
    except ValueError as exc:
        # _soap_call lanza ValueError genérico; lo elevamos como SOAP-específico
        raise SIIAuthSOAPError(f"getSeed SOAP falló: {exc} (ambiente={ambiente})") from exc

    # Parsear el XML interno
    root = etree.fromstring(xml_interno.encode("utf-8"))
    semilla = root.findtext(f".//{{{SII_NS}}}RESP_BODY/SEMILLA")
    if semilla is None:
        semilla = root.findtext(".//SEMILLA")
    if semilla is None:
        raise SIIAuthSOAPError(
            f"SOAP getSeed: no se encontró SEMILLA en respuesta SII "
            f"(ambiente={ambiente}). Primer fragmento: {xml_interno[:500]}"
        )

    # Verificar estado
    estado = root.findtext(f".//{{{SII_NS}}}RESP_HDR/ESTADO")
    if estado and estado != "00":
        raise SIIAuthSOAPError(
            f"SOAP getSeed: estado={estado} (ambiente={ambiente})"
        )

    return semilla


def firmar_semilla(semilla: str, private_key_pem: bytes, cert_der: bytes) -> str:
    """Firma la semilla con el certificado digital y retorna el XML firmado."""
    DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"

    # Construir XML de solicitud de token
    gettoken = etree.Element("getToken")
    item = etree.SubElement(gettoken, "item")
    etree.SubElement(item, "Semilla").text = semilla

    # Canonicalizar para digest
    item_c14n = etree.tostring(gettoken, method="c14n")
    digest = hashlib.sha1(item_c14n).digest()
    digest_b64 = base64.b64encode(digest).decode("ascii")

    # Construir Signature
    signature = etree.SubElement(gettoken, f"{{{DSIG_NS}}}Signature", nsmap={None: DSIG_NS})
    signed_info = etree.SubElement(signature, "SignedInfo")
    etree.SubElement(
        signed_info, "CanonicalizationMethod",
        Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    etree.SubElement(
        signed_info, "SignatureMethod",
        Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1",
    )
    reference = etree.SubElement(signed_info, "Reference", URI="")
    transforms = etree.SubElement(reference, "Transforms")
    etree.SubElement(
        transforms, "Transform",
        Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature",
    )
    etree.SubElement(reference, "DigestMethod", Algorithm="http://www.w3.org/2000/09/xmldsig#sha1")
    etree.SubElement(reference, "DigestValue").text = digest_b64

    # Firmar SignedInfo
    signed_info_c14n = etree.tostring(signed_info, method="c14n")
    private_key = load_pem_private_key(private_key_pem, password=None)
    firma = private_key.sign(signed_info_c14n, padding.PKCS1v15(), hashes.SHA1())

    etree.SubElement(signature, "SignatureValue").text = base64.b64encode(firma).decode("ascii")

    key_info = etree.SubElement(signature, "KeyInfo")
    x509_data = etree.SubElement(key_info, "X509Data")
    etree.SubElement(x509_data, "X509Certificate").text = base64.b64encode(cert_der).decode("ascii")

    return etree.tostring(gettoken, encoding="unicode")


def obtener_token(private_key_pem: bytes, cert_der: bytes, ambiente: str) -> str:
    """Obtiene un token de autenticación del SII.

    Args:
        private_key_pem: llave privada del certificado digital, en PEM.
        cert_der: certificado en DER para incluir en X509Data.
        ambiente: "certificacion" o "produccion" — determina el host SII.
    """
    semilla = obtener_semilla(ambiente)
    logger.debug("Semilla SII obtenida: %s (ambiente=%s)", semilla, ambiente)

    xml_firmado = firmar_semilla(semilla, private_key_pem, cert_der)

    url = get_sii_url("token", ambiente)
    # Enviar como SOAP call con el XML firmado
    xml_interno = _soap_call_with_body(url, "getToken", xml_firmado)

    # Parsear respuesta
    root = etree.fromstring(xml_interno.encode("utf-8"))
    token = root.findtext(f".//{{{SII_NS}}}RESP_BODY/TOKEN")
    if token is None:
        token = root.findtext(".//TOKEN")

    estado = root.findtext(f".//{{{SII_NS}}}RESP_HDR/ESTADO")
    if estado and estado != "00":
        glosa = root.findtext(f".//{{{SII_NS}}}RESP_HDR/GLOSA") or ""
        raise SIIAuthSOAPError(
            f"SOAP getToken: estado={estado}, glosa={glosa!r} (ambiente={ambiente})"
        )

    if token is None:
        raise SIIAuthSOAPError(
            f"SOAP getToken: no se encontró TOKEN en respuesta SII. "
            f"Primer fragmento: {xml_interno[:500]}"
        )

    return token


# ==================== BOLETA REST API ====================

def obtener_semilla_boleta(ambiente: str) -> str:
    """Obtiene una semilla del SII vía REST API (boletas).

    Args:
        ambiente: "certificacion" o "produccion" — resuelve apicert vs api.

    Raises:
        SIIAuthRESTError: si la respuesta REST no contiene SEMILLA o si el
            ESTADO retornado no es "00".
    """
    url = get_sii_url("boleta_seed", ambiente)
    try:
        response = requests.get(url, headers={"Accept": "application/xml"}, timeout=30)
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise SIIAuthRESTError(
            f"REST getSeed boleta: HTTP {response.status_code} "
            f"(ambiente={ambiente}, url={url})"
        ) from exc
    except requests.RequestException as exc:
        raise SIIAuthRESTError(
            f"REST getSeed boleta: error de red ({exc.__class__.__name__}: {exc}) "
            f"(ambiente={ambiente})"
        ) from exc

    xml_text = response.text.replace('<?xml version="1.0" encoding="UTF-8"?>', '')
    root = etree.fromstring(xml_text.encode("utf-8"))

    # Buscar semilla en la respuesta
    semilla = root.findtext(f".//{{{SII_NS}}}RESP_BODY/SEMILLA")
    if semilla is None:
        semilla = root.findtext(".//SEMILLA")
    if semilla is None:
        raise SIIAuthRESTError(
            f"REST getSeed boleta: no se encontró SEMILLA en respuesta SII "
            f"(ambiente={ambiente}). Primer fragmento: {response.text[:500]}"
        )

    estado = root.findtext(f".//{{{SII_NS}}}RESP_HDR/ESTADO")
    if estado and estado != "00":
        raise SIIAuthRESTError(
            f"REST getSeed boleta: estado={estado} (ambiente={ambiente})"
        )

    return semilla


def firmar_semilla_boleta(semilla: str, firma) -> str:
    """Firma la semilla de boleta usando la librería facturacion_electronica.

    La semilla de boleta usa un formato diferente al SOAP tradicional:
    <getToken><item ID="IdAFirmar"><Semilla>{seed}</Semilla></item></getToken>

    Raises:
        SIIAuthRESTError: si la firma de la semilla falla (cert vencido,
            password incorrecta, semilla inválida, etc.).
    """
    xml_seed = f'<getToken><item ID="IdAFirmar"><Semilla>{semilla}</Semilla></item></getToken>'
    signed = firma.firmar(xml_seed, uri="IdAFirmar", type="token")
    if not signed:
        raise SIIAuthRESTError(
            f"REST firmar semilla boleta: error en firma electrónica: {firma.errores}"
        )
    return signed


def obtener_token_boleta(firma, ambiente: str) -> str:
    """Obtiene un token de autenticación del SII para boletas vía REST API.

    Args:
        firma: instancia de facturacion_electronica.firma.Firma ya inicializada
        ambiente: "certificacion" o "produccion" — resuelve apicert vs api.

    Returns:
        Token string para uso en Cookie header
    """
    semilla = obtener_semilla_boleta(ambiente)
    logger.debug("Semilla boleta SII obtenida: %s (ambiente=%s)", semilla, ambiente)

    xml_firmado = firmar_semilla_boleta(semilla, firma)

    url = get_sii_url("boleta_token", ambiente)
    body = '<?xml version="1.0" encoding="UTF-8"?>' + xml_firmado
    response = requests.post(
        url,
        data=body.encode("utf-8"),
        headers={
            "Accept": "application/xml",
            "Content-Type": "application/xml",
        },
        timeout=30,
    )
    response.raise_for_status()

    xml_text = response.text.replace('<?xml version="1.0" encoding="UTF-8"?>', '')
    root = etree.fromstring(xml_text.encode("utf-8"))

    token = root.findtext(f".//{{{SII_NS}}}RESP_BODY/TOKEN")
    if token is None:
        token = root.findtext(".//TOKEN")

    estado = root.findtext(f".//{{{SII_NS}}}RESP_HDR/ESTADO")
    if estado and estado != "00":
        glosa = root.findtext(f".//{{{SII_NS}}}RESP_HDR/GLOSA") or ""
        raise SIIAuthRESTError(
            f"REST getToken boleta: estado={estado}, glosa={glosa!r} (ambiente={ambiente})"
        )

    if token is None:
        raise SIIAuthRESTError(
            f"REST getToken boleta: no se encontró TOKEN en respuesta SII. "
            f"Primer fragmento: {response.text[:500]}"
        )

    return token
