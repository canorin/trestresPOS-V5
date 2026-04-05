"""
Envía los sobres firmados al SII en ambiente de certificación.

Proceso para DTEs (facturas, NC, ND, guías):
1. Obtener token via SOAP (CrSeed + GetTokenFromSeed)
2. Enviar via DTEUpload (maullin.sii.cl)

Proceso para Boletas:
1. Obtener token via REST API (boleta.electronica.semilla + boleta.electronica.token)
2. Enviar via REST API (pangal.sii.cl/boleta.electronica.envio)

IMPORTANTE: Las boletas usan un endpoint completamente diferente al de DTEs.
"""
import sys
import base64
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from crumbpos.config import settings
from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
from crumbpos.core.sii_client.autenticacion import obtener_token, obtener_token_boleta
from crumbpos.core.sii_client.envio import enviar_dte, enviar_boleta
from facturacion_electronica.firma import Firma


PFX_PATH = settings.CERT_DIR / "17586255-2.pfx"
PFX_PASSWORD = "2656"
RUT_EMISOR = "77051056-2"
RUT_FIRMANTE = "17586255-2"


def cargar_firma_lib():
    """Carga la firma electrónica usando la librería facturacion_electronica.
    Necesaria para firmar la semilla de boletas."""
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


def main():
    output_dir = settings.OUTPUT_DIR

    # Cargar certificado (para DTEs via SOAP)
    print("Cargando certificado digital...")
    private_key_pem, cert_pem, cert_der = cargar_certificado_pfx(str(PFX_PATH), PFX_PASSWORD)
    print("  OK\n")

    # Sobres a enviar
    sobres_dte = [
        "EnvioDTE_SetBasico_firmado.xml",
        "EnvioDTE_SetExentas_firmado.xml",
        "EnvioDTE_SetGuias_firmado.xml",
    ]
    sobres_boleta = [
        "EnvioBOLETA_Set_firmado.xml",
    ]

    resultados = []

    # === ENVIAR DTEs (SOAP/DTEUpload) ===
    sobres_dte_existentes = [s for s in sobres_dte if (output_dir / s).exists()]
    if sobres_dte_existentes:
        print("Obteniendo token SOAP del SII (DTEs)...")
        token_dte = obtener_token(private_key_pem, cert_der)
        print(f"  Token: {token_dte}\n")

        for nombre in sobres_dte_existentes:
            filepath = output_dir / nombre
            print(f"Enviando {nombre} (DTEUpload)...")
            with open(filepath, "rb") as f:
                xml_bytes = f.read()

            resultado = enviar_dte(
                xml_bytes=xml_bytes,
                token=token_dte,
                rut_emisor=RUT_EMISOR,
                rut_envia=RUT_FIRMANTE,
            )

            print(f"  Status: {resultado['status']}")
            print(f"  TrackID: {resultado['track_id']}")
            print(f"  Glosa: {resultado['glosa']}")
            print()
            resultados.append((nombre, resultado.get("track_id")))

    # === ENVIAR BOLETAS (REST API) ===
    sobres_boleta_existentes = [s for s in sobres_boleta if (output_dir / s).exists()]
    if sobres_boleta_existentes:
        print("=" * 60)
        print("BOLETAS - Usando REST API (boleta.electronica)")
        print("=" * 60)

        print("Cargando firma para semilla de boletas...")
        firma = cargar_firma_lib()
        print("  OK\n")

        print("Obteniendo token REST del SII (Boletas)...")
        token_boleta = obtener_token_boleta(firma)
        print(f"  Token boleta: {token_boleta}\n")

        for nombre in sobres_boleta_existentes:
            filepath = output_dir / nombre
            print(f"Enviando {nombre} (REST API boleta.electronica.envio)...")
            with open(filepath, "rb") as f:
                xml_bytes = f.read()

            resultado = enviar_boleta(
                xml_bytes=xml_bytes,
                token=token_boleta,
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

    # Resumen
    print("=" * 60)
    print("RESUMEN DE ENVIOS")
    print("=" * 60)
    for nombre, track_id in resultados:
        status = "OK" if track_id else "ERROR"
        print(f"  {nombre}: {status} - TrackID: {track_id}")


if __name__ == "__main__":
    main()
