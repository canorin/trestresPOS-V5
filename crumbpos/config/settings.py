"""Configuración central de CrumbPOS."""
from pathlib import Path

# Directorios base
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
CERT_DIR = BASE_DIR / "data" / "certificados"
# CAF_DIR legacy (file-based) — en producción los CAFs se suben via API a la DB
CAF_DIR = BASE_DIR / "data" / "caf"

# RUT del SII como receptor de envíos en certificación
RUT_SII = "60803000-K"

# Tipos de DTE
TIPO_DTE = {
    33: "Factura Electrónica",
    34: "Factura No Afecta o Exenta Electrónica",
    39: "Boleta Electrónica",
    41: "Boleta Exenta Electrónica",
    46: "Factura de Compra Electrónica",
    52: "Guía de Despacho Electrónica",
    56: "Nota de Débito Electrónica",
    61: "Nota de Crédito Electrónica",
}

# IVA
TASA_IVA = 19

# Ambiente
AMBIENTE = "certificacion"  # "certificacion" o "produccion"

# URLs SII
SII_URLS = {
    "certificacion": {
        "seed": "https://maullin.sii.cl/DTEWS/CrSeed.jws",
        "token": "https://maullin.sii.cl/DTEWS/GetTokenFromSeed.jws",
        "upload": "https://maullin.sii.cl/cgi_dte/UPL/DTEUpload",
        "estado_envio": "https://maullin.sii.cl/DTEWS/QueryEstUp.jws",
        "estado_dte": "https://maullin.sii.cl/DTEWS/QueryEstDte.jws",
        # Boleta REST API (endpoints separados del SOAP tradicional)
        "boleta_seed": "https://apicert.sii.cl/recursos/v1/boleta.electronica.semilla",
        "boleta_token": "https://apicert.sii.cl/recursos/v1/boleta.electronica.token",
        "boleta_upload": "https://pangal.sii.cl/recursos/v1/boleta.electronica.envio",
        "boleta_consumo": "https://apicert.sii.cl/recursos/v1/boleta.electronica.consumo",
        "boleta_estado": "https://apicert.sii.cl/recursos/v1/boleta.electronica.envio",
    },
    "produccion": {
        "seed": "https://palena.sii.cl/DTEWS/CrSeed.jws",
        "token": "https://palena.sii.cl/DTEWS/GetTokenFromSeed.jws",
        "upload": "https://palena.sii.cl/cgi_dte/UPL/DTEUpload",
        "estado_envio": "https://palena.sii.cl/DTEWS/QueryEstUp.jws",
        "estado_dte": "https://palena.sii.cl/DTEWS/QueryEstDte.jws",
        # Boleta REST API
        "boleta_seed": "https://api.sii.cl/recursos/v1/boleta.electronica.semilla",
        "boleta_token": "https://api.sii.cl/recursos/v1/boleta.electronica.token",
        "boleta_upload": "https://rahue.sii.cl/recursos/v1/boleta.electronica.envio",
        "boleta_consumo": "https://api.sii.cl/recursos/v1/boleta.electronica.consumo",
        "boleta_estado": "https://api.sii.cl/recursos/v1/boleta.electronica.envio",
    },
}


def get_sii_url(servicio: str) -> str:
    """Obtiene la URL del SII según ambiente actual."""
    return SII_URLS[AMBIENTE][servicio]
