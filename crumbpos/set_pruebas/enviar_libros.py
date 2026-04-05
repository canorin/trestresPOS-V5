"""Envía los Libros firmados al SII (Compras y Guías)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from crumbpos.config import settings
from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
from crumbpos.core.sii_client.autenticacion import obtener_token
from crumbpos.core.sii_client.envio import enviar_dte


PFX_PATH = settings.CERT_DIR / "17586255-2.pfx"
PFX_PASSWORD = "2656"
RUT_EMISOR = "77051056-2"
RUT_FIRMANTE = "17586255-2"


def main():
    output_dir = settings.OUTPUT_DIR

    print("Cargando certificado digital...")
    private_key_pem, cert_pem, cert_der = cargar_certificado_pfx(str(PFX_PATH), PFX_PASSWORD)
    print("  OK\n")

    print("Obteniendo token SOAP del SII...")
    token = obtener_token(private_key_pem, cert_der)
    print(f"  Token: {token}\n")

    libros = [
        "LibroCompras_firmado.xml",
        "LibroGuias_firmado.xml",
    ]

    resultados = []
    for nombre in libros:
        filepath = output_dir / nombre
        if not filepath.exists():
            print(f"  SKIP: {nombre} no existe")
            continue

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
        if resultado['status'] == 'ERROR':
            print(f"  Raw: {resultado['raw'][:1000]}")
        print()
        resultados.append((nombre, resultado.get("track_id")))

    # Resumen
    print("=" * 60)
    print("RESUMEN DE ENVIOS")
    print("=" * 60)
    for nombre, track_id in resultados:
        status = "OK" if track_id else "ERROR"
        print(f"  {nombre}: {status} - TrackID: {track_id}")


if __name__ == "__main__":
    main()
