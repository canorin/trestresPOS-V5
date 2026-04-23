"""Generador de los 3 XMLs de respuesta al Intercambio SII.

Dado un `SobreIntercambio` parseado y el certificado de la empresa,
arma y firma:

1. **RecepcionDTE.xml** — acuse de formato del sobre.
   Schema: `RespuestaEnvioDTE_v10.xsd`.
   Contiene `<RecepcionEnvio>` con N `<RecepcionDTE>` (uno por DTE).

2. **EnvioRecibos.xml** — acuse comercial Ley 19.983.
   Schema: `EnvioRecibos_v10.xsd`.
   Contiene `<SetRecibos>` con N `<Recibo>` (uno por DTE aceptado).
   Cada `<Recibo>` lleva firma propia.

3. **ResultadoDTE.xml** — resultado comercial (aceptación/rechazo).
   Schema: `RespuestaEnvioDTE_v10.xsd`.
   Contiene N `<ResultadoDTE>` (uno por DTE).

Reglas de decisión implementadas:

- Si `dte.rut_recep` == RUT de la empresa → **aceptado** (EstadoRecepDTE=0,
  EstadoDTE=0), se incluye Recibo en EnvioRecibos.
- Si `dte.rut_recep` != RUT de la empresa → **rechazado** (EstadoRecepDTE=3
  con glosa "DTE No Recibido - Error en RUT Receptor", EstadoDTE=2 con
  glosa "RECHAZADO"), NO se incluye Recibo.

Encoding: ISO-8859-1 (es el que pide el SII y el que valida la firma).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lxml import etree

from crumbpos.core.firma.firma_digital import firmar_documento
from crumbpos.core.intercambio.parser import DteIntercambio, SobreIntercambio


# ═══════════════════════════════════════════════════════════════════
# Constantes SII (literales — NO modificar)
# ═══════════════════════════════════════════════════════════════════

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

# Glosas oficiales — los textos están fijados por el SII. Mantener
# sin tildes donde el SII los escribe sin tildes (ej: "Envio").
GLOSA_ENVIO_OK = "Envio Recibido Conforme"
GLOSA_DTE_OK = "DTE Recibido OK"
GLOSA_DTE_ERR_RUT_RECEP = "DTE No Recibido - Error en RUT Receptor"
GLOSA_ACEPTADO = "ACEPTADO OK"
GLOSA_RECHAZADO = "RECHAZADO"

# Declaración Ley 19.983 — `fixed=` en Recibos_v10.xsd. Un solo char
# cambiado y el XSD falla. Texto canónico:
DECLARACION_LEY_19983 = (
    "El acuse de recibo que se declara en este acto, de acuerdo a lo "
    "dispuesto en la letra b) del Art. 4, y la letra c) del Art. 5 de "
    "la Ley 19.983, acredita que la entrega de mercaderias o servicio(s) "
    "prestado(s) ha(n) sido recibido(s)."
)

# EstadoRecepDTE
RECEP_DTE_OK = 0
RECEP_DTE_ERR_RUT_RECEP = 3

# EstadoDTE (ResultadoDTE)
DTE_ACEPTADO = 0
DTE_RECHAZADO = 2

# IDs con prefijo LibreDTE_ — misma convención que el resto del core.
ID_RESULTADO_ENVIO = "LibreDTE_ResultadoEnvio"
ID_SET_RECIBOS = "LibreDTE_SetDteRecibidos"

# Recinto default (donde se "recepcionan" las mercaderías/servicios).
RECINTO_DEFAULT = "Oficina central"


# ═══════════════════════════════════════════════════════════════════
# Config de contacto
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ContactoIntercambio:
    """Datos que van en las Caratulas de los 3 XMLs.

    `nombre` y `email` son **opcionales** según el XSD pero el SII los
    pide en la práctica durante la certificación.
    """
    nombre: str
    email: str
    # RUT del firmante del Recibo (representante legal). Formato
    # "99999999-9". Va en `<RutFirma>` de cada DocumentoRecibo.
    rut_firma: str
    # RUT de la empresa que responde (el que se está certificando).
    # Va en `<RutResponde>` y se usa para decidir aceptado/rechazado
    # comparando contra `dte.rut_recep`.
    rut_responde: str


# ═══════════════════════════════════════════════════════════════════
# Helpers comunes
# ═══════════════════════════════════════════════════════════════════

def _qn(tag: str) -> str:
    """QName con namespace SII — para crear elementos con lxml."""
    return f"{{{SII_NS}}}{tag}"


def _sub(parent: etree._Element, tag: str, text: str | int | None = None) -> etree._Element:
    """Crea un subelemento SII con texto opcional."""
    elem = etree.SubElement(parent, _qn(tag))
    if text is not None:
        elem.text = str(text)
    return elem


def _tmst_ahora() -> str:
    """xs:dateTime SII: YYYY-MM-DDTHH:MM:SS (sin timezone)."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _es_aceptado(dte: DteIntercambio, rut_responde: str) -> bool:
    """Un DTE es aceptable si va dirigido a la empresa que responde."""
    return _normalizar_rut(dte.rut_recep) == _normalizar_rut(rut_responde)


def _normalizar_rut(rut: str) -> str:
    """Normaliza formato RUT: sin puntos, upper, trim."""
    return rut.replace(".", "").strip().upper()


def _serializar(root: etree._Element) -> bytes:
    """Serializa a bytes ISO-8859-1 con declaración XML normalizada."""
    xml_bytes = etree.tostring(
        root,
        xml_declaration=True,
        encoding="ISO-8859-1",
    )
    # lxml emite comillas simples — normalizamos a dobles para
    # consistencia con el resto del core.
    return xml_bytes.replace(
        b"<?xml version='1.0' encoding='ISO-8859-1'?>",
        b'<?xml version="1.0" encoding="ISO-8859-1"?>',
    )


# ═══════════════════════════════════════════════════════════════════
# 1. RecepcionDTE.xml — acuse de formato
# ═══════════════════════════════════════════════════════════════════

def armar_recepcion_dte(
    sobre: SobreIntercambio,
    contacto: ContactoIntercambio,
    private_key_pem: bytes,
    cert_der: bytes,
    tmst: str | None = None,
) -> bytes:
    """Arma y firma el XML de RecepcionDTE (acuse del sobre).

    Returns: bytes en ISO-8859-1 listos para subir al SII.
    """
    tmst = tmst or _tmst_ahora()

    # Namespaces del root: xmlns=SII, xmlns:xsi
    nsmap = {None: SII_NS, "xsi": XSI_NS}
    root = etree.Element(_qn("RespuestaDTE"), nsmap=nsmap)
    root.set(
        f"{{{XSI_NS}}}schemaLocation",
        "http://www.sii.cl/SiiDte RespuestaEnvioDTE_v10.xsd",
    )
    root.set("version", "1.0")

    resultado = etree.SubElement(root, _qn("Resultado"))
    resultado.set("ID", ID_RESULTADO_ENVIO)

    # ── Caratula ──────────────────────────────────────────────────
    caratula = etree.SubElement(resultado, _qn("Caratula"))
    caratula.set("version", "1.0")
    _sub(caratula, "RutResponde", contacto.rut_responde)
    _sub(caratula, "RutRecibe", sobre.rut_emisor)
    _sub(caratula, "IdRespuesta", 1)
    _sub(caratula, "NroDetalles", 1)  # Un solo RecepcionEnvio
    if contacto.nombre:
        _sub(caratula, "NmbContacto", contacto.nombre)
    if contacto.email:
        _sub(caratula, "MailContacto", contacto.email)
    _sub(caratula, "TmstFirmaResp", tmst)

    # ── RecepcionEnvio ────────────────────────────────────────────
    recep_env = etree.SubElement(resultado, _qn("RecepcionEnvio"))
    _sub(recep_env, "NmbEnvio", sobre.nombre_archivo)
    _sub(recep_env, "FchRecep", tmst)
    _sub(recep_env, "CodEnvio", 1)
    _sub(recep_env, "EnvioDTEID", sobre.set_id)
    _sub(recep_env, "Digest", sobre.digest_sha1_b64)
    _sub(recep_env, "RutEmisor", sobre.rut_emisor)
    _sub(recep_env, "RutReceptor", sobre.rut_receptor)
    _sub(recep_env, "EstadoRecepEnv", 0)
    _sub(recep_env, "RecepEnvGlosa", GLOSA_ENVIO_OK)
    _sub(recep_env, "NroDTE", len(sobre.dtes))

    # ── N × RecepcionDTE ──────────────────────────────────────────
    for dte in sobre.dtes:
        _armar_recepcion_dte_item(recep_env, dte, contacto.rut_responde)

    # ── Firma sobre <Resultado ID="LibreDTE_ResultadoEnvio"> ─────
    firmar_documento(
        doc_element=resultado,
        private_key_pem=private_key_pem,
        cert_der=cert_der,
        parent_element=root,
        es_sobre=True,
    )

    return _serializar(root)


def _armar_recepcion_dte_item(
    parent: etree._Element,
    dte: DteIntercambio,
    rut_responde: str,
) -> etree._Element:
    """Agrega un <RecepcionDTE> al contenedor <RecepcionEnvio>."""
    item = etree.SubElement(parent, _qn("RecepcionDTE"))

    if _es_aceptado(dte, rut_responde):
        estado = RECEP_DTE_OK
        glosa = GLOSA_DTE_OK
    else:
        estado = RECEP_DTE_ERR_RUT_RECEP
        glosa = GLOSA_DTE_ERR_RUT_RECEP

    _sub(item, "TipoDTE", dte.tipo_dte)
    _sub(item, "Folio", dte.folio)
    _sub(item, "FchEmis", dte.fch_emis)
    _sub(item, "RUTEmisor", dte.rut_emisor)
    _sub(item, "RUTRecep", dte.rut_recep)
    _sub(item, "MntTotal", dte.mnt_total)
    _sub(item, "EstadoRecepDTE", estado)
    _sub(item, "RecepDTEGlosa", glosa)
    return item


# ═══════════════════════════════════════════════════════════════════
# 2. EnvioRecibos.xml — acuse comercial Ley 19.983
# ═══════════════════════════════════════════════════════════════════

def armar_envio_recibos(
    sobre: SobreIntercambio,
    contacto: ContactoIntercambio,
    private_key_pem: bytes,
    cert_der: bytes,
    tmst: str | None = None,
    incluir_rechazados: bool = False,
) -> bytes:
    """Arma y firma el XML de EnvioRecibos (acuse comercial).

    Por defecto solo incluye recibos de los DTEs aceptados (rut_recep
    coincide con rut_responde). Pasar `incluir_rechazados=True` si
    alguna vez el SII pidiera incluir todos — no es el caso en la
    certificación estándar.

    El SetRecibos debe tener al menos 1 Recibo (maxOccurs="unbounded",
    minOccurs implícito = 1). Si todos los DTEs son rechazados este
    XML no tiene sentido — el caller debe manejarlo.

    Returns: bytes en ISO-8859-1.

    Raises:
        ValueError: si no quedan DTEs aceptados para incluir.
    """
    tmst = tmst or _tmst_ahora()

    dtes_a_incluir = [
        dte for dte in sobre.dtes
        if incluir_rechazados or _es_aceptado(dte, contacto.rut_responde)
    ]
    if not dtes_a_incluir:
        raise ValueError(
            "EnvioRecibos requiere al menos 1 DTE aceptado; no hay "
            "ninguno cuyo RUTRecep coincida con rut_responde."
        )

    nsmap = {None: SII_NS, "xsi": XSI_NS}
    root = etree.Element(_qn("EnvioRecibos"), nsmap=nsmap)
    root.set(
        f"{{{XSI_NS}}}schemaLocation",
        "http://www.sii.cl/SiiDte EnvioRecibos_v10.xsd",
    )
    root.set("version", "1.0")

    set_recibos = etree.SubElement(root, _qn("SetRecibos"))
    set_recibos.set("ID", ID_SET_RECIBOS)

    # ── Caratula ──────────────────────────────────────────────────
    caratula = etree.SubElement(set_recibos, _qn("Caratula"))
    caratula.set("version", "1.0")
    _sub(caratula, "RutResponde", contacto.rut_responde)
    _sub(caratula, "RutRecibe", sobre.rut_emisor)
    if contacto.nombre:
        _sub(caratula, "NmbContacto", contacto.nombre)
    if contacto.email:
        _sub(caratula, "MailContacto", contacto.email)
    _sub(caratula, "TmstFirmaEnv", tmst)

    # ── N × Recibo (cada uno firmado individualmente) ────────────
    for dte in dtes_a_incluir:
        _armar_recibo(set_recibos, dte, contacto, private_key_pem, cert_der, tmst)

    # ── Firma sobre <SetRecibos ID="LibreDTE_SetDteRecibidos"> ──
    firmar_documento(
        doc_element=set_recibos,
        private_key_pem=private_key_pem,
        cert_der=cert_der,
        parent_element=root,
        es_sobre=True,
    )

    return _serializar(root)


def _armar_recibo(
    set_recibos: etree._Element,
    dte: DteIntercambio,
    contacto: ContactoIntercambio,
    private_key_pem: bytes,
    cert_der: bytes,
    tmst: str,
) -> etree._Element:
    """Agrega un <Recibo> firmado al SetRecibos."""
    recibo_id = f"LibreDTE_T{dte.tipo_dte}F{dte.folio}"
    recibo = etree.SubElement(set_recibos, _qn("Recibo"))
    recibo.set("version", "1.0")

    doc_recibo = etree.SubElement(recibo, _qn("DocumentoRecibo"))
    doc_recibo.set("ID", recibo_id)

    _sub(doc_recibo, "TipoDoc", dte.tipo_dte)
    _sub(doc_recibo, "Folio", dte.folio)
    _sub(doc_recibo, "FchEmis", dte.fch_emis)
    _sub(doc_recibo, "RUTEmisor", dte.rut_emisor)
    _sub(doc_recibo, "RUTRecep", dte.rut_recep)
    _sub(doc_recibo, "MntTotal", dte.mnt_total)
    _sub(doc_recibo, "Recinto", RECINTO_DEFAULT)
    _sub(doc_recibo, "RutFirma", contacto.rut_firma)
    _sub(doc_recibo, "Declaracion", DECLARACION_LEY_19983)
    _sub(doc_recibo, "TmstFirmaRecibo", tmst)

    # Firma del Recibo: <DocumentoRecibo ID="LibreDTE_T33F52126">
    # El Signature se anida dentro de <Recibo>, junto al DocumentoRecibo.
    firmar_documento(
        doc_element=doc_recibo,
        private_key_pem=private_key_pem,
        cert_der=cert_der,
        parent_element=recibo,
        es_sobre=False,  # Documento standalone — sin xmlns:xsi en digest
    )
    return recibo


# ═══════════════════════════════════════════════════════════════════
# 3. ResultadoDTE.xml — resultado comercial
# ═══════════════════════════════════════════════════════════════════

def armar_resultado_dte(
    sobre: SobreIntercambio,
    contacto: ContactoIntercambio,
    private_key_pem: bytes,
    cert_der: bytes,
    tmst: str | None = None,
) -> bytes:
    """Arma y firma el XML de ResultadoDTE (aprobación/rechazo comercial).

    Returns: bytes en ISO-8859-1.
    """
    tmst = tmst or _tmst_ahora()

    nsmap = {None: SII_NS, "xsi": XSI_NS}
    root = etree.Element(_qn("RespuestaDTE"), nsmap=nsmap)
    root.set(
        f"{{{XSI_NS}}}schemaLocation",
        "http://www.sii.cl/SiiDte RespuestaEnvioDTE_v10.xsd",
    )
    root.set("version", "1.0")

    resultado = etree.SubElement(root, _qn("Resultado"))
    resultado.set("ID", ID_RESULTADO_ENVIO)

    # ── Caratula ──────────────────────────────────────────────────
    caratula = etree.SubElement(resultado, _qn("Caratula"))
    caratula.set("version", "1.0")
    _sub(caratula, "RutResponde", contacto.rut_responde)
    _sub(caratula, "RutRecibe", sobre.rut_emisor)
    _sub(caratula, "IdRespuesta", 1)
    _sub(caratula, "NroDetalles", len(sobre.dtes))
    if contacto.nombre:
        _sub(caratula, "NmbContacto", contacto.nombre)
    if contacto.email:
        _sub(caratula, "MailContacto", contacto.email)
    _sub(caratula, "TmstFirmaResp", tmst)

    # ── N × ResultadoDTE ──────────────────────────────────────────
    for i, dte in enumerate(sobre.dtes, start=1):
        _armar_resultado_dte_item(resultado, dte, contacto.rut_responde, cod_envio=i)

    # ── Firma sobre <Resultado ID="LibreDTE_ResultadoEnvio"> ─────
    firmar_documento(
        doc_element=resultado,
        private_key_pem=private_key_pem,
        cert_der=cert_der,
        parent_element=root,
        es_sobre=True,
    )

    return _serializar(root)


def _armar_resultado_dte_item(
    parent: etree._Element,
    dte: DteIntercambio,
    rut_responde: str,
    cod_envio: int,
) -> etree._Element:
    """Agrega un <ResultadoDTE> al contenedor <Resultado>."""
    item = etree.SubElement(parent, _qn("ResultadoDTE"))

    if _es_aceptado(dte, rut_responde):
        estado = DTE_ACEPTADO
        glosa = GLOSA_ACEPTADO
    else:
        estado = DTE_RECHAZADO
        glosa = GLOSA_RECHAZADO

    _sub(item, "TipoDTE", dte.tipo_dte)
    _sub(item, "Folio", dte.folio)
    _sub(item, "FchEmis", dte.fch_emis)
    _sub(item, "RUTEmisor", dte.rut_emisor)
    _sub(item, "RUTRecep", dte.rut_recep)
    _sub(item, "MntTotal", dte.mnt_total)
    _sub(item, "CodEnvio", cod_envio)
    _sub(item, "EstadoDTE", estado)
    _sub(item, "EstadoDTEGlosa", glosa)
    return item
