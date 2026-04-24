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
# DEPRECATED global. Se mantiene solo como fallback para scripts legacy
# (set_pruebas/*, certificacion/nuevapostulacion/*). En el core SII y en la
# API multi-tenant, el ambiente se resuelve dinámicamente desde la empresa
# (Empresa.ambiente_sii / EmpresaRegistro.ambiente_activo) y se pasa como
# parámetro a get_sii_url(). No agregar nuevos usos de esta constante.
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


def get_sii_url(servicio: str, ambiente: str | None = None) -> str:
    """Obtiene la URL del SII para el servicio y ambiente indicados.

    En contexto multi-tenant, ``ambiente`` debe pasarse explícitamente
    (resuelto desde ``Empresa.ambiente_sii`` / ``tenant.ambiente``). El
    fallback a ``settings.AMBIENTE`` existe únicamente para scripts
    legacy internos (``set_pruebas/*``, ``certificacion/nuevapostulacion/*``)
    y NO debe usarse desde el core SII ni desde la API.

    Args:
        servicio: clave del servicio ("seed", "token", "upload",
            "estado_envio", "estado_dte", "boleta_seed", "boleta_token",
            "boleta_upload", "boleta_consumo", "boleta_estado").
        ambiente: "certificacion" o "produccion". Obligatorio en el core SII.

    Raises:
        ValueError: si ``ambiente`` o ``servicio`` son inválidos.
    """
    amb = ambiente if ambiente is not None else AMBIENTE
    if amb not in SII_URLS:
        raise ValueError(
            f"Ambiente SII inválido: '{amb}'. "
            "Debe ser 'certificacion' o 'produccion'."
        )
    servicios = SII_URLS[amb]
    if servicio not in servicios:
        raise ValueError(
            f"Servicio SII '{servicio}' no existe en ambiente '{amb}'. "
            f"Disponibles: {sorted(servicios.keys())}."
        )
    return servicios[servicio]
