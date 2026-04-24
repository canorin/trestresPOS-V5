"""CRUD de cajas (terminales POS) — multi-tenant.

Incluye flujo de instalación: al configurar un nuevo punto de venta,
se selecciona una sucursal disponible (sin terminal asignado) para que
su dirección quede como predeterminada del terminal.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from crumbpos.core.roles import puede_gestionar_sucursal
from crumbpos.db.models import Caja, Sucursal
from crumbpos.api.dependencies import get_tenant, TenantContext


router = APIRouter(prefix="/api/cajas", tags=["cajas"])


# ─── SCHEMAS ───

class CajaCreate(BaseModel):
    sucursal_id: str
    nombre: str


class CajaUpdate(BaseModel):
    nombre: Optional[str] = None
    sucursal_id: Optional[str] = None
    activa: Optional[bool] = None


class CajaOut(BaseModel):
    id: str
    sucursal_id: str
    nombre: str
    activa: bool
    sucursal_nombre: Optional[str] = None
    sucursal_direccion: Optional[str] = None
    sucursal_comuna: Optional[str] = None
    sucursal_ciudad: Optional[str] = None

    model_config = {"from_attributes": True}


class SucursalDisponibleOut(BaseModel):
    """Sucursal sin terminal POS asignado — disponible para instalación."""
    id: str
    nombre: str
    codigo: Optional[str]
    direccion: str
    comuna: str
    ciudad: str
    sii_sucursal: str

    model_config = {"from_attributes": True}


class InstalacionPOSIn(BaseModel):
    """Datos para instalar un terminal POS en una sucursal."""
    sucursal_id: str
    nombre_caja: str = "Caja 1"


class InstalacionPOSOut(BaseModel):
    """Resultado de la instalación del POS."""
    caja_id: str
    caja_nombre: str
    sucursal_id: str
    sucursal_nombre: str
    direccion: str
    comuna: str
    ciudad: str


# ─── HELPERS ───

def _query_cajas_empresa(db: Session, empresa_id: str):
    """Base query: cajas filtered by empresa via join with sucursal."""
    return db.query(Caja).join(Sucursal).filter(
        Sucursal.empresa_id == empresa_id,
    )


def _build_caja_out(caja: Caja) -> CajaOut:
    return CajaOut(
        id=caja.id,
        sucursal_id=caja.sucursal_id,
        nombre=caja.nombre,
        activa=caja.activa,
        sucursal_nombre=caja.sucursal.nombre if caja.sucursal else None,
        sucursal_direccion=caja.sucursal.direccion if caja.sucursal else None,
        sucursal_comuna=caja.sucursal.comuna if caja.sucursal else None,
        sucursal_ciudad=caja.sucursal.ciudad if caja.sucursal else None,
    )


def _validate_sucursal(db: Session, empresa_id: str, sucursal_id: str) -> Sucursal:
    """Validate that the sucursal belongs to the empresa."""
    suc = db.query(Sucursal).filter(
        Sucursal.id == sucursal_id,
        Sucursal.empresa_id == empresa_id,
        Sucursal.activa == True,
    ).first()
    if not suc:
        raise HTTPException(400, "Sucursal no encontrada o no pertenece a esta empresa")
    return suc


# ─── ENDPOINTS ───

@router.get("/", response_model=list[CajaOut])
def listar_cajas(
    sucursal_id: Optional[str] = Query(None, description="Filtrar por sucursal"),
    tenant: TenantContext = Depends(get_tenant),
):
    """Lista cajas de la empresa, con filtro opcional por sucursal."""
    try:
        q = _query_cajas_empresa(tenant.db, tenant.empresa_id).filter(
            Caja.activa == True,
        )
        if sucursal_id:
            q = q.filter(Caja.sucursal_id == sucursal_id)

        cajas = q.order_by(Caja.nombre).all()
        return [_build_caja_out(c) for c in cajas]
    finally:
        tenant.close()


@router.get("/{caja_id}", response_model=CajaOut)
def obtener_caja(caja_id: str, tenant: TenantContext = Depends(get_tenant)):
    """Obtiene una caja por ID."""
    try:
        caja = _query_cajas_empresa(tenant.db, tenant.empresa_id).filter(
            Caja.id == caja_id,
        ).first()
        if not caja:
            raise HTTPException(404, "Caja no encontrada")
        return _build_caja_out(caja)
    finally:
        tenant.close()


@router.post("/", response_model=CajaOut, status_code=201)
def crear_caja(body: CajaCreate, tenant: TenantContext = Depends(get_tenant)):
    """Crea una caja (valida que la sucursal pertenezca a la empresa)."""
    try:
        if not puede_gestionar_sucursal(tenant.user.rol):
            raise HTTPException(403, "No tiene permisos para crear cajas")

        _validate_sucursal(tenant.db, tenant.empresa_id, body.sucursal_id)

        caja = Caja(
            sucursal_id=body.sucursal_id,
            nombre=body.nombre,
            activa=True,
        )
        tenant.db.add(caja)
        tenant.db.commit()
        tenant.db.refresh(caja)
        return _build_caja_out(caja)
    finally:
        tenant.close()


@router.put("/{caja_id}", response_model=CajaOut)
def actualizar_caja(
    caja_id: str,
    body: CajaUpdate,
    tenant: TenantContext = Depends(get_tenant),
):
    """Actualiza una caja existente."""
    try:
        caja = _query_cajas_empresa(tenant.db, tenant.empresa_id).filter(
            Caja.id == caja_id,
        ).first()
        if not caja:
            raise HTTPException(404, "Caja no encontrada")

        updates = body.model_dump(exclude_unset=True)

        # If changing sucursal, validate it belongs to the empresa
        if "sucursal_id" in updates:
            _validate_sucursal(tenant.db, tenant.empresa_id, updates["sucursal_id"])

        for key, value in updates.items():
            setattr(caja, key, value)

        tenant.db.commit()
        tenant.db.refresh(caja)
        return _build_caja_out(caja)
    finally:
        tenant.close()


@router.delete("/{caja_id}")
def desactivar_caja(caja_id: str, tenant: TenantContext = Depends(get_tenant)):
    """Soft delete: desactiva la caja."""
    try:
        if not puede_gestionar_sucursal(tenant.user.rol):
            raise HTTPException(403, "No tiene permisos para desactivar cajas")

        caja = _query_cajas_empresa(tenant.db, tenant.empresa_id).filter(
            Caja.id == caja_id,
        ).first()
        if not caja:
            raise HTTPException(404, "Caja no encontrada")

        caja.activa = False
        tenant.db.commit()
        return {"ok": True, "detail": f"Caja '{caja.nombre}' desactivada"}
    finally:
        tenant.close()


# ─── INSTALACIÓN POS ───

@router.get("/instalacion/sucursales-disponibles", response_model=list[SucursalDisponibleOut])
def sucursales_disponibles(tenant: TenantContext = Depends(get_tenant)):
    """Lista sucursales sin terminal POS asignado.

    Cuando se instala el software POS en un nuevo punto de venta,
    este endpoint muestra las direcciones disponibles para seleccionar.
    Solo aparecen sucursales activas que NO tienen ninguna caja activa asignada.
    """
    try:
        # Sucursales que ya tienen al menos una caja activa
        sucursales_con_caja = (
            tenant.db.query(Caja.sucursal_id)
            .filter(Caja.activa == True)
            .distinct()
            .subquery()
        )

        # Sucursales activas de la empresa SIN caja asignada
        disponibles = (
            tenant.db.query(Sucursal)
            .filter(
                Sucursal.empresa_id == tenant.empresa_id,
                Sucursal.activa == True,
                ~Sucursal.id.in_(sucursales_con_caja),
            )
            .order_by(Sucursal.nombre)
            .all()
        )
        return disponibles
    finally:
        tenant.close()


@router.post("/instalacion", response_model=InstalacionPOSOut, status_code=201)
def instalar_pos(body: InstalacionPOSIn, tenant: TenantContext = Depends(get_tenant)):
    """Instala un terminal POS en una sucursal disponible.

    Asigna la dirección de la sucursal como predeterminada del terminal.
    La sucursal no debe tener otro terminal activo asignado.
    """
    try:
        if not puede_gestionar_sucursal(tenant.user.rol):
            raise HTTPException(403, "No tiene permisos para instalar terminales")

        # Validar que la sucursal pertenece a la empresa
        sucursal = tenant.db.query(Sucursal).filter(
            Sucursal.id == body.sucursal_id,
            Sucursal.empresa_id == tenant.empresa_id,
            Sucursal.activa == True,
        ).first()
        if not sucursal:
            raise HTTPException(404, "Sucursal no encontrada o no pertenece a esta empresa")

        # Verificar que no tenga ya un terminal activo
        caja_existente = tenant.db.query(Caja).filter(
            Caja.sucursal_id == body.sucursal_id,
            Caja.activa == True,
        ).first()
        if caja_existente:
            raise HTTPException(
                409,
                f"La sucursal '{sucursal.nombre}' ya tiene un terminal asignado: '{caja_existente.nombre}'. "
                "Desactive el terminal existente primero o use otro endpoint para agregar cajas adicionales.",
            )

        # Crear la caja (terminal POS) vinculada a la sucursal
        caja = Caja(
            sucursal_id=sucursal.id,
            nombre=body.nombre_caja,
            activa=True,
        )
        tenant.db.add(caja)
        tenant.db.commit()
        tenant.db.refresh(caja)

        return InstalacionPOSOut(
            caja_id=caja.id,
            caja_nombre=caja.nombre,
            sucursal_id=sucursal.id,
            sucursal_nombre=sucursal.nombre,
            direccion=sucursal.direccion,
            comuna=sucursal.comuna,
            ciudad=sucursal.ciudad,
        )
    finally:
        tenant.close()
