"""
Verificación de firma del documento de referencia (factura real recibida).
Compara diferentes métodos de cómputo de digest para encontrar el correcto.
"""
import base64
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lxml import etree

XML_PATH = Path("/Users/matiasbanados/Downloads/33_76096747-5_66051_20260324_ce84afb8-8563-4333-af53-a04a4f1f64b3.xml")

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"

def compute_digest(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


def main():
    tree = etree.parse(str(XML_PATH))
    root = tree.getroot()

    ns = {"sii": SII_NS, "ds": DSIG_NS}
    documento = root.find(".//sii:Documento", ns)
    dte_sig = root.findall(f".//{{{DSIG_NS}}}Signature")[0]
    ref = dte_sig.find(f".//{{{DSIG_NS}}}Reference")
    digest_expected = ref.find(f".//{{{DSIG_NS}}}DigestValue").text

    print(f"Documento tag: {documento.tag}")
    print(f"Documento ID: {documento.get('ID')}")
    print(f"DigestValue esperado: {digest_expected}")
    print()

    # ================================================================
    # MÉTODO 1: serialize-reparse-C14N (incluye xmlns y xmlns:xsi)
    # ================================================================
    xml_str = etree.tostring(documento)
    doc_reparsed = etree.fromstring(xml_str)
    c14n_1 = etree.tostring(doc_reparsed, method="c14n")
    d1 = compute_digest(c14n_1)
    print(f"M1 serialize-reparse (con xmlns + xmlns:xsi): {d1}  {'✅' if d1 == digest_expected else '❌'}")

    # ================================================================
    # MÉTODO 2: serialize, quitar xmlns:xsi, reparse, C14N
    # ================================================================
    xml_str_2 = etree.tostring(documento, encoding="unicode")
    # Solo quitar xmlns:xsi, mantener xmlns="http://www.sii.cl/SiiDte"
    xml_str_2 = re.sub(r'\s+xmlns:xsi="[^"]*"', '', xml_str_2)
    doc_reparsed_2 = etree.fromstring(xml_str_2.encode("utf-8"))
    c14n_2 = etree.tostring(doc_reparsed_2, method="c14n")
    d2 = compute_digest(c14n_2)
    print(f"M2 serialize-reparse (con xmlns, SIN xmlns:xsi): {d2}  {'✅' if d2 == digest_expected else '❌'}")

    # ================================================================
    # MÉTODO 3: serialize, quitar TODOS los ns, reparse, C14N
    # ================================================================
    xml_str_3 = etree.tostring(documento, encoding="unicode")
    xml_str_3 = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', xml_str_3)
    doc_reparsed_3 = etree.fromstring(xml_str_3.encode("utf-8"))
    c14n_3 = etree.tostring(doc_reparsed_3, method="c14n")
    d3 = compute_digest(c14n_3)
    print(f"M3 serialize-reparse (SIN ningún xmlns):        {d3}  {'✅' if d3 == digest_expected else '❌'}")

    # ================================================================
    # MÉTODO 4: serialize, quitar xsi:schemaLocation también
    # ================================================================
    xml_str_4 = etree.tostring(documento, encoding="unicode")
    xml_str_4 = re.sub(r'\s+xsi:schemaLocation="[^"]*"', '', xml_str_4)
    xml_str_4 = re.sub(r'\s+xmlns:xsi="[^"]*"', '', xml_str_4)
    doc_reparsed_4 = etree.fromstring(xml_str_4.encode("utf-8"))
    c14n_4 = etree.tostring(doc_reparsed_4, method="c14n")
    d4 = compute_digest(c14n_4)
    print(f"M4 sin xsi:schemaLocation y xmlns:xsi:          {d4}  {'✅' if d4 == digest_expected else '❌'}")

    # ================================================================
    # MÉTODO 5: C14N in-tree (tiene bug lxml xmlns="")
    # ================================================================
    c14n_5 = etree.tostring(documento, method="c14n")
    d5 = compute_digest(c14n_5)
    print(f"M5 C14N in-tree (con bug lxml xmlns=''):        {d5}  {'✅' if d5 == digest_expected else '❌'}")

    # ================================================================
    # MÉTODO 6: C14N exclusive (excluyendo namespaces no usados)
    # ================================================================
    try:
        c14n_6 = etree.tostring(documento, method="c14n", exclusive=True)
        d6 = compute_digest(c14n_6)
        print(f"M6 C14N exclusive:                              {d6}  {'✅' if d6 == digest_expected else '❌'}")
    except Exception as e:
        print(f"M6 C14N exclusive: ERROR - {e}")

    # ================================================================
    # MÉTODO 7: serialize-reparse con exclusive C14N
    # ================================================================
    try:
        xml_str_7 = etree.tostring(documento)
        doc_reparsed_7 = etree.fromstring(xml_str_7)
        c14n_7 = etree.tostring(doc_reparsed_7, method="c14n", exclusive=True)
        d7 = compute_digest(c14n_7)
        print(f"M7 serialize-reparse + exclusive C14N:          {d7}  {'✅' if d7 == digest_expected else '❌'}")
    except Exception as e:
        print(f"M7 exclusive: ERROR - {e}")

    # ================================================================
    # MÉTODO 8: Reconstruir con mismo whitespace pero namespace SII
    # El documento original usa tabs. Verificar si el C14N normaliza.
    # ================================================================

    # Mostrar primeros bytes de cada método para comparar
    print("\n" + "=" * 70)
    print("DETALLE DE BYTES C14N")
    print("=" * 70)

    print(f"\nM1 (primeros 300 chars):")
    print(c14n_1[:300].decode("utf-8", errors="replace"))

    print(f"\nM2 (primeros 300 chars):")
    print(c14n_2[:300].decode("utf-8", errors="replace"))

    print(f"\nM3 (primeros 300 chars):")
    print(c14n_3[:300].decode("utf-8", errors="replace"))

    print(f"\nM5 in-tree (primeros 300 chars):")
    print(c14n_5[:300].decode("utf-8", errors="replace"))

    # ================================================================
    # VERIFICACIÓN DE FIRMA RSA DEL SIGNEDINFO
    # ================================================================
    print("\n" + "=" * 70)
    print("VERIFICACIÓN FIRMA RSA")
    print("=" * 70)

    signed_info = dte_sig.find(f"{{{DSIG_NS}}}SignedInfo")
    sig_value_text = dte_sig.find(f"{{{DSIG_NS}}}SignatureValue").text.strip()
    sig_bytes = base64.b64decode(sig_value_text)

    cert_b64 = dte_sig.find(f".//{{{DSIG_NS}}}X509Certificate").text.strip()
    cert_der = base64.b64decode(cert_b64)

    from cryptography.x509 import load_der_x509_certificate
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    cert = load_der_x509_certificate(cert_der)
    pub_key = cert.public_key()

    # Método A: C14N in-tree del SignedInfo
    si_c14n_a = etree.tostring(signed_info, method="c14n")
    print(f"\nSignedInfo C14N in-tree:")
    print(si_c14n_a.decode("utf-8"))
    try:
        pub_key.verify(sig_bytes, si_c14n_a, padding.PKCS1v15(), hashes.SHA1())
        print("✅ Firma RSA verificada con C14N in-tree")
    except Exception as e:
        print(f"❌ NO verifica in-tree: {e}")

    # Método B: serialize-reparse
    si_c14n_b = etree.tostring(etree.fromstring(etree.tostring(signed_info)), method="c14n")
    print(f"\nSignedInfo C14N serialize-reparse:")
    print(si_c14n_b.decode("utf-8"))
    try:
        pub_key.verify(sig_bytes, si_c14n_b, padding.PKCS1v15(), hashes.SHA1())
        print("✅ Firma RSA verificada con serialize-reparse")
    except Exception as e:
        print(f"❌ NO verifica serialize-reparse: {e}")

    # Método C: C14N exclusive
    try:
        si_c14n_c = etree.tostring(signed_info, method="c14n", exclusive=True)
        print(f"\nSignedInfo C14N exclusive:")
        print(si_c14n_c.decode("utf-8"))
        try:
            pub_key.verify(sig_bytes, si_c14n_c, padding.PKCS1v15(), hashes.SHA1())
            print("✅ Firma RSA verificada con exclusive C14N")
        except Exception as e:
            print(f"❌ NO verifica exclusive: {e}")
    except Exception as e:
        print(f"C14N exclusive failed: {e}")

    # ================================================================
    # Check: ¿Nuestro Documento tiene Signature adentro al momento del digest?
    # ================================================================
    print("\n" + "=" * 70)
    print("¿EL DOCUMENTO INCLUYE LA SIGNATURE AL COMPUTAR EL DIGEST?")
    print("=" * 70)
    sig_inside = documento.find(f"{{{DSIG_NS}}}Signature")
    print(f"Signature dentro de Documento: {'SÍ' if sig_inside is not None else 'NO'}")
    # La Signature está en el DTE, NO en el Documento
    dte_elem = root.find(".//sii:DTE", ns)
    sig_in_dte = dte_elem.find(f"{{{DSIG_NS}}}Signature")
    print(f"Signature dentro de DTE: {'SÍ' if sig_in_dte is not None else 'NO'}")
    print(f"Hijos de DTE: {[c.tag.split('}')[-1] for c in dte_elem]}")


if __name__ == "__main__":
    main()
