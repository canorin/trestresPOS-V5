"""Parser del `ENVIO_DTE.xml` que el SII entrega en la etapa de
Intercambio de Información (Ley 19.983).

El SII manda un sobre firmado por un emisor simulado (RUT 88888888-8)
con N DTEs dirigidos al contribuyente en certificación. Este parser
lo abre y devuelve una estructura tipada para que el generador pueda
armar las 3 respuestas.

Importante:
- El archivo viene en ISO-8859-1 — **NUNCA** re-codificar a UTF-8 antes
  de calcular el Digest: el SII valida sobre los bytes originales.
- El Digest que va en `<RecepcionEnvio><Digest>` es
  base64(sha1(bytes_originales_xml)).
- El `EnvioDTEID` en la respuesta debe ser el `ID` del `SetDTE`
  del sobre recibido (típicamente "SetDoc").
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree


SII_NS = "http://www.sii.cl/SiiDte"
_NSMAP = {"s": SII_NS}


# ═══════════════════════════════════════════════════════════════════
# Modelos
# ═══════════════════════════════════════════════════════════════════

@dataclass
class DteIntercambio:
    """Un DTE extraído del sobre recibido."""
    tipo_dte: int
    folio: int
    fch_emis: str          # YYYY-MM-DD literal del XML
    rut_emisor: str        # RUTEmisor del Documento (formato 99999999-9)
    rut_recep: str         # RUTRecep del Documento
    mnt_total: int
    # ID opcional del Documento (ej: "T33F52126"); lo usamos para armar
    # el ID del Recibo en el acuse comercial.
    doc_id: str | None = None


@dataclass
class SobreIntercambio:
    """Sobre EnvioDTE parseado."""
    set_id: str                        # ID del SetDTE (default "SetDoc")
    rut_emisor: str                    # Caratula/RutEmisor (proveedor)
    rut_envia: str                     # Caratula/RutEnvia (firmante)
    rut_receptor: str                  # Caratula/RutReceptor (nosotros)
    tmst_firma_env: str                # Caratula/TmstFirmaEnv
    nombre_archivo: str                # nombre original, ej ENVIO_DTE_4792140.xml
    digest_sha1_b64: str               # base64(sha1(xml_bytes))
    dtes: list[DteIntercambio] = field(default_factory=list)
    # Bytes originales — conservamos el XML crudo para recomputar digests
    # o adjuntarlo al expediente de certificación.
    xml_bytes: bytes = b""


# ═══════════════════════════════════════════════════════════════════
# Parser principal
# ═══════════════════════════════════════════════════════════════════

def parsear_envio_dte_sii(
    xml_bytes: bytes,
    nombre_archivo: str = "ENVIO_DTE.xml",
) -> SobreIntercambio:
    """Parsea un `ENVIO_DTE.xml` y devuelve un `SobreIntercambio`.

    Args:
        xml_bytes: bytes del archivo tal cual vino del SII (ISO-8859-1).
        nombre_archivo: nombre original (va en `<NmbEnvio>` del
            RecepcionDTE). Default "ENVIO_DTE.xml" para casos sin
            nombre conocido.

    Raises:
        ValueError: si el XML es inválido, no es un EnvioDTE, o no
            trae DTEs.
    """
    if not isinstance(xml_bytes, (bytes, bytearray)):
        raise TypeError(
            f"parsear_envio_dte_sii espera bytes, no {type(xml_bytes).__name__}"
        )
    if not xml_bytes.strip():
        raise ValueError("XML vacío")

    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"XML inválido: {exc}") from exc

    # El root puede ser EnvioDTE directo, o una bolsa con namespace.
    tag_local = etree.QName(root.tag).localname
    if tag_local != "EnvioDTE":
        raise ValueError(
            f"Se esperaba <EnvioDTE> como root, se encontró <{tag_local}>"
        )

    set_dte = _find(root, "s:SetDTE")
    if set_dte is None:
        raise ValueError("EnvioDTE no contiene SetDTE")

    set_id = (set_dte.get("ID") or "SetDoc").strip() or "SetDoc"

    caratula = _find(set_dte, "s:Caratula")
    if caratula is None:
        raise ValueError("SetDTE no contiene Caratula")

    rut_emisor = _text(caratula, "s:RutEmisor")
    rut_envia = _text(caratula, "s:RutEnvia")
    rut_receptor = _text(caratula, "s:RutReceptor")
    tmst_firma = _text(caratula, "s:TmstFirmaEnv")

    if not rut_emisor:
        raise ValueError("Caratula sin RutEmisor")
    if not rut_receptor:
        raise ValueError("Caratula sin RutReceptor")
    if not tmst_firma:
        raise ValueError("Caratula sin TmstFirmaEnv")

    # DTEs del sobre
    dtes: list[DteIntercambio] = []
    for dte_elem in set_dte.findall("s:DTE", _NSMAP):
        documento = _find(dte_elem, "s:Documento")
        if documento is None:
            # Podría ser una liquidación (Liquidacion) — por ahora no
            # las soportamos en intercambio; el SII solo manda facturas.
            continue
        doc_id = documento.get("ID") or None

        encabezado = _find(documento, "s:Encabezado")
        if encabezado is None:
            raise ValueError(f"Documento {doc_id or '?'} sin Encabezado")

        id_doc = _find(encabezado, "s:IdDoc")
        emisor = _find(encabezado, "s:Emisor")
        receptor = _find(encabezado, "s:Receptor")
        totales = _find(encabezado, "s:Totales")
        if any(e is None for e in (id_doc, emisor, receptor, totales)):
            raise ValueError(
                f"Documento {doc_id or '?'} con Encabezado incompleto"
            )

        try:
            tipo_dte = int(_text(id_doc, "s:TipoDTE"))
            folio = int(_text(id_doc, "s:Folio"))
            mnt_total = int(_text(totales, "s:MntTotal"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Documento {doc_id or '?'} con numéricos inválidos: {exc}"
            ) from exc

        fch_emis = _text(id_doc, "s:FchEmis")
        rut_emisor_doc = _text(emisor, "s:RUTEmisor")
        rut_recep_doc = _text(receptor, "s:RUTRecep")

        if not all((fch_emis, rut_emisor_doc, rut_recep_doc)):
            raise ValueError(
                f"Documento {doc_id or '?'} sin FchEmis/RUTEmisor/RUTRecep"
            )

        dtes.append(
            DteIntercambio(
                tipo_dte=tipo_dte,
                folio=folio,
                fch_emis=fch_emis,
                rut_emisor=rut_emisor_doc,
                rut_recep=rut_recep_doc,
                mnt_total=mnt_total,
                doc_id=doc_id,
            )
        )

    if not dtes:
        raise ValueError("EnvioDTE no contiene DTEs")

    digest_b64 = base64.b64encode(hashlib.sha1(xml_bytes).digest()).decode("ascii")

    return SobreIntercambio(
        set_id=set_id,
        rut_emisor=rut_emisor,
        rut_envia=rut_envia,
        rut_receptor=rut_receptor,
        tmst_firma_env=tmst_firma,
        nombre_archivo=nombre_archivo,
        digest_sha1_b64=digest_b64,
        dtes=dtes,
        xml_bytes=bytes(xml_bytes),
    )


def parsear_envio_dte_desde_archivo(path: str | Path) -> SobreIntercambio:
    """Atajo: abre el archivo en binario y delega al parser."""
    p = Path(path)
    return parsear_envio_dte_sii(p.read_bytes(), nombre_archivo=p.name)


# ═══════════════════════════════════════════════════════════════════
# Helpers internos
# ═══════════════════════════════════════════════════════════════════

def _find(parent: etree._Element, xpath: str) -> etree._Element | None:
    """find() con namespace SII forzado."""
    return parent.find(xpath, _NSMAP)


def _text(parent: etree._Element, xpath: str) -> str:
    """findtext() con namespace SII forzado, trim, y None → ''."""
    elem = parent.find(xpath, _NSMAP)
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()
