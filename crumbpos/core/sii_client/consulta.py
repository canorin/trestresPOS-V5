"""Consulta de estado de DTEs en el SII."""
import requests
from crumbpos.config.settings import get_sii_url


def consultar_estado_dte(
    token: str,
    rut_emisor: str,
    tipo_dte: int,
    folio: int,
    fecha_emision: str,
    monto_total: int,
    rut_receptor: str,
) -> dict:
    """Consulta el estado de un DTE específico en el SII."""
    rut_e_num, rut_e_dv = rut_emisor.split("-")
    rut_r_num, rut_r_dv = rut_receptor.split("-")
    url = get_sii_url("estado_dte")

    params = {
        "TOKEN": token,
        "RUT_EMISOR": rut_e_num,
        "DV_EMISOR": rut_e_dv,
        "TIPO_DTE": tipo_dte,
        "FOLIO_DTE": folio,
        "FECHA_EMISION_DTE": fecha_emision,
        "MONTO_DTE": monto_total,
        "RUT_RECEPTOR": rut_r_num,
        "DV_RECEPTOR": rut_r_dv,
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()

    return {"raw": response.text}
