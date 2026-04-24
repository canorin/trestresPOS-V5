"""CRUD de sucursales — multi-tenant."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from crumbpos.core.roles import puede_gestionar_empresa
from crumbpos.db.models import Sucursal
from crumbpos.api.dependencies import get_tenant, TenantContext


router = APIRouter(prefix="/api/sucursales", tags=["sucursales"])


# ─── SCHEMAS ───

class SucursalCreate(BaseModel):
    nombre: str
    codigo: Optional[str] = None
    direccion: str
    comuna: str
    ciudad: str
    sii_sucursal: str = "SANTIAGO ORIENTE"


class SucursalUpdate(BaseModel):
    nombre: Optional[str] = None
    codigo: Optional[str] = None
    direccion: Optional[str] = None
    comuna: Optional[str] = None
    ciudad: Optional[str] = None
    sii_sucursal: Optional[str] = None
    activa: Optional[bool] = None


class SucursalOut(BaseModel):
    id: str
    empresa_id: str
    nombre: str
    codigo: Optional[str]
    direccion: str
    comuna: str
    ciudad: str
    sii_sucursal: str
    activa: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── ENDPOINTS ───

@router.get("/", response_model=list[SucursalOut])
def listar_sucursales(tenant: TenantContext = Depends(get_tenant)):
    """Lista todas las sucursales de la empresa."""
    try:
        return tenant.db.query(Sucursal).filter(
            Sucursal.empresa_id == tenant.empresa_id,
            Sucursal.activa == True,
        ).order_by(Sucursal.nombre).all()
    finally:
        tenant.close()


@router.get("/{sucursal_id}", response_model=SucursalOut)
def obtener_sucursal(sucursal_id: str, tenant: TenantContext = Depends(get_tenant)):
    """Obtiene una sucursal por ID."""
    try:
        suc = tenant.db.query(Sucursal).filter(
            Sucursal.id == sucursal_id,
            Sucursal.empresa_id == tenant.empresa_id,
        ).first()
        if not suc:
            raise HTTPException(404, "Sucursal no encontrada")
        return suc
    finally:
        tenant.close()


@router.post("/", response_model=SucursalOut, status_code=201)
def crear_sucursal(body: SucursalCreate, tenant: TenantContext = Depends(get_tenant)):
    """Crea una sucursal (master_client / administrador / super_admin)."""
    try:
        if not puede_gestionar_empresa(tenant.user.rol):
            raise HTTPException(403, "No tiene permisos para crear sucursales")

        suc = Sucursal(
            empresa_id=tenant.empresa_id,
            **body.model_dump(),
        )
        tenant.db.add(suc)
        tenant.db.commit()
        tenant.db.refresh(suc)
        return suc
    finally:
        tenant.close()


@router.put("/{sucursal_id}", response_model=SucursalOut)
def actualizar_sucursal(
    sucursal_id: str,
    body: SucursalUpdate,
    tenant: TenantContext = Depends(get_tenant),
):
    """Actualiza una sucursal existente."""
    try:
        suc = tenant.db.query(Sucursal).filter(
            Sucursal.id == sucursal_id,
            Sucursal.empresa_id == tenant.empresa_id,
        ).first()
        if not suc:
            raise HTTPException(404, "Sucursal no encontrada")

        updates = body.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(suc, key, value)

        tenant.db.commit()
        tenant.db.refresh(suc)
        return suc
    finally:
        tenant.close()


@router.delete("/{sucursal_id}")
def desactivar_sucursal(sucursal_id: str, tenant: TenantContext = Depends(get_tenant)):
    """Soft delete: desactiva la sucursal."""
    try:
        if not puede_gestionar_empresa(tenant.user.rol):
            raise HTTPException(403, "No tiene permisos para desactivar sucursales")

        suc = tenant.db.query(Sucursal).filter(
            Sucursal.id == sucursal_id,
            Sucursal.empresa_id == tenant.empresa_id,
        ).first()
        if not suc:
            raise HTTPException(404, "Sucursal no encontrada")

        suc.activa = False
        tenant.db.commit()
        return {"ok": True, "detail": f"Sucursal '{suc.nombre}' desactivada"}
    finally:
        tenant.close()
