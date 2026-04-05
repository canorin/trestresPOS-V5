"""
Envía sobres firmados al SII — GRUPO TRESTRES SPA.

Uso:
  python enviar_sii.py dtes         # Enviar DTEs (básico + guías)
  python enviar_sii.py libros       # Enviar libros
  python enviar_sii.py todo         # Enviar todo

COPIA EXACTA del motor de nuevapostulacion/enviar_sii.py
Solo cambian: datos empresa y rutas.
"""
import sys
import base64
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "grupotrestres"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
from crumbpos.core.sii_client.autenticacion import obtener_token
from crumbpos.core.sii_client.envio import enviar_dte

PFX_PATH = COMPANY_DIR / "certificado" / "17586255-2.pfx"
PFX_PASSWORD = "2656"
RUT_EMISOR = "77829149-5"
RUT_FIRMANTE = "17586255-2"
OUTPUT_DIR = COMPANY_DIR / "output"


def enviar_sobres_dte(nombres: list):
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


def main():
    modo = sys.argv[1] if len(sys.argv) > 1 else "todo"

    resultados = []

    if modo in ("dtes", "todo"):
        print("=" * 60)
        print("ENVIANDO DTEs")
        print("=" * 60)
        resultados += enviar_sobres_dte([
            "EnvioDTE_SetBasico_firmado.xml",
            "EnvioDTE_SetGuias_firmado.xml",
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
        status = "[OK]" if track_id else "[ERROR]"
        print(f"  {status} {nombre}: TrackID={track_id}")


if __name__ == "__main__":
    main()
