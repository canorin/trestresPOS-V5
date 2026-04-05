"""Endpoints de envio de DTE al receptor via email.

El SII exige que el emisor entregue el DTE al receptor electronico.
Estos endpoints permiten enviar DTEs individuales o en lote,
y consultar cuales estan pendientes de envio.
"""
import base64
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_

from crumbpos.api.dependencies import get_tenant, TenantContext
from crumbpos.core.envio_receptor.email_dte import (
    enviar_dte_email,
    get_email_config_from_env,
)
from crumbpos.db.models import DteEmitido, Cliente, Empresa

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/envio-receptor", tags=["envio-receptor"])


# ── Schemas ──

class EnviarDteIn(BaseModel):
    email_destino: str | None = None  # Override, otherwise uses Cliente.contacto_email


class EnvioResultOut(BaseModel):
    dte_id: str
    tipo_dte: int
    folio: int
    ok: bool
    mensaje: str
    error: str | None = None


class DtePendienteOut(BaseModel):
    id: str
    tipo_dte: int
    folio: int
    fecha_emision: str
    receptor_rut: str | None
    receptor_razon: str | None
    monto_total: int
    estado_sii: str
    estado_receptor: str | None

    model_config = {"from_attributes": True}


# ── Helpers ──

def _resolve_email(
    db,
    empresa_id: str,
    receptor_rut: str | None,
    override_email: str | None,
) -> str | None:
    """Resolve receptor email: override > Cliente.contacto_email."""
    if override_email:
        return override_email
    if not receptor_rut:
        return None
    cliente = db.query(Cliente).filter(
        Cliente.empresa_id == empresa_id,
        Cliente.rut == receptor_rut,
    ).first()
    if cliente and cliente.contacto_email:
        return cliente.contacto_email
    return None


def _send_single_dte(
    tenant: TenantContext,
    dte: DteEmitido,
    override_email: str | None = None,
) -> dict:
    """Send a single DTE and update its estado_receptor."""
    db = tenant.db

    # Resolve email
    email = _resolve_email(db, tenant.empresa_id, dte.receptor_rut, override_email)
    if not email:
        return {
            "dte_id": dte.id,
            "tipo_dte": dte.tipo_dte,
            "folio": dte.folio,
            "ok": False,
            "mensaje": "No se encontro email del receptor. Proporcione email_destino o registre contacto_email en el cliente.",
            "error": "sin_email_receptor",
        }

    # Decode XML from base64
    if not dte.xml_firmado:
        return {
            "dte_id": dte.id,
            "tipo_dte": dte.tipo_dte,
            "folio": dte.folio,
            "ok": False,
            "mensaje": "DTE no tiene XML firmado almacenado",
            "error": "sin_xml_firmado",
        }

    try:
        xml_bytes = base64.b64decode(dte.xml_firmado)
    except Exception:
        return {
            "dte_id": dte.id,
            "tipo_dte": dte.tipo_dte,
            "folio": dte.folio,
            "ok": False,
            "mensaje": "Error decodificando XML firmado (base64 invalido)",
            "error": "xml_decode_error",
        }

    # Get emisor info
    empresa = db.query(Empresa).filter(Empresa.id == tenant.empresa_id).first()
    emisor_razon = empresa.razon_social if empresa else tenant.empresa_rut

    # Optional PDF (read from filesystem if path exists)
    pdf_bytes = None
    if dte.pdf_path:
        try:
            with open(dte.pdf_path, "rb") as f:
                pdf_bytes = f.read()
        except OSError:
            logger.warning("PDF no encontrado en %s, enviando solo XML", dte.pdf_path)

    # SMTP config from env
    config = get_email_config_from_env()
    if not config.smtp_user or not config.smtp_password:
        return {
            "dte_id": dte.id,
            "tipo_dte": dte.tipo_dte,
            "folio": dte.folio,
            "ok": False,
            "mensaje": "Configuracion SMTP incompleta. Defina SMTP_USER y SMTP_PASSWORD.",
            "error": "smtp_no_configurado",
        }

    # Send
    result = enviar_dte_email(
        config=config,
        destinatario_email=email,
        emisor_razon=emisor_razon,
        receptor_razon=dte.receptor_razon or "Receptor",
        tipo_dte=dte.tipo_dte,
        folio=dte.folio,
        monto_total=dte.monto_total or 0,
        xml_bytes=xml_bytes,
        pdf_bytes=pdf_bytes,
    )

    # Update estado_receptor
    if result["ok"]:
        dte.estado_receptor = "enviado"
        db.commit()

    return {
        "dte_id": dte.id,
        "tipo_dte": dte.tipo_dte,
        "folio": dte.folio,
        **result,
    }


# ── Endpoints ──

@router.post("/enviar/{dte_id}", response_model=EnvioResultOut)
def enviar_dte(
    dte_id: str,
    body: EnviarDteIn | None = None,
    tenant: TenantContext = Depends(get_tenant),
):
    """Enviar DTE especifico al receptor por email.

    Si no se proporciona email_destino, se busca en Cliente.contacto_email
    usando el receptor_rut del DTE.
    """
    db = tenant.db
    dte = db.query(DteEmitido).filter(
        DteEmitido.id == dte_id,
        DteEmitido.empresa_id == tenant.empresa_id,
    ).first()
    if not dte:
        raise HTTPException(404, f"DTE {dte_id} no encontrado")

    override_email = body.email_destino if body else None
    return _send_single_dte(tenant, dte, override_email)


@router.post("/enviar-pendientes", response_model=list[EnvioResultOut])
def enviar_pendientes(
    tenant: TenantContext = Depends(get_tenant),
):
    """Enviar todos los DTEs pendientes de entrega al receptor.

    Se consideran pendientes aquellos con estado_receptor NULL o 'pendiente'.
    Solo se envian DTEs que tengan XML firmado y receptor con email registrado.
    """
    db = tenant.db
    pendientes = db.query(DteEmitido).filter(
        DteEmitido.empresa_id == tenant.empresa_id,
        or_(
            DteEmitido.estado_receptor.is_(None),
            DteEmitido.estado_receptor == "pendiente",
        ),
    ).all()

    resultados = []
    for dte in pendientes:
        result = _send_single_dte(tenant, dte)
        resultados.append(result)

    return resultados


@router.get("/pendientes", response_model=list[DtePendienteOut])
def listar_pendientes(
    limit: int = Query(50, ge=1, le=500),
    tenant: TenantContext = Depends(get_tenant),
):
    """Listar DTEs no enviados al receptor.

    Devuelve DTEs con estado_receptor NULL o 'pendiente'.
    """
    db = tenant.db
    pendientes = db.query(DteEmitido).filter(
        DteEmitido.empresa_id == tenant.empresa_id,
        or_(
            DteEmitido.estado_receptor.is_(None),
            DteEmitido.estado_receptor == "pendiente",
        ),
    ).order_by(DteEmitido.fecha_emision.desc()).limit(limit).all()

    return [
        DtePendienteOut(
            id=d.id,
            tipo_dte=d.tipo_dte,
            folio=d.folio,
            fecha_emision=str(d.fecha_emision),
            receptor_rut=d.receptor_rut,
            receptor_razon=d.receptor_razon,
            monto_total=d.monto_total or 0,
            estado_sii=d.estado_sii,
            estado_receptor=d.estado_receptor,
        )
        for d in pendientes
    ]
