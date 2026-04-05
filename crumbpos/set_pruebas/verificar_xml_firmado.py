"""
Verificación de firma desde archivo XML (simula lo que hace el SII).

Lee el XML firmado desde disco, parsea, y verifica:
1. Digest de cada Documento
2. Firma RSA de cada DTE
3. Digest del SetDTE
4. Firma RSA del sobre

Compara distintos métodos de C14N para diagnosticar cuál usa el SII.
"""
import base64
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lxml import etree
from cryptography.x509 import load_der_x509_certificate
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

SII_NS = "http://www.sii.cl/SiiDte"
DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


def compute_digest(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


def verify_rsa(pub_key, sig_bytes, data):
    """Intenta verificar firma RSA-SHA1."""
    try:
        pub_key.verify(sig_bytes, data, padding.PKCS1v15(), hashes.SHA1())
        return True
    except Exception:
        return False


def get_pub_key(signature_elem):
    """Extrae la clave pública del certificado en la firma."""
    cert_b64 = signature_elem.find(f".//{{{DSIG_NS}}}X509Certificate").text.strip()
    cert_der = base64.b64decode(cert_b64)
    cert = load_der_x509_certificate(cert_der)
    return cert.public_key()


def try_digest_methods(element, expected_digest, label=""):
    """Prueba múltiples métodos de C14N para un elemento."""
    results = {}

    # ── Método A: Nuestro método (serialize, strip xsi, reparse, C14N) ──
    xml_str = etree.tostring(element, encoding="unicode")
    xml_str_a = re.sub(r'\s+xmlns:xsi="[^"]*"', '', xml_str)
    xml_str_a = re.sub(r'\s+xsi:schemaLocation="[^"]*"', '', xml_str_a)
    if f'xmlns="{SII_NS}"' not in xml_str_a:
        tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag
        xml_str_a = re.sub(rf'^(<{tag})\b', rf'\1 xmlns="{SII_NS}"', xml_str_a)
    doc_a = etree.fromstring(xml_str_a.encode("utf-8"))
    c14n_a = etree.tostring(doc_a, method="c14n")
    d_a = compute_digest(c14n_a)
    results["A:serialize-strip_xsi-reparse-C14N"] = d_a

    # ── Método B: serialize-reparse con TODO (xmlns + xmlns:xsi) ──
    xml_str_b = etree.tostring(element, encoding="unicode")
    if f'xmlns="{SII_NS}"' not in xml_str_b:
        tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag
        xml_str_b = re.sub(rf'^(<{tag})\b', rf'\1 xmlns="{SII_NS}"', xml_str_b)
    doc_b = etree.fromstring(xml_str_b.encode("utf-8"))
    c14n_b = etree.tostring(doc_b, method="c14n")
    d_b = compute_digest(c14n_b)
    results["B:serialize-reparse-C14N(con_xsi)"] = d_b

    # ── Método C: C14N in-tree (directo) ──
    c14n_c = etree.tostring(element, method="c14n")
    d_c = compute_digest(c14n_c)
    results["C:C14N-in-tree"] = d_c

    # ── Método D: C14N exclusive ──
    try:
        c14n_d = etree.tostring(element, method="c14n", exclusive=True)
        d_d = compute_digest(c14n_d)
        results["D:C14N-exclusive"] = d_d
    except Exception as e:
        results["D:C14N-exclusive"] = f"ERROR: {e}"

    # ── Método E: serialize-reparse SIN ningún namespace ──
    xml_str_e = etree.tostring(element, encoding="unicode")
    xml_str_e = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', xml_str_e)
    doc_e = etree.fromstring(xml_str_e.encode("utf-8"))
    c14n_e = etree.tostring(doc_e, method="c14n")
    d_e = compute_digest(c14n_e)
    results["E:serialize-strip_ALL_ns-reparse"] = d_e

    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"  Digest esperado: {expected_digest}")
    print(f"{'─'*60}")
    for method, digest in results.items():
        match = "✅" if digest == expected_digest else "❌"
        print(f"  {match} {method}: {digest}")

    # Mostrar primeros bytes del método A para diagnóstico
    if d_a != expected_digest:
        print(f"\n  C14N método A (primeros 300 chars):")
        print(f"  {c14n_a[:300].decode('utf-8', errors='replace')}")

    return results


def try_signedinfo_methods(signed_info, sig_bytes, pub_key, label=""):
    """Prueba múltiples métodos de C14N para SignedInfo."""
    results = {}

    # ── Método A: C14N in-tree ──
    si_c14n_a = etree.tostring(signed_info, method="c14n")
    ok_a = verify_rsa(pub_key, sig_bytes, si_c14n_a)
    results["A:C14N-in-tree"] = ok_a

    # ── Método B: serialize, strip xsi, reparse, C14N ──
    si_str = etree.tostring(signed_info, encoding="unicode")
    si_str_b = re.sub(r'\s+xmlns:xsi="[^"]*"', '', si_str)
    si_reparsed = etree.fromstring(si_str_b.encode("utf-8"))
    si_c14n_b = etree.tostring(si_reparsed, method="c14n")
    ok_b = verify_rsa(pub_key, sig_bytes, si_c14n_b)
    results["B:serialize-strip_xsi-reparse-C14N"] = ok_b

    # ── Método C: serialize-reparse sin cambios ──
    si_reparsed_c = etree.fromstring(etree.tostring(signed_info))
    si_c14n_c = etree.tostring(si_reparsed_c, method="c14n")
    ok_c = verify_rsa(pub_key, sig_bytes, si_c14n_c)
    results["C:serialize-reparse-C14N"] = ok_c

    # ── Método D: C14N exclusive ──
    try:
        si_c14n_d = etree.tostring(signed_info, method="c14n", exclusive=True)
        ok_d = verify_rsa(pub_key, sig_bytes, si_c14n_d)
        results["D:C14N-exclusive"] = ok_d
    except Exception as e:
        results[f"D:C14N-exclusive"] = f"ERROR: {e}"

    # ── Método E: Nuestro método (bare + inject xmlns) ──
    si_bare = etree.Element("SignedInfo")
    for child in signed_info:
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        si_bare.append(child.__deepcopy__(True))
    # Reconstruir bare elements sin namespace
    si_str_bare = etree.tostring(signed_info, encoding="unicode")
    # Remove all namespace prefixes
    si_str_bare = re.sub(r'<(?:ds:)?(\w)', r'<\1', si_str_bare)
    si_str_bare = re.sub(r'</(?:ds:)?(\w)', r'</\1', si_str_bare)
    si_str_bare = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', si_str_bare)
    # C14N bare
    si_bare_parsed = etree.fromstring(si_str_bare.encode("utf-8"))
    si_bare_c14n = etree.tostring(si_bare_parsed, method="c14n").decode()
    # Inject xmlns dsig
    si_with_ns = si_bare_c14n.replace(
        "<SignedInfo>", f'<SignedInfo xmlns="{DSIG_NS}">')
    ok_e = verify_rsa(pub_key, sig_bytes, si_with_ns.encode("utf-8"))
    results["E:bare-inject-xmlns-dsig"] = ok_e

    # ── Método F: bare + inject xmlns dsig + xmlns:xsi ──
    si_with_ns_xsi = si_bare_c14n.replace(
        "<SignedInfo>",
        f'<SignedInfo xmlns="{DSIG_NS}"'
        f' xmlns:xsi="{XSI_NS}">')
    ok_f = verify_rsa(pub_key, sig_bytes, si_with_ns_xsi.encode("utf-8"))
    results["F:bare-inject-xmlns-dsig+xsi"] = ok_f

    print(f"\n{'─'*60}")
    print(f"  SignedInfo RSA: {label}")
    print(f"{'─'*60}")
    for method, result in results.items():
        if isinstance(result, bool):
            symbol = "✅" if result else "❌"
            print(f"  {symbol} {method}")
        else:
            print(f"  ⚠️  {method}: {result}")

    # Show what the in-tree C14N looks like
    if not ok_a:
        print(f"\n  SignedInfo C14N in-tree:")
        print(f"  {si_c14n_a.decode('utf-8')[:500]}")

    return results


def verify_file(xml_path: str):
    """Verifica todas las firmas en un archivo XML firmado."""
    print(f"\n{'='*70}")
    print(f"VERIFICANDO: {Path(xml_path).name}")
    print(f"{'='*70}")

    tree = etree.parse(xml_path)
    root = tree.getroot()

    # Detectar namespace del root
    root_ns = root.tag.split('}')[0].strip('{') if '}' in root.tag else ""
    ns = {"sii": SII_NS, "ds": DSIG_NS}

    print(f"\nRoot tag: {root.tag}")
    print(f"Root attribs: {dict(root.attrib)}")

    # Encontrar todos los DTE
    dtes = root.findall(f".//{{{SII_NS}}}DTE")
    if not dtes:
        dtes = root.findall(".//DTE")

    print(f"DTEs encontrados: {len(dtes)}")

    signatures = root.findall(f".//{{{DSIG_NS}}}Signature")
    print(f"Signatures totales: {len(signatures)}")

    # ── Verificar firma de cada DTE ──
    for i, dte in enumerate(dtes):
        # Encontrar Documento dentro del DTE
        documento = None
        sig = None
        for child in dte:
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == "Documento":
                documento = child
            elif tag == "Signature":
                sig = child

        if documento is None:
            print(f"\n⚠️ DTE {i}: No se encontró Documento")
            continue

        doc_id = documento.get("ID", "?")
        print(f"\n{'═'*70}")
        print(f"DTE {i}: Documento ID={doc_id}")
        print(f"{'═'*70}")

        if sig is None:
            print("  ⚠️ No tiene Signature")
            continue

        # Extraer digest esperado
        ref = sig.find(f".//{{{DSIG_NS}}}Reference")
        digest_expected = ref.find(f".//{{{DSIG_NS}}}DigestValue").text.strip()
        ref_uri = ref.get("URI", "")

        print(f"  Reference URI: {ref_uri}")

        # Probar métodos de digest
        try_digest_methods(documento, digest_expected, f"Documento ID={doc_id}")

        # Verificar firma RSA del SignedInfo
        signed_info = sig.find(f"{{{DSIG_NS}}}SignedInfo")
        sig_value_text = sig.find(f"{{{DSIG_NS}}}SignatureValue").text.strip()
        sig_bytes = base64.b64decode(sig_value_text)
        pub_key = get_pub_key(sig)

        try_signedinfo_methods(signed_info, sig_bytes, pub_key, f"DTE {doc_id}")

    # ── Verificar firma del sobre (SetDTE) ──
    set_dte = root.find(f"{{{SII_NS}}}SetDTE")
    if set_dte is None:
        set_dte = root.find("SetDTE")

    if set_dte is not None:
        # La firma del sobre es la última Signature (hija directa de root)
        sobre_sig = None
        for child in root:
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == "Signature":
                sobre_sig = child

        if sobre_sig is not None:
            print(f"\n{'═'*70}")
            print(f"SOBRE: SetDTE ID={set_dte.get('ID', '?')}")
            print(f"{'═'*70}")

            ref = sobre_sig.find(f".//{{{DSIG_NS}}}Reference")
            digest_expected = ref.find(f".//{{{DSIG_NS}}}DigestValue").text.strip()

            # Para el sobre, probar con xmlns:xsi incluido
            try_digest_methods(set_dte, digest_expected, f"SetDTE (sobre)")

            signed_info = sobre_sig.find(f"{{{DSIG_NS}}}SignedInfo")
            sig_value_text = sobre_sig.find(f"{{{DSIG_NS}}}SignatureValue").text.strip()
            sig_bytes = base64.b64decode(sig_value_text)
            pub_key = get_pub_key(sobre_sig)

            try_signedinfo_methods(signed_info, sig_bytes, pub_key, "SetDTE (sobre)")


def verify_reference(xml_path: str):
    """Verifica la factura de referencia NAGOY."""
    print(f"\n{'='*70}")
    print(f"REFERENCIA: {Path(xml_path).name}")
    print(f"{'='*70}")

    tree = etree.parse(xml_path)
    root = tree.getroot()

    documento = root.find(f".//{{{SII_NS}}}Documento")
    dte_sig = root.findall(f".//{{{DSIG_NS}}}Signature")[0]
    ref = dte_sig.find(f".//{{{DSIG_NS}}}Reference")
    digest_expected = ref.find(f".//{{{DSIG_NS}}}DigestValue").text.strip()

    print(f"Documento ID: {documento.get('ID')}")
    try_digest_methods(documento, digest_expected, "NAGOY Documento (referencia)")

    signed_info = dte_sig.find(f"{{{DSIG_NS}}}SignedInfo")
    sig_value_text = dte_sig.find(f"{{{DSIG_NS}}}SignatureValue").text.strip()
    sig_bytes = base64.b64decode(sig_value_text)
    pub_key = get_pub_key(dte_sig)
    try_signedinfo_methods(signed_info, sig_bytes, pub_key, "NAGOY DTE (referencia)")


if __name__ == "__main__":
    # 1. Verificar referencia NAGOY
    nagoy = Path("/Users/matiasbanados/Downloads/33_76096747-5_66051_20260324_ce84afb8-8563-4333-af53-a04a4f1f64b3.xml")
    if nagoy.exists():
        verify_reference(str(nagoy))

    # 2. Verificar nuestros XMLs firmados
    output_dir = Path("/Users/matiasbanados/POS NANUC/output")
    for xml_file in sorted(output_dir.glob("*_firmado.xml")):
        verify_file(str(xml_file))
