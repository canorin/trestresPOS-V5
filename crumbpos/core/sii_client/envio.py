"""Envío de DTEs al SII."""
import asyncio
import json
import logging
import time
import requests
import httpx
from lxml import etree

from crumbpos.config.settings import get_sii_url, RUT_SII

logger = logging.getLogger(__name__)


def enviar_dte(
    xml_bytes: bytes,
    token: str,
    rut_emisor: str,
    ambiente: str,
    rut_envia: str | None = None,
    es_boleta: bool = False,
) -> dict:
    """
    Envía un EnvioDTE al SII.

    Args:
        xml_bytes: XML del EnvioDTE serializado como bytes
        token: Token de autenticación del SII
        rut_emisor: RUT de la empresa (sin puntos, con guión)
        ambiente: "certificacion" o "produccion" — define el host SII.
        rut_envia: RUT de la persona que envía (firmante). Si None, usa rut_emisor.
        es_boleta: True si es envío de boletas

    Returns:
        dict con TrackID y estado
    """
    if rut_envia is None:
        rut_envia = rut_emisor

    # NOTA: es_boleta=True en enviar_dte() mezclaría token SOAP con endpoint
    # REST de boletas — combinación incorrecta. Para boletas usar enviar_boleta().
    # El parámetro se mantiene por compatibilidad de firma pero no debe usarse.
    if es_boleta:
        logger.warning(
            "enviar_dte() llamado con es_boleta=True — usar enviar_boleta() "
            "para boletas T39/T41. Token SOAP y endpoint REST no son compatibles."
        )

    sender_num, sender_dv = rut_envia.split("-")
    company_num, company_dv = rut_emisor.split("-")
    servicio = "boleta_upload" if es_boleta else "upload"
    url = get_sii_url(servicio, ambiente)

    headers = {
        "Cookie": f"TOKEN={token}",
        "User-Agent": "Mozilla/4.0 (compatible; PROG 1.0; CrumbPOS)",
    }

    files = {
        "rutSender": (None, sender_num),
        "dvSender": (None, sender_dv),
        "rutCompany": (None, company_num),
        "dvCompany": (None, company_dv),
        "archivo": ("envio.xml", xml_bytes, "text/xml"),
    }

    # Reintentos por conexiones inestables del SII
    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = requests.post(url, files=files, headers=headers, timeout=90)
            response.raise_for_status()
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < max_retries - 1:
                wait = 10 * (attempt + 1)
                logger.warning("Reintento %d/%d en %ds... (%s)", attempt + 2, max_retries, wait, e.__class__.__name__)
                time.sleep(wait)
            else:
                raise

    text = response.text
    import re

    track_id = None
    glosa = ""
    status_code = None

    # Con User-Agent "PROG", SII responde XML tipo RECEPCIONDTE
    xml_track = re.search(r'<TRACKID>(\d+)</TRACKID>', text, re.IGNORECASE)
    xml_status = re.search(r'<STATUS>(\d+)</STATUS>', text, re.IGNORECASE)
    if xml_track:
        track_id = xml_track.group(1)
    if xml_status:
        status_code = xml_status.group(1)

    # Fallback: respuesta HTML
    if not track_id:
        track_match = re.search(r'Trackid\s+(\d+)', text, re.IGNORECASE)
        if not track_match:
            track_match = re.search(r'TRACKID\s*[:=]\s*(\d+)', text, re.IGNORECASE)
        if not track_match:
            track_match = re.search(r'Identificador de env[^:]*:\s*<strong>(\d+)</strong>', text, re.IGNORECASE)
        if track_match:
            track_id = track_match.group(1)

    status = "OK" if (status_code == "0" or track_id) else "ERROR"

    # Buscar glosa en XML o HTML
    glosa_match = re.search(r'<GLOSA>([^<]+)</GLOSA>', text, re.IGNORECASE)
    if glosa_match:
        glosa = glosa_match.group(1).strip()
    else:
        # HTML fallback
        td_matches = re.findall(r'<TD>(.+?)</TD>', text, re.DOTALL)
        if td_matches:
            glosa = " | ".join(m.strip() for m in td_matches if m.strip())
        if not glosa:
            h3_match = re.search(r'<h3[^>]*><font[^>]*>\s*(.+?)\s*</font></h3>', text, re.DOTALL)
            if h3_match:
                glosa = re.sub(r'<[^>]+>', '', h3_match.group(1)).strip()

    return {
        "status": status,
        "status_code": status_code,
        "track_id": track_id,
        "glosa": glosa,
        "raw": text,
    }


def consultar_estado_envio(
    track_id: str,
    token: str,
    rut_emisor: str,
    ambiente: str,
) -> dict:
    """Consulta el estado de un envío al SII via SOAP.

    Args:
        track_id: TrackID devuelto por el SII al hacer upload.
        token: Token SOAP vigente.
        rut_emisor: RUT de la empresa (sin puntos, con guión).
        ambiente: "certificacion" o "produccion".
    """
    import xml.sax.saxutils
    rut_num, rut_dv = rut_emisor.split("-")
    url = get_sii_url("estado_envio", ambiente)

    # Namespace estándar SII para servicios SOAP (convención de todos los
    # WS expuestos en maullin/palena: getSeed, getToken, getEstUp, getEstDte).
    # NO usar la URL del endpoint como namespace — algunas implementaciones
    # del servidor SII lo aceptan, pero la convención literal es:
    SII_SOAP_NAMESPACE = "http://DefaultNamespace"

    soap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<soapenv:Body>
<getEstUp xmlns="{SII_SOAP_NAMESPACE}" soapenv:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<RutCompania xsi:type="xsd:string">{rut_num}</RutCompania>
<DvCompania xsi:type="xsd:string">{rut_dv}</DvCompania>
<TrackId xsi:type="xsd:string">{track_id}</TrackId>
<Token xsi:type="xsd:string">{token}</Token>
</getEstUp>
</soapenv:Body>
</soapenv:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=UTF-8",
        "SOAPAction": '""',
    }

    response = requests.post(url, data=soap_body.encode("utf-8"), headers=headers, timeout=30)
    response.raise_for_status()

    text = response.text

    # Extract the return value from SOAP response
    import re
    # The response contains HTML-escaped XML inside getEstUpReturn (may have ns prefix)
    match = re.search(r'getEstUpReturn[^>]*>([^<]+)</\w*:?getEstUpReturn', text)
    if match:
        import html
        result_xml = html.unescape(match.group(1))
        return {"raw": result_xml, "soap_raw": text}

    return {"raw": text, "soap_raw": text}


def enviar_boleta(
    xml_bytes: bytes,
    token: str,
    rut_emisor: str,
    ambiente: str,
    rut_envia: str | None = None,
) -> dict:
    """
    Envía un EnvioBOLETA al SII vía REST API.

    La boleta usa endpoints REST diferentes a los DTEs normales:
    - Upload: pangal.sii.cl (cert) / rahue.sii.cl (prod)
    - Respuesta: JSON (no XML)

    Args:
        xml_bytes: XML del EnvioBOLETA serializado como bytes
        token: Token de autenticación del SII (obtenido vía boleta.electronica.token)
        rut_emisor: RUT de la empresa (sin puntos, con guión)
        ambiente: "certificacion" o "produccion" — resuelve pangal vs rahue.
        rut_envia: RUT de la persona que envía (firmante)

    Returns:
        dict con TrackID y estado
    """
    if rut_envia is None:
        rut_envia = rut_emisor

    sender_num, sender_dv = rut_envia.split("-")
    company_num, company_dv = rut_emisor.split("-")
    url = get_sii_url("boleta_upload", ambiente)

    headers = {
        "Cookie": f"TOKEN={token}",
        "User-Agent": "Mozilla/4.0 (compatible; PROG 1.0; CrumbPOS)",
        "Accept": "application/json",
    }

    files = {
        "rutSender": (None, sender_num),
        "dvSender": (None, sender_dv),
        "rutCompany": (None, company_num),
        "dvCompany": (None, company_dv),
        "archivo": ("envio_boleta.xml", xml_bytes, "text/xml"),
    }

    # Reintentos
    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = requests.post(url, files=files, headers=headers, timeout=90)
            response.raise_for_status()
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < max_retries - 1:
                wait = 10 * (attempt + 1)
                logger.warning("Reintento %d/%d en %ds... (%s)", attempt + 2, max_retries, wait, e.__class__.__name__)
                time.sleep(wait)
            else:
                raise

    text = response.text

    # La REST API de boletas responde JSON
    try:
        resp_json = json.loads(text)
        track_id = resp_json.get("trackid")
        estado = resp_json.get("estado")
        estadistica = resp_json.get("estadistica", [])

        status = "OK" if track_id else "ERROR"

        return {
            "status": status,
            "track_id": str(track_id) if track_id else None,
            "estado": estado,
            "estadistica": estadistica,
            "raw": text,
        }
    except json.JSONDecodeError:
        # Fallback: puede ser que responda XML o HTML
        import re
        track_id = None
        xml_track = re.search(r'<TRACKID>(\d+)</TRACKID>', text, re.IGNORECASE)
        if xml_track:
            track_id = xml_track.group(1)

        return {
            "status": "OK" if track_id else "ERROR",
            "track_id": track_id,
            "estado": None,
            "raw": text,
        }


async def enviar_dte_async(
    xml_bytes: bytes,
    token: str,
    rut_emisor: str,
    ambiente: str,
    rut_envia: str | None = None,
    es_boleta: bool = False,
) -> dict:
    """Versión asíncrona de ``enviar_dte`` — usa ``httpx.AsyncClient``.

    Libera el event loop durante los reintentos al SII (hasta 340 s con la
    versión síncrona).  La firma y el parsing de respuesta son idénticos.
    """
    if rut_envia is None:
        rut_envia = rut_emisor

    if es_boleta:
        logger.warning(
            "enviar_dte_async() llamado con es_boleta=True — usar "
            "enviar_boleta_async() para boletas T39/T41."
        )

    sender_num, sender_dv = rut_envia.split("-")
    company_num, company_dv = rut_emisor.split("-")
    servicio = "boleta_upload" if es_boleta else "upload"
    url = get_sii_url(servicio, ambiente)

    headers = {
        "Cookie": f"TOKEN={token}",
        "User-Agent": "Mozilla/4.0 (compatible; PROG 1.0; CrumbPOS)",
    }

    data = {
        "rutSender": sender_num,
        "dvSender": sender_dv,
        "rutCompany": company_num,
        "dvCompany": company_dv,
    }
    files = {"archivo": ("envio.xml", xml_bytes, "text/xml")}

    max_retries = 5
    response = None
    async with httpx.AsyncClient() as client:
        for attempt in range(max_retries):
            try:
                response = await client.post(
                    url, data=data, files=files, headers=headers, timeout=90.0,
                )
                response.raise_for_status()
                break
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                if attempt < max_retries - 1:
                    wait = 10 * (attempt + 1)
                    logger.warning(
                        "Reintento %d/%d en %ds... (%s)",
                        attempt + 2, max_retries, wait, e.__class__.__name__,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise

    import re
    text = response.text
    track_id = None
    glosa = ""
    status_code = None

    xml_track = re.search(r'<TRACKID>(\d+)</TRACKID>', text, re.IGNORECASE)
    xml_status = re.search(r'<STATUS>(\d+)</STATUS>', text, re.IGNORECASE)
    if xml_track:
        track_id = xml_track.group(1)
    if xml_status:
        status_code = xml_status.group(1)

    if not track_id:
        for pat in (
            r'Trackid\s+(\d+)',
            r'TRACKID\s*[:=]\s*(\d+)',
            r'Identificador de env[^:]*:\s*<strong>(\d+)</strong>',
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                track_id = m.group(1)
                break

    status = "OK" if (status_code == "0" or track_id) else "ERROR"

    glosa_match = re.search(r'<GLOSA>([^<]+)</GLOSA>', text, re.IGNORECASE)
    if glosa_match:
        glosa = glosa_match.group(1).strip()
    else:
        td_matches = re.findall(r'<TD>(.+?)</TD>', text, re.DOTALL)
        if td_matches:
            glosa = " | ".join(m.strip() for m in td_matches if m.strip())
        if not glosa:
            h3_match = re.search(
                r'<h3[^>]*><font[^>]*>\s*(.+?)\s*</font></h3>', text, re.DOTALL,
            )
            if h3_match:
                glosa = re.sub(r'<[^>]+>', '', h3_match.group(1)).strip()

    return {
        "status": status,
        "status_code": status_code,
        "track_id": track_id,
        "glosa": glosa,
        "raw": text,
    }


async def enviar_boleta_async(
    xml_bytes: bytes,
    token: str,
    rut_emisor: str,
    ambiente: str,
    rut_envia: str | None = None,
) -> dict:
    """Versión asíncrona de ``enviar_boleta`` — usa ``httpx.AsyncClient``.

    Libera el event loop durante los reintentos al endpoint REST de boletas.
    """
    if rut_envia is None:
        rut_envia = rut_emisor

    sender_num, sender_dv = rut_envia.split("-")
    company_num, company_dv = rut_emisor.split("-")
    url = get_sii_url("boleta_upload", ambiente)

    headers = {
        "Cookie": f"TOKEN={token}",
        "User-Agent": "Mozilla/4.0 (compatible; PROG 1.0; CrumbPOS)",
        "Accept": "application/json",
    }

    data = {
        "rutSender": sender_num,
        "dvSender": sender_dv,
        "rutCompany": company_num,
        "dvCompany": company_dv,
    }
    files = {"archivo": ("envio_boleta.xml", xml_bytes, "text/xml")}

    max_retries = 5
    response = None
    async with httpx.AsyncClient() as client:
        for attempt in range(max_retries):
            try:
                response = await client.post(
                    url, data=data, files=files, headers=headers, timeout=90.0,
                )
                response.raise_for_status()
                break
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                if attempt < max_retries - 1:
                    wait = 10 * (attempt + 1)
                    logger.warning(
                        "Reintento %d/%d en %ds... (%s)",
                        attempt + 2, max_retries, wait, e.__class__.__name__,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise

    text = response.text
    try:
        resp_json = json.loads(text)
        track_id = resp_json.get("trackid")
        estado = resp_json.get("estado")
        estadistica = resp_json.get("estadistica", [])
        return {
            "status": "OK" if track_id else "ERROR",
            "track_id": str(track_id) if track_id else None,
            "estado": estado,
            "estadistica": estadistica,
            "raw": text,
        }
    except json.JSONDecodeError:
        import re
        xml_track = re.search(r'<TRACKID>(\d+)</TRACKID>', text, re.IGNORECASE)
        track_id = xml_track.group(1) if xml_track else None
        return {
            "status": "OK" if track_id else "ERROR",
            "track_id": track_id,
            "estado": None,
            "raw": text,
        }


def consultar_estado_boleta(
    track_id: str,
    token: str,
    rut_emisor: str,
    ambiente: str,
) -> dict:
    """Consulta el estado de un envío de boletas vía REST API.

    Endpoint SII (REST):
        GET {boleta_estado}/{rutEmisor}-{dvEmisor}-{trackId}

    Donde `boleta_estado` resuelve a:
        - cert: https://apicert.sii.cl/recursos/v1/boleta.electronica.envio
        - prod: https://api.sii.cl/recursos/v1/boleta.electronica.envio

    Headers obligatorios:
        - Accept: application/json (la API REST de boletas siempre responde JSON)
        - Cookie: TOKEN=... (token REST vigente, obtenido vía obtener_token_boleta)

    Args:
        track_id: TrackID devuelto por el SII al hacer upload de boletas.
        token: Token REST vigente para boletas.
        rut_emisor: RUT de la empresa (sin puntos, con guión).
        ambiente: "certificacion" o "produccion".

    Returns:
        dict con la respuesta JSON del SII. Si el SII responde no-JSON
        (excepcional), retorna `{"raw": <texto crudo>}` para diagnóstico.
        El llamador debe verificar la presencia de `estado` y/o `errores`
        en la respuesta para decidir si reintentar.
    """
    rut_num, rut_dv = rut_emisor.split("-")
    url = f"{get_sii_url('boleta_estado', ambiente)}/{rut_num}-{rut_dv}-{track_id}"

    headers = {
        "Accept": "application/json",
        "Cookie": f"TOKEN={token}",
        "User-Agent": "Mozilla/4.0 (compatible; PROG 1.0; CrumbPOS)",
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        logger.warning(
            "consultar_estado_boleta: SII respondió no-JSON (track=%s, ambiente=%s)",
            track_id, ambiente,
        )
        return {"raw": response.text}
