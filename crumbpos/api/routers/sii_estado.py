"""Endpoints de consulta de estado SII — polling de DTEs, Boletas y Libros.

Multi-tenant: usa TenantContext para acceder a la BD correcta
segun la empresa y ambiente del usuario autenticado.

Boletas (T39/T41) usan la REST API del SII con token diferente
a los DTEs tradicionales (SOAP). Este router obtiene ambos tokens
cuando hay boletas pendientes.
"""
import logging
import os
import base64
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from crumbpos.api.dependencies import get_tenant, TenantContext
from crumbpos.db.models import DteEmitido, LibroGenerado, Empresa
from crumbpos.core.sii_client.polling import (
    poll_all, poll_dtes, ESTADO_BOLETA_MAP, _parse_estado_boleta,
)
from crumbpos.core.sii_client.autenticacion import obtener_token, obtener_token_boleta
from crumbpos.core.sii_client.envio import consultar_estado_boleta
from crumbpos.core.sii_client.consulta import consultar_estado_dte
from crumbpos.config import settings

# Firma library for boleta token authentication
from facturacion_electronica.firma import Firma

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sii", tags=["sii-estado"])


# ── Helpers ──

def _get_empresa(tenant: TenantContext) -> Empresa:
    """Obtiene la empresa del tenant actual."""
    empresa = (
        tenant.db.query(Empresa)
        .filter(Empresa.rut == tenant.empresa_rut)
        .first()
    )
    if not empresa:
        raise HTTPException(404, f"Empresa no encontrada: {tenant.empresa_rut}")
    return empresa


def _load_cert_and_get_token(empresa: Empresa) -> str:
    """Carga el certificado de la empresa y obtiene un token SII.

    El ambiente (cert/prod) se resuelve desde ``empresa.ambiente_sii``,
    no desde el settings global.

    Sigue el mismo patron que facturacion.py para cargar el certificado:
    1. cert_data (base64 en DB) — prioridad
    2. cert_path (archivo local)
    3. Fallback: buscar .pfx en directorios conocidos
    """
    from cryptography.hazmat.primitives.serialization import pkcs12, Encoding, PrivateFormat, NoEncryption

    cert_path = None
    cert_password = empresa.cert_password
    tmp_file = None

    # Certificado desde DB (prioridad)
    if empresa.cert_data:
        pfx_bytes = base64.b64decode(empresa.cert_data)
        fd, tmp_pfx = tempfile.mkstemp(suffix=".pfx")
        os.write(fd, pfx_bytes)
        os.close(fd)
        cert_path = tmp_pfx
        tmp_file = tmp_pfx
    elif empresa.cert_path and Path(empresa.cert_path).exists():
        cert_path = empresa.cert_path

    # Fallback: buscar .pfx local
    if not cert_path:
        base_dir = Path(settings.BASE_DIR)
        for d in [base_dir / "certificados", base_dir / "cert"]:
            if d.is_dir():
                pfx_files = list(d.glob("*.pfx")) + list(d.glob("*.p12"))
                if pfx_files:
                    cert_path = str(pfx_files[0])
                    break

    if not cert_path:
        raise HTTPException(500, "Certificado .pfx no encontrado para esta empresa")

    try:
        # Leer el PFX y extraer key + cert para autenticacion SII
        with open(cert_path, "rb") as f:
            pfx_data = f.read()

        password = cert_password.encode() if cert_password else None
        private_key, certificate, _ = pkcs12.load_key_and_certificates(pfx_data, password)

        if private_key is None or certificate is None:
            raise HTTPException(500, "Certificado no contiene llave privada o certificado")

        private_key_pem = private_key.private_bytes(
            Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption(),
        )
        cert_der = certificate.public_bytes(Encoding.DER)

        token = obtener_token(private_key_pem, cert_der, empresa.ambiente_sii)
        return token

    finally:
        # Limpiar archivo temporal
        if tmp_file and os.path.exists(tmp_file):
            os.unlink(tmp_file)


def _get_boleta_token(empresa: Empresa) -> str:
    """Obtiene un token REST API para boletas del SII.

    Usa la libreria facturacion_electronica.firma.Firma para firmar
    la semilla de boleta, mismo patron que rcof_service.py y emision_dte.py.

    Multi-tenant: usa cert_path/cert_data y cert_password de la empresa.
    """
    cert_path = None
    cert_password = empresa.cert_password
    tmp_file = None

    # Certificado desde DB (prioridad)
    if empresa.cert_data:
        pfx_bytes = base64.b64decode(empresa.cert_data)
        fd, tmp_pfx = tempfile.mkstemp(suffix=".pfx")
        os.write(fd, pfx_bytes)
        os.close(fd)
        cert_path = tmp_pfx
        tmp_file = tmp_pfx
    elif empresa.cert_path and Path(empresa.cert_path).exists():
        cert_path = empresa.cert_path

    # Fallback: buscar .pfx local
    if not cert_path:
        base_dir = Path(settings.BASE_DIR)
        for d in [base_dir / "certificados", base_dir / "cert"]:
            if d.is_dir():
                pfx_files = list(d.glob("*.pfx")) + list(d.glob("*.p12"))
                if pfx_files:
                    cert_path = str(pfx_files[0])
                    break

    if not cert_path:
        raise HTTPException(500, "Certificado .pfx no encontrado para esta empresa")

    try:
        pfx_data = open(cert_path, "rb").read()
        rut_firmante = empresa.cert_rut_firmante or empresa.rut
        firma = Firma({
            "string_firma": base64.b64encode(pfx_data).decode(),
            "string_password": cert_password or "",
            "init_signature": True,
            "rut_firmante": rut_firmante,
        })
        if not firma.firma_electronica:
            raise HTTPException(500, f"Error cargando certificado: {firma.errores}")
        firma.verify = False

        token = obtener_token_boleta(firma, empresa.ambiente_sii)
        return token

    finally:
        if tmp_file and os.path.exists(tmp_file):
            os.unlink(tmp_file)


# ── Endpoints ──

@router.post("/polling")
def trigger_polling(tenant: TenantContext = Depends(get_tenant)):
    """Ejecuta polling de estado SII para todos los DTEs, Boletas y Libros pendientes.

    Consulta el SII por cada DTE/Boleta/Libro con estado 'pendiente' o 'enviado'
    que tenga track_id, y actualiza el estado en la base de datos.

    Para boletas (T39/T41) usa la REST API con token de boleta.
    Para DTEs y Libros usa SOAP con token tradicional.
    """
    try:
        empresa = _get_empresa(tenant)
        token = _load_cert_and_get_token(empresa)

        # Intentar obtener token boleta si hay boletas pendientes
        token_boleta = None
        has_boletas = (
            tenant.db.query(DteEmitido)
            .filter(
                DteEmitido.empresa_id == empresa.id,
                DteEmitido.estado_sii.in_(["pendiente", "enviado"]),
                DteEmitido.tipo_dte.in_([39, 41]),
                DteEmitido.track_id.isnot(None),
                DteEmitido.track_id != "",
            )
            .first()
        )
        if has_boletas:
            try:
                token_boleta = _get_boleta_token(empresa)
            except Exception as e:
                logger.warning("No se pudo obtener token boleta: %s", e)

        result = poll_all(
            db=tenant.db,
            empresa=empresa,
            token=token,
            token_boleta=token_boleta,
        )

        tenant.db.commit()
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error en polling SII: %s", e, exc_info=True)
        raise HTTPException(500, f"Error consultando SII: {str(e)}")
    finally:
        tenant.close()


@router.get("/dtes/pendientes")
def listar_dtes_pendientes(tenant: TenantContext = Depends(get_tenant)):
    """Lista DTEs con estado_sii distinto de 'aceptado'.

    Retorna DTEs pendientes, enviados, rechazados o con reparos.
    """
    try:
        empresa = _get_empresa(tenant)

        dtes = (
            tenant.db.query(DteEmitido)
            .filter(
                DteEmitido.empresa_id == empresa.id,
                DteEmitido.estado_sii != "aceptado",
            )
            .order_by(DteEmitido.fecha_emision.desc())
            .all()
        )

        return {
            "total": len(dtes),
            "dtes": [
                {
                    "id": dte.id,
                    "tipo_dte": dte.tipo_dte,
                    "folio": dte.folio,
                    "fecha_emision": str(dte.fecha_emision),
                    "receptor_rut": dte.receptor_rut,
                    "receptor_razon": dte.receptor_razon,
                    "monto_total": dte.monto_total,
                    "track_id": dte.track_id,
                    "estado_sii": dte.estado_sii,
                    "glosa_sii": dte.glosa_sii,
                    "fecha_consulta_sii": (
                        dte.fecha_consulta_sii.isoformat() if dte.fecha_consulta_sii else None
                    ),
                }
                for dte in dtes
            ],
        }
    finally:
        tenant.close()


@router.get("/dtes/{tipo_dte}/{folio}/estado")
def consultar_estado_dte_especifico(
    tipo_dte: int,
    folio: int,
    tenant: TenantContext = Depends(get_tenant),
):
    """Consulta el estado de un DTE especifico en el SII.

    Busca el DTE en la base de datos, consulta el SII si tiene track_id,
    y actualiza el registro.
    """
    try:
        empresa = _get_empresa(tenant)

        dte = (
            tenant.db.query(DteEmitido)
            .filter(
                DteEmitido.empresa_id == empresa.id,
                DteEmitido.tipo_dte == tipo_dte,
                DteEmitido.folio == folio,
            )
            .first()
        )

        if not dte:
            raise HTTPException(404, f"DTE T{tipo_dte} F{folio} no encontrado")

        result = {
            "tipo_dte": dte.tipo_dte,
            "folio": dte.folio,
            "fecha_emision": str(dte.fecha_emision),
            "receptor_rut": dte.receptor_rut,
            "monto_total": dte.monto_total,
            "track_id": dte.track_id,
            "estado_sii": dte.estado_sii,
            "glosa_sii": dte.glosa_sii,
            "fecha_consulta_sii": (
                dte.fecha_consulta_sii.isoformat() if dte.fecha_consulta_sii else None
            ),
        }

        # Si tiene track_id, consultar SII en tiempo real
        if dte.track_id:
            try:
                from crumbpos.core.sii_client.polling import _parse_estado_envio, ESTADO_DTE_MAP
                from crumbpos.core.sii_client.envio import consultar_estado_envio
                from datetime import datetime, timezone

                es_boleta = dte.tipo_dte in (39, 41)

                if es_boleta:
                    # Boletas usan REST API con token de boleta
                    token_bol = _get_boleta_token(empresa)
                    resp_json = consultar_estado_boleta(
                        track_id=dte.track_id,
                        token=token_bol,
                        rut_emisor=empresa.rut,
                        ambiente=empresa.ambiente_sii,
                    )
                    estado_sii_code, glosa = _parse_estado_boleta(resp_json)
                    estado_map = ESTADO_BOLETA_MAP
                    result["respuesta_sii"] = resp_json
                else:
                    # DTEs tradicionales usan SOAP
                    token = _load_cert_and_get_token(empresa)
                    resp = consultar_estado_envio(
                        track_id=dte.track_id,
                        token=token,
                        rut_emisor=empresa.rut,
                        ambiente=empresa.ambiente_sii,
                    )
                    raw_xml = resp.get("raw", "")
                    estado_sii_code, glosa = _parse_estado_envio(raw_xml)
                    estado_map = ESTADO_DTE_MAP

                if estado_sii_code:
                    nuevo_estado = estado_map.get(estado_sii_code, "pendiente")
                    dte.estado_sii = nuevo_estado
                    dte.glosa_sii = glosa or estado_sii_code
                    dte.fecha_consulta_sii = datetime.now(timezone.utc)
                    tenant.db.commit()

                    result["estado_sii"] = nuevo_estado
                    result["glosa_sii"] = glosa or estado_sii_code
                    result["estado_sii_raw"] = estado_sii_code
                    result["fecha_consulta_sii"] = dte.fecha_consulta_sii.isoformat()
                    result["consulta_realizada"] = True

            except Exception as e:
                logger.warning("Error consultando SII para T%d F%d: %s", tipo_dte, folio, e)
                result["consulta_error"] = str(e)
                result["consulta_realizada"] = False
        else:
            result["consulta_realizada"] = False
            result["nota"] = "DTE sin track_id, no se puede consultar SII"

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error consultando estado DTE: %s", e, exc_info=True)
        raise HTTPException(500, f"Error: {str(e)}")
    finally:
        tenant.close()


@router.get("/boletas/{track_id}/estado")
def consultar_estado_envio_boleta(
    track_id: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Consulta el estado de un envío de boletas en el SII vía REST API.

    Usa el endpoint REST del SII para boletas electrónicas:
    GET /boleta.electronica.envio/{rut}-{dv}-{trackid}

    Retorna la respuesta completa del SII incluyendo:
    - estado: REC, EPR, RCH, RCO, RPR, SOK
    - estadistica: conteo por tipo de documento
    - detalle_rep_rech: detalles de rechazos/reparos

    También actualiza todos los DTEs de ese track_id en la base de datos.
    """
    try:
        empresa = _get_empresa(tenant)
        token_bol = _get_boleta_token(empresa)

        resp_json = consultar_estado_boleta(
            track_id=track_id,
            token=token_bol,
            rut_emisor=empresa.rut,
            ambiente=empresa.ambiente_sii,
        )

        estado_sii_code, glosa = _parse_estado_boleta(resp_json)

        # Actualizar DTEs con ese track_id
        dtes_actualizados = 0
        if estado_sii_code:
            from datetime import datetime, timezone
            nuevo_estado = ESTADO_BOLETA_MAP.get(estado_sii_code, "pendiente")

            dtes = (
                tenant.db.query(DteEmitido)
                .filter(
                    DteEmitido.empresa_id == empresa.id,
                    DteEmitido.track_id == track_id,
                )
                .all()
            )

            for dte in dtes:
                dte.estado_sii = nuevo_estado
                dte.glosa_sii = glosa or estado_sii_code
                dte.fecha_consulta_sii = datetime.now(timezone.utc)
                dtes_actualizados += 1

            if dtes:
                tenant.db.commit()

        return {
            "track_id": track_id,
            "estado_sii": estado_sii_code,
            "estado_interpretado": ESTADO_BOLETA_MAP.get(estado_sii_code, "desconocido") if estado_sii_code else None,
            "glosa": glosa,
            "dtes_actualizados": dtes_actualizados,
            "respuesta_sii": resp_json,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error consultando estado boleta track=%s: %s", track_id, e, exc_info=True)
        raise HTTPException(500, f"Error consultando estado boleta: {str(e)}")
    finally:
        tenant.close()
