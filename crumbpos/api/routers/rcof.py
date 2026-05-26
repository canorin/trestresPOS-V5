"""Endpoints para RCOF (Reporte de Consumo de Folios) — multi-tenant."""
import json
import logging
import os
import base64
import tempfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from crumbpos.api.services.rcof_service import ServicioRCOF
from crumbpos.config import settings
from crumbpos.db.models import Empresa, RcofDiario
from crumbpos.api.dependencies import get_tenant, TenantContext, check_dte_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rcof", tags=["rcof"])


# ── Helper: create ServicioRCOF ──

def _get_servicio_rcof(tenant: TenantContext) -> tuple[ServicioRCOF, Empresa]:
    """Creates a ServicioRCOF for the tenant's empresa."""
    db = tenant.db
    empresa = db.query(Empresa).filter(Empresa.rut == tenant.empresa_rut).first()
    if not empresa:
        raise HTTPException(404, f"Empresa no encontrada: {tenant.empresa_rut}")
    if not empresa.fecha_resolucion:
        raise HTTPException(422, f"Empresa {tenant.empresa_rut} sin fecha_resolucion")
    if not empresa.cert_rut_firmante:
        raise HTTPException(422, f"Empresa {tenant.empresa_rut} sin cert_rut_firmante")

    base_dir = Path(settings.BASE_DIR)
    cert_path = None
    cert_password = None

    if empresa.cert_data:
        pfx_bytes = base64.b64decode(empresa.cert_data)
        fd, tmp_pfx = tempfile.mkstemp(suffix=".pfx")
        os.write(fd, pfx_bytes)
        os.close(fd)
        cert_path = tmp_pfx
        cert_password = empresa.cert_password
    elif empresa.cert_path and Path(empresa.cert_path).exists():
        cert_path = empresa.cert_path
        cert_password = empresa.cert_password

    if not cert_path:
        for d in [base_dir / "certificados", base_dir / "cert"]:
            if d.is_dir():
                pfx_files = list(d.glob("*.pfx")) + list(d.glob("*.p12"))
                if pfx_files:
                    cert_path = str(pfx_files[0])
                    break

    if not cert_path:
        raise HTTPException(500, "Certificado .pfx no encontrado")

    servicio = ServicioRCOF(
        empresa=empresa,
        cert_path=cert_path,
        cert_password=cert_password,
    )
    return servicio, empresa


# ── Schemas ──

class GenerarRcofIn(BaseModel):
    fecha: str  # "YYYY-MM-DD"
    enviar: bool = True


class GenerarRcofOut(BaseModel):
    ok: bool
    rcof_id: str | None = None
    track_id: str | None = None
    estado_sii: str | None = None
    total_boletas: int | None = None
    resumen: dict | None = None
    mensaje: str | None = None
    error: str | None = None


class RcofOut(BaseModel):
    id: str
    fecha: str
    track_id: str | None = None
    estado_sii: str
    resumen: dict | None = None
    created_at: str


# ── Endpoints ──

@router.post("/generar", response_model=GenerarRcofOut)
def generar_rcof(
    body: GenerarRcofIn,
    tenant: TenantContext = Depends(get_tenant),
    _rl: None = Depends(check_dte_rate_limit),
):
    """Genera, firma y envia el RCOF para una fecha."""
    try:
        # Validar formato fecha
        try:
            fecha = date.fromisoformat(body.fecha)
        except ValueError:
            raise HTTPException(400, "Formato de fecha invalido. Use YYYY-MM-DD")

        servicio, empresa = _get_servicio_rcof(tenant)

        resultado = servicio.generar_rcof_diario(
            db=tenant.db,
            fecha=fecha,
            enviar=body.enviar,
        )

        return GenerarRcofOut(
            ok=resultado.get("ok", False),
            rcof_id=resultado.get("rcof_id"),
            track_id=resultado.get("track_id"),
            estado_sii=resultado.get("estado_sii"),
            total_boletas=resultado.get("total_boletas"),
            resumen=resultado.get("resumen"),
            mensaje=resultado.get("mensaje"),
            error=resultado.get("error"),
        )
    finally:
        tenant.close()


@router.get("/{fecha}", response_model=RcofOut)
def obtener_rcof(
    fecha: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Obtiene el RCOF generado para una fecha."""
    try:
        try:
            fecha_date = date.fromisoformat(fecha)
        except ValueError:
            raise HTTPException(400, "Formato de fecha invalido. Use YYYY-MM-DD")

        rcof = (
            tenant.db.query(RcofDiario)
            .filter(
                RcofDiario.empresa_id == tenant.empresa_id,
                RcofDiario.fecha == fecha_date,
            )
            .first()
        )

        if not rcof:
            raise HTTPException(404, f"No hay RCOF para {fecha}")

        return RcofOut(
            id=rcof.id,
            fecha=rcof.fecha.isoformat(),
            track_id=rcof.track_id,
            estado_sii=rcof.estado_sii,
            resumen=rcof.resumen,
            created_at=rcof.created_at.isoformat() if rcof.created_at else "",
        )
    finally:
        tenant.close()


@router.get("", response_model=list[RcofOut])
def listar_rcofs(tenant: TenantContext = Depends(get_tenant)):
    """Lista todos los RCOFs generados."""
    try:
        rcofs = (
            tenant.db.query(RcofDiario)
            .filter(RcofDiario.empresa_id == tenant.empresa_id)
            .order_by(RcofDiario.fecha.desc())
            .all()
        )

        return [
            RcofOut(
                id=r.id,
                fecha=r.fecha.isoformat(),
                track_id=r.track_id,
                estado_sii=r.estado_sii,
                resumen=r.resumen,
                created_at=r.created_at.isoformat() if r.created_at else "",
            )
            for r in rcofs
        ]
    finally:
        tenant.close()
