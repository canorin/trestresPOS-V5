"""Generación del Timbre Electrónico (TED) del SII.

El TED se firma con la llave privada RSA del CAF (NO con el certificado .pfx).

IMPORTANTE: Según el instructivo técnico del SII (Anexo 2, A.2.4):
- La firma del TED NO usa el estándar XMLDSIG/C14N.
- Se debe eliminar whitespace entre tags y referencias a NameSpaces.
- El digest se calcula sobre el string resultante.
"""
import base64
import re
from datetime import datetime
from lxml import etree
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from crumbpos.core.caf.caf_manager import CAF


def _preparar_dd_para_firma(dd: etree._Element) -> bytes:
    """Prepara el DD para firma según instructivo SII A.2.4.

    Proceso:
    1. Serializar DD a XML string
    2. Eliminar whitespace entre tags (\\n, \\t, espacios entre > y <)
    3. Eliminar referencias a NameSpaces (xmlns=... y xmlns:xxx=...)
    4. Retornar como bytes ISO-8859-1
    """
    # Serializar a string (sin xml declaration)
    dd_xml = etree.tostring(dd, encoding="unicode")

    # Eliminar whitespace entre tags: todo lo que está entre > y <
    # que sea solo whitespace
    dd_xml = re.sub(r'>\s+<', '><', dd_xml)

    # Eliminar referencias a NameSpaces
    # xmlns="..." y xmlns:xxx="..."
    dd_xml = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', dd_xml)

    return dd_xml.encode("ISO-8859-1")


def generar_ted(
    rut_emisor: str,
    tipo_dte: int,
    folio: int,
    fecha_emision: str,
    rut_receptor: str,
    razon_social_receptor: str,
    monto_total: int,
    nombre_primer_item: str,
    caf: CAF,
    timestamp: str | None = None,
) -> etree._Element:
    """
    Genera el elemento TED (Timbre Electrónico del DTE).

    El TED contiene:
    - DD: datos del documento
    - FRMT: firma RSA sobre DD usando la llave privada del CAF

    La firma se genera según el instructivo técnico del SII (A.2.4):
    - Se eliminan whitespace entre tags y referencias a NameSpaces
    - Se firma con SHA1withRSA usando la llave privada del CAF
    """
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Construir DD como string raw para preservar el CAF byte-a-byte
    # CRÍTICO: no re-serializar el CAF con lxml para mantener la firma FRMA válida

    # Escapar caracteres XML en campos de texto
    from xml.sax.saxutils import escape as _xml_escape
    _rsr = _xml_escape(razon_social_receptor[:40])
    _it1 = _xml_escape(nombre_primer_item[:40])

    # Datos del documento
    dd_parts = [
        "<DD>",
        f"<RE>{rut_emisor}</RE>",
        f"<TD>{tipo_dte}</TD>",
        f"<F>{folio}</F>",
        f"<FE>{fecha_emision}</FE>",
        f"<RR>{rut_receptor}</RR>",
        f"<RSR>{_rsr}</RSR>",
        f"<MNT>{monto_total}</MNT>",
        f"<IT1>{_it1}</IT1>",
    ]

    # CAF como string raw del archivo original (preserva firma FRMA)
    caf_str = caf.caf_xml
    dd_parts.append(caf_str)

    dd_parts.append(f"<TSTED>{timestamp}</TSTED>")
    dd_parts.append("</DD>")

    dd_xml_raw = "".join(dd_parts)

    # Preparar DD para firma según instructivo SII A.2.4:
    # Eliminar whitespace entre tags y referencias a NameSpaces
    dd_for_sign = re.sub(r'>\s+<', '><', dd_xml_raw)
    dd_for_sign = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', dd_for_sign)
    dd_bytes = dd_for_sign.encode("ISO-8859-1")

    # Firmar con la llave privada del CAF
    private_key = caf.get_private_key()
    # SHA1withRSA según especificación SII
    firma = private_key.sign(
        dd_bytes,
        padding.PKCS1v15(),
        hashes.SHA1(),
    )
    firma_b64 = base64.b64encode(firma).decode("ascii")

    # Construir TED completo como string, parsear con encoding ISO-8859-1
    ted_xml = f'<TED version="1.0">{dd_xml_raw}<FRMT algoritmo="SHA1withRSA">{firma_b64}</FRMT></TED>'
    ted_bytes = ted_xml.encode("ISO-8859-1")
    parser = etree.XMLParser(encoding="ISO-8859-1")
    ted = etree.fromstring(b'<?xml version="1.0" encoding="ISO-8859-1"?>' + ted_bytes, parser)

    return ted
