"""Test rápido: verifica que la firma corregida produce el digest correcto
usando como referencia la factura real de NAGOY SPA."""
import base64
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lxml import etree

from crumbpos.config import settings

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"

# XML de referencia (factura real). Archivo externo, no vive en el repo:
# indicar su ruta con CRUMBPOS_REF_XML.
XML_PATH = Path(os.getenv("CRUMBPOS_REF_XML", str(settings.OUTPUT_DIR / "referencia.xml")))


def test_digest_con_factura_real():
    """Verifica que nuestro método de digest produce el resultado correcto."""
    tree = etree.parse(str(XML_PATH))
    root = tree.getroot()
    ns = {"sii": SII_NS, "ds": DSIG_NS}

    documento = root.find(".//sii:Documento", ns)
    dte_sig = root.findall(f".//{{{DSIG_NS}}}Signature")[0]
    ref = dte_sig.find(f".//{{{DSIG_NS}}}Reference")
    digest_expected = ref.find(f".//{{{DSIG_NS}}}DigestValue").text

    # Simular nuestro nuevo código de firma_digital.py
    xml_str = etree.tostring(documento, encoding="unicode")

    # Quitar xmlns:xsi
    xml_str = re.sub(r'\s+xmlns:xsi="[^"]*"', '', xml_str)
    # Quitar xsi:schemaLocation
    xml_str = re.sub(r'\s+xsi:schemaLocation="[^"]*"', '', xml_str)

    # Asegurar xmlns="http://www.sii.cl/SiiDte"
    if f'xmlns="{SII_NS}"' not in xml_str:
        tag = "Documento"
        xml_str = re.sub(rf'^(<{tag})\b', rf'\1 xmlns="{SII_NS}"', xml_str)

    doc_reparsed = etree.fromstring(xml_str.encode("utf-8"))
    c14n_bytes = etree.tostring(doc_reparsed, method="c14n")
    digest = base64.b64encode(hashlib.sha1(c14n_bytes).digest()).decode()

    print(f"Digest esperado:  {digest_expected}")
    print(f"Digest computado: {digest}")
    print(f"MATCH: {'✅ CORRECTO' if digest == digest_expected else '❌ INCORRECTO'}")
    print()


def test_digest_elemento_sin_namespace():
    """Simula un Documento creado sin namespace (como nuestro generador)
    que luego se inserta en un EnvioDTE con namespace."""

    # Crear como nuestro generador
    documento = etree.Element("Documento", ID="F1T33")
    enc = etree.SubElement(documento, "Encabezado")
    id_doc = etree.SubElement(enc, "IdDoc")
    etree.SubElement(id_doc, "TipoDTE").text = "33"
    etree.SubElement(id_doc, "Folio").text = "1"
    etree.SubElement(id_doc, "FchEmis").text = "2026-03-25"
    etree.SubElement(documento, "TmstFirma").text = "2026-03-25T12:00:00"

    # Envolver en DTE sin namespace
    dte = etree.Element("DTE", version="1.0")
    dte.append(documento)

    # Insertar en EnvioDTE con namespace
    nsmap = {None: SII_NS, "xsi": XSI_NS}
    envio = etree.Element("EnvioDTE", nsmap=nsmap, version="1.0")
    set_dte = etree.SubElement(envio, "SetDTE", ID="SetDoc")
    set_dte.append(dte)

    print("=" * 60)
    print("TEST: Documento sin namespace dentro de EnvioDTE")
    print("=" * 60)

    # Mostrar cómo se serializa el Documento desde dentro del árbol
    raw_serialize = etree.tostring(documento, encoding="unicode")
    print(f"\nSerialize crudo del Documento:")
    print(raw_serialize[:200])

    # Aplicar nuestro fix
    xml_str = raw_serialize
    xml_str = re.sub(r'\s+xmlns:xsi="[^"]*"', '', xml_str)
    xml_str = re.sub(r'\s+xsi:schemaLocation="[^"]*"', '', xml_str)

    if f'xmlns="{SII_NS}"' not in xml_str:
        tag = "Documento"
        xml_str = re.sub(rf'^(<{tag})\b', rf'\1 xmlns="{SII_NS}"', xml_str)

    print(f"\nDespués del fix:")
    print(xml_str[:200])

    doc_reparsed = etree.fromstring(xml_str.encode("utf-8"))
    c14n_bytes = etree.tostring(doc_reparsed, method="c14n")
    print(f"\nC14N final:")
    print(c14n_bytes.decode("utf-8"))

    # Verificar que el C14N tiene xmlns="SII" pero NO xmlns:xsi
    c14n_str = c14n_bytes.decode("utf-8")
    has_sii = f'xmlns="{SII_NS}"' in c14n_str
    has_xsi = 'xmlns:xsi=' in c14n_str
    has_empty_xmlns = 'xmlns=""' in c14n_str

    print(f"\n✅ Tiene xmlns SII: {has_sii}")
    print(f"{'✅' if not has_xsi else '❌'} Sin xmlns:xsi: {not has_xsi}")
    print(f"{'✅' if not has_empty_xmlns else '❌'} Sin xmlns='': {not has_empty_xmlns}")


def test_signedinfo():
    """Verifica que el SignedInfo se construye correctamente."""
    print("\n" + "=" * 60)
    print("TEST: Construcción de SignedInfo")
    print("=" * 60)

    signed_info = etree.Element("SignedInfo")
    etree.SubElement(signed_info, "CanonicalizationMethod",
        Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    etree.SubElement(signed_info, "SignatureMethod",
        Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1")
    reference = etree.SubElement(signed_info, "Reference", URI="#F1T33")
    transforms = etree.SubElement(reference, "Transforms")
    etree.SubElement(transforms, "Transform",
        Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    etree.SubElement(reference, "DigestMethod",
        Algorithm="http://www.w3.org/2000/09/xmldsig#sha1")
    etree.SubElement(reference, "DigestValue").text = "test123="

    si_c14n = etree.tostring(signed_info, method="c14n").decode()
    si_c14n = si_c14n.replace("<SignedInfo>", f'<SignedInfo xmlns="{DSIG_NS}">')

    print(f"\nSignedInfo C14N con xmlns inyectado:")
    print(si_c14n)

    has_dsig = f'xmlns="{DSIG_NS}"' in si_c14n
    has_xsi = 'xmlns:xsi=' in si_c14n
    has_transforms = '<Transforms>' in si_c14n and '<Transform ' in si_c14n

    print(f"\n✅ Tiene xmlns dsig: {has_dsig}")
    print(f"{'✅' if not has_xsi else '❌'} Sin xmlns:xsi: {not has_xsi}")
    print(f"{'✅' if has_transforms else '❌'} Tiene Transforms: {has_transforms}")


def test_verificar_firma_referencia():
    """Verifica la firma RSA completa de la factura real."""
    print("\n" + "=" * 60)
    print("TEST: Verificación firma RSA de factura real")
    print("=" * 60)

    tree = etree.parse(str(XML_PATH))
    root = tree.getroot()
    ns = {"sii": SII_NS, "ds": DSIG_NS}

    dte_sig = root.findall(f".//{{{DSIG_NS}}}Signature")[0]
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

    # Nuestro método: serialize-reparse SignedInfo, C14N, sin xmlns:xsi
    si_serialized = etree.tostring(signed_info, encoding="unicode")
    si_serialized = re.sub(r'\s+xmlns:xsi="[^"]*"', '', si_serialized)
    si_reparsed = etree.fromstring(si_serialized.encode("utf-8"))
    si_c14n = etree.tostring(si_reparsed, method="c14n")
    print(f"\nSignedInfo con nuestro método (serialize, strip xsi, reparse, C14N):")
    print(si_c14n.decode("utf-8")[:300])

    try:
        pub_key.verify(sig_bytes, si_c14n, padding.PKCS1v15(), hashes.SHA1())
        print("\n✅ Firma RSA verificada con nuestro método!")
    except Exception as e:
        print(f"\n❌ NO verifica: {e}")

        # Fallback: exclusive C14N
        si_c14n_exc = etree.tostring(signed_info, method="c14n", exclusive=True)
        try:
            pub_key.verify(sig_bytes, si_c14n_exc, padding.PKCS1v15(), hashes.SHA1())
            print("✅ Pero SÍ verifica con exclusive C14N")
            print(f"Exclusive: {si_c14n_exc.decode('utf-8')[:300]}")

            # Comparar
            if si_c14n != si_c14n_exc:
                print("\nDiferencias:")
                for i, (a, b) in enumerate(zip(si_c14n, si_c14n_exc)):
                    if a != b:
                        print(f"  Byte {i}: nuestro={chr(a)!r} exclusive={chr(b)!r}")
                        print(f"  Contexto nuestro:   ...{si_c14n[max(0,i-40):i+40].decode()}...")
                        print(f"  Contexto exclusive: ...{si_c14n_exc[max(0,i-40):i+40].decode()}...")
                        break
        except:
            print("❌ Tampoco verifica con exclusive C14N")


if __name__ == "__main__":
    test_digest_con_factura_real()
    test_digest_elemento_sin_namespace()
    test_signedinfo()
    test_verificar_firma_referencia()
