"""
Envía sobres firmados al SII — Nueva Postulación trestresPOS.

Uso:
  python enviar_sii.py boletas      # Enviar solo boletas
  python enviar_sii.py dtes         # Enviar solo DTEs (básico, exentas, guías)
  python enviar_sii.py libros       # Enviar solo libros
  python enviar_sii.py todo         # Enviar todo
"""
import sys
import base64
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "nuevapostulacion"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from crumbpos.config import settings
from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
from crumbpos.core.sii_client.autenticacion import obtener_token, obtener_token_boleta
from crumbpos.core.sii_client.envio import enviar_dte, enviar_boleta
from facturacion_electronica.firma import Firma

PFX_PATH = settings.CERT_DIR / "17586255-2.pfx"
PFX_PASSWORD = "2656"
RUT_EMISOR = "77051056-2"
RUT_FIRMANTE = "17586255-2"
OUTPUT_DIR = COMPANY_DIR / "output"


def cargar_firma_lib():
    pfx_data = open(PFX_PATH, "rb").read()
    firma = Firma({
        "string_firma": base64.b64encode(pfx_data).decode(),
        "string_password": PFX_PASSWORD,
        "init_signature": True,
        "rut_firmante": RUT_FIRMANTE,
    })
    if not firma.firma_electronica:
        raise RuntimeError(f"Error cargando certificado: {firma.errores}")
    firma.verify = False
    return firma


def enviar_sobres_dte(nombres: list[str]):
    """Envía sobres DTE/Libros via SOAP DTEUpload."""
    existentes = [n for n in nombres if (OUTPUT_DIR / n).exists()]
    if not existentes:
        print("  No hay archivos para enviar")
        return []

    print("Cargando certificado...")
    private_key_pem, cert_pem, cert_der = cargar_certificado_pfx(str(PFX_PATH), PFX_PASSWORD)

    print("Obteniendo token SOAP del SII...")
    token = obtener_token(private_key_pem, cert_der)
    print(f"  Token: {token}\n")

    resultados = []
    for nombre in existentes:
        filepath = OUTPUT_DIR / nombre
        print(f"Enviando {nombre}...")
        with open(filepath, "rb") as f:
            xml_bytes = f.read()

        resultado = enviar_dte(
            xml_bytes=xml_bytes,
            token=token,
            rut_emisor=RUT_EMISOR,
            rut_envia=RUT_FIRMANTE,
        )

        print(f"  Status: {resultado['status']}")
        print(f"  TrackID: {resultado['track_id']}")
        print(f"  Glosa: {resultado['glosa']}")
        print()
        resultados.append((nombre, resultado.get("track_id")))

    return resultados


def enviar_sobres_boleta(nombres: list[str]):
    """Envía sobres Boleta via REST API."""
    existentes = [n for n in nombres if (OUTPUT_DIR / n).exists()]
    if not existentes:
        print("  No hay archivos de boleta para enviar")
        return []

    print("Cargando firma para boletas...")
    firma = cargar_firma_lib()

    print("Obteniendo token REST del SII (Boletas)...")
    token = obtener_token_boleta(firma)
    print(f"  Token boleta: {token}\n")

    resultados = []
    for nombre in existentes:
        filepath = OUTPUT_DIR / nombre
        print(f"Enviando {nombre} (REST API)...")
        with open(filepath, "rb") as f:
            xml_bytes = f.read()

        resultado = enviar_boleta(
            xml_bytes=xml_bytes,
            token=token,
            rut_emisor=RUT_EMISOR,
            rut_envia=RUT_FIRMANTE,
        )

        print(f"  Status: {resultado['status']}")
        print(f"  TrackID: {resultado.get('track_id')}")
        print(f"  Estado SII: {resultado.get('estado')}")
        if resultado.get('estadistica'):
            print(f"  Estadistica: {resultado['estadistica']}")
        print()
        resultados.append((nombre, resultado.get("track_id")))

    return resultados


def main():
    modo = sys.argv[1] if len(sys.argv) > 1 else "todo"

    resultados = []

    if modo in ("boletas", "todo"):
        print("=" * 60)
        print("ENVIANDO BOLETAS")
        print("=" * 60)
        resultados += enviar_sobres_boleta(["EnvioBOLETA_Set_firmado.xml"])

    if modo in ("dtes", "todo"):
        print("=" * 60)
        print("ENVIANDO DTEs")
        print("=" * 60)
        # NOTA: SET GUÍA (4758675) YA APROBADO SOK — NO re-enviar
        resultados += enviar_sobres_dte([
            "EnvioDTE_SetBasico_firmado.xml",
            "EnvioDTE_SetExentas_firmado.xml",
            # "EnvioDTE_SetGuias_firmado.xml",  # SOK ✅ — ya aprobado, track 0246379780
        ])

    if modo in ("libros", "todo"):
        print("=" * 60)
        print("ENVIANDO LIBROS")
        print("=" * 60)
        resultados += enviar_sobres_dte([
            "LIBRODEVENTAS_firmado.xml",
            "LIBRODECOMPRAS_firmado.xml",
            "LIBRODEGUIAS_firmado.xml",
        ])

    print("=" * 60)
    print("RESUMEN")
    print("=" * 60)
    for nombre, track_id in resultados:
        status = "✅" if track_id else "❌"
        print(f"  {status} {nombre}: TrackID={track_id}")


if __name__ == "__main__":
    main()
