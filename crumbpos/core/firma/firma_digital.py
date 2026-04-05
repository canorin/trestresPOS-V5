"""Firma digital XMLDSig para DTEs del SII Chile.

Verificado contra factura real de producción (NAGOY SPA → TRESTRES).

Hallazgos clave (verificados empíricamente):
- DIGEST INNER: xmlns="SiiDte" SIN xmlns:xsi (Documento standalone)
- DIGEST OUTER: xmlns="SiiDte" CON xmlns:xsi (SetDTE hereda de EnvioDTE)
- SIGNEDINFO: SIEMPRE xmlns="dsig#" + xmlns:xsi (ambos inner y outer)
  El SII verifica con C14N in-tree que propaga xmlns:xsi del ancestro EnvioDTE
- TRANSFORMS: OBLIGATORIO con algoritmo C14N inclusivo

Ref: Verificación contra factura 33_76096747-5_66051 de NAGOY SPA
"""
import base64
import hashlib
import re
from lxml import etree
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12, load_pem_private_key
from cryptography.x509 import load_der_x509_certificate


DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
SII_NS = "http://www.sii.cl/SiiDte"


def _wrap_b64(data: bytes, line_len: int = 76) -> str:
    """Codifica bytes a base64 con saltos de línea cada line_len chars."""
    raw = base64.b64encode(data).decode("ascii")
    lines = [raw[i:i + line_len] for i in range(0, len(raw), line_len)]
    return "\n" + "\n".join(lines) + "\n"


def cargar_certificado_pfx(pfx_path: str, password: str | None = None) -> tuple:
    """Carga un certificado .pfx/.p12."""
    with open(pfx_path, "rb") as f:
        pfx_data = f.read()

    pwd = password.encode() if password else None
    private_key, cert, extra_certs = pkcs12.load_key_and_certificates(pfx_data, pwd)

    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_der = cert.public_bytes(serialization.Encoding.DER)

    return private_key_pem, cert_pem, cert_der


def firmar_documento(
    doc_element: etree._Element,
    private_key_pem: bytes,
    cert_der: bytes,
    parent_element: etree._Element | None = None,
    es_sobre: bool = False,
) -> etree._Element:
    """
    Firma un elemento XML con XMLDSig.

    Proceso verificado contra factura real de producción (NAGOY SPA):
    1. Digest inner: serialize, xmlns SII, SIN xmlns:xsi, reparse, C14N
    2. Digest outer: serialize, xmlns SII, CON xmlns:xsi, reparse, C14N
    3. SignedInfo: SIEMPRE xmlns dsig + xmlns:xsi (inner Y outer)
    4. Transforms: C14N inclusivo obligatorio en Reference
    5. Firma RSA-SHA1 sobre SignedInfo canónico
    """
    doc_id = doc_element.get("ID")
    uri = f"#{doc_id}" if doc_id else ""

    # ================================================================
    # 1. DIGEST: Canonicalizar el elemento referenciado
    # ================================================================
    # Serializar el elemento a string
    xml_str = etree.tostring(doc_element, encoding="unicode")

    if not es_sobre:
        # INNER DTE (Documento): xmlns="SII" pero SIN xmlns:xsi
        # Verificado contra factura real de producción (NAGOY SPA).
        # El SII trata cada DTE como documento standalone.
        xml_str = re.sub(r'\s+xmlns:xsi="[^"]*"', '', xml_str)
        xml_str = re.sub(r'\s+xsi:schemaLocation="[^"]*"', '', xml_str)

        # Asegurar que tiene xmlns="...SiiDte"
        if f'xmlns="{SII_NS}"' not in xml_str:
            tag = doc_element.tag.split('}')[-1] if '}' in doc_element.tag else doc_element.tag
            xml_str = re.sub(
                rf'^(<{tag})\b',
                rf'\1 xmlns="{SII_NS}"',
                xml_str,
            )
    else:
        # OUTER (SetDTE): mantener xmlns:xsi heredado del EnvioDTE.
        # C14N inclusivo propaga namespaces de ancestros.
        # Solo asegurar que tiene xmlns="...SiiDte"
        if f'xmlns="{SII_NS}"' not in xml_str:
            tag = doc_element.tag.split('}')[-1] if '}' in doc_element.tag else doc_element.tag
            xml_str = re.sub(
                rf'^(<{tag})\b',
                rf'\1 xmlns="{SII_NS}"',
                xml_str,
            )

    # Reparse y C14N
    doc_reparsed = etree.fromstring(xml_str.encode("utf-8"))
    c14n_bytes = etree.tostring(doc_reparsed, method="c14n")

    digest = hashlib.sha1(c14n_bytes).digest()
    digest_b64 = base64.b64encode(digest).decode("ascii")

    # ================================================================
    # 2. SIGNEDINFO: Construir CON namespace dsig en TODOS los elementos
    # ================================================================
    # CRÍTICO: Todos los elementos del SignedInfo deben estar en el namespace
    # dsig para que el C14N in-tree no genere xmlns="" espurios.
    # El SII (Java) hace C14N in-tree del SignedInfo tras parsear el XML,
    # y todos los hijos de Signature heredan xmlns="dsig#".
    _d = DSIG_NS  # shorthand
    signed_info = etree.Element(f"{{{_d}}}SignedInfo", nsmap={None: _d})
    etree.SubElement(
        signed_info, f"{{{_d}}}CanonicalizationMethod",
        Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    etree.SubElement(
        signed_info, f"{{{_d}}}SignatureMethod",
        Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1",
    )
    reference = etree.SubElement(signed_info, f"{{{_d}}}Reference", URI=uri)

    # TRANSFORMS: Obligatorio (verificado en factura real)
    transforms = etree.SubElement(reference, f"{{{_d}}}Transforms")
    etree.SubElement(
        transforms, f"{{{_d}}}Transform",
        Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )

    etree.SubElement(
        reference, f"{{{_d}}}DigestMethod",
        Algorithm="http://www.w3.org/2000/09/xmldsig#sha1",
    )
    digest_value = etree.SubElement(reference, f"{{{_d}}}DigestValue")
    digest_value.text = digest_b64

    # C14N del SignedInfo, luego inyectar xmlns:xsi
    # El SII verifica con C14N in-tree que propaga xmlns:xsi del EnvioDTE.
    # Verificado contra factura real NAGOY SPA (inner SignedInfo tiene xmlns:xsi)
    si_c14n = etree.tostring(signed_info, method="c14n").decode()
    # C14N ya incluye xmlns="dsig#" en SignedInfo. Solo inyectar xmlns:xsi.
    si_c14n = si_c14n.replace(
        f'<SignedInfo xmlns="{DSIG_NS}">',
        f'<SignedInfo xmlns="{DSIG_NS}"'
        f' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
    )
    signed_info_bytes = si_c14n.encode("utf-8")

    # ================================================================
    # 3. FIRMAR SignedInfo
    # ================================================================
    private_key = load_pem_private_key(private_key_pem, password=None)
    firma = private_key.sign(
        signed_info_bytes,
        padding.PKCS1v15(),
        hashes.SHA1(),
    )
    firma_b64 = _wrap_b64(firma)

    # ================================================================
    # 4. Construir KeyInfo
    # ================================================================
    cert_obj = load_der_x509_certificate(cert_der)
    pub_key = cert_obj.public_key()
    pub_numbers = pub_key.public_numbers()
    mod_bytes = pub_numbers.n.to_bytes(
        (pub_numbers.n.bit_length() + 7) // 8, byteorder="big")
    exp_bytes = pub_numbers.e.to_bytes(
        (pub_numbers.e.bit_length() + 7) // 8, byteorder="big")

    modulus_b64 = _wrap_b64(mod_bytes)
    exponent_b64 = base64.b64encode(exp_bytes).decode("ascii")
    cert_b64 = _wrap_b64(cert_der)

    # ================================================================
    # 5. Construir Signature completa como elemento lxml
    # ================================================================
    nsmap = {None: DSIG_NS}
    signature = etree.Element(f"{{{DSIG_NS}}}Signature", nsmap=nsmap)

    # Re-parsear el SignedInfo con namespace correcto para insertarlo
    si_element = etree.fromstring(si_c14n.encode("utf-8"))
    signature.append(si_element)

    sig_value = etree.SubElement(signature, f"{{{DSIG_NS}}}SignatureValue")
    sig_value.text = firma_b64

    key_info = etree.SubElement(signature, f"{{{DSIG_NS}}}KeyInfo")
    key_value = etree.SubElement(key_info, f"{{{DSIG_NS}}}KeyValue")
    rsa_key_value = etree.SubElement(key_value, f"{{{DSIG_NS}}}RSAKeyValue")
    etree.SubElement(rsa_key_value, f"{{{DSIG_NS}}}Modulus").text = modulus_b64
    etree.SubElement(rsa_key_value, f"{{{DSIG_NS}}}Exponent").text = exponent_b64

    x509_data = etree.SubElement(key_info, f"{{{DSIG_NS}}}X509Data")
    etree.SubElement(x509_data, f"{{{DSIG_NS}}}X509Certificate").text = cert_b64

    # ================================================================
    # 6. Insertar Signature en el parent
    # ================================================================
    if parent_element is not None:
        parent_element.append(signature)

    return signature
