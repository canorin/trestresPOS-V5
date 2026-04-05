"""Endpoints de gestión de clientes — multi-tenant."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from crumbpos.db.models import Cliente
from crumbpos.api.dependencies import get_tenant, TenantContext

router = APIRouter(prefix="/api/clientes", tags=["clientes"])


# ── Schemas ──

class ClienteIn(BaseModel):
    rut: str
    razon_social: str
    nombre_fantasia: str | None = None
    giro: str
    direccion: str
    comuna: str
    ciudad: str | None = None
    contacto_nombre: str | None = None
    contacto_email: str | None = None
    contacto_telefono: str | None = None
    condicion_pago: int | None = None
    notas: str | None = None


class ClienteUpdate(BaseModel):
    razon_social: str | None = None
    nombre_fantasia: str | None = None
    giro: str | None = None
    direccion: str | None = None
    comuna: str | None = None
    ciudad: str | None = None
    contacto_nombre: str | None = None
    contacto_email: str | None = None
    contacto_telefono: str | None = None
    condicion_pago: int | None = None
    notas: str | None = None
    activo: bool | None = None


class ClienteOut(BaseModel):
    id: str
    empresa_id: str
    rut: str
    razon_social: str
    nombre_fantasia: str | None
    giro: str
    direccion: str
    comuna: str
    ciudad: str | None
    contacto_nombre: str | None
    contacto_email: str | None
    contacto_telefono: str | None
    condicion_pago: int | None
    notas: str | None
    activo: bool

    model_config = {"from_attributes": True}


# ── Endpoints ──

@router.get("/", response_model=list[ClienteOut])
def listar_clientes(
    q: str | None = Query(None, description="Buscar por RUT o razón social"),
    activo: bool = Query(True, description="Filtrar solo activos"),
    tenant: TenantContext = Depends(get_tenant),
):
    """Lista clientes de la empresa. Soporta búsqueda por RUT o razón social."""
    try:
        db = tenant.db
        query = db.query(Cliente).filter(Cliente.empresa_id == tenant.empresa_id)

        if activo:
            query = query.filter(Cliente.activo == True)

        if q:
            like = f"%{q}%"
            query = query.filter(
                (Cliente.rut.ilike(like)) |
                (Cliente.razon_social.ilike(like)) |
                (Cliente.nombre_fantasia.ilike(like))
            )

        return query.order_by(Cliente.razon_social).limit(50).all()
    finally:
        tenant.close()


@router.get("/{cliente_id}", response_model=ClienteOut)
def obtener_cliente(cliente_id: str, tenant: TenantContext = Depends(get_tenant)):
    """Obtiene un cliente por ID."""
    try:
        cliente = tenant.db.query(Cliente).filter(
            Cliente.id == cliente_id,
            Cliente.empresa_id == tenant.empresa_id,
        ).first()
        if not cliente:
            raise HTTPException(404, "Cliente no encontrado")
        return cliente
    finally:
        tenant.close()


@router.post("/", response_model=ClienteOut)
def crear_cliente(body: ClienteIn, tenant: TenantContext = Depends(get_tenant)):
    """Crea un nuevo cliente para la empresa."""
    try:
        db = tenant.db

        existente = db.query(Cliente).filter(
            Cliente.empresa_id == tenant.empresa_id,
            Cliente.rut == body.rut,
        ).first()
        if existente:
            raise HTTPException(400, f"Ya existe un cliente con RUT {body.rut}")

        cliente = Cliente(
            empresa_id=tenant.empresa_id,
            rut=body.rut,
            razon_social=body.razon_social,
            nombre_fantasia=body.nombre_fantasia,
            giro=body.giro,
            direccion=body.direccion,
            comuna=body.comuna,
            ciudad=body.ciudad,
            contacto_nombre=body.contacto_nombre,
            contacto_email=body.contacto_email,
            contacto_telefono=body.contacto_telefono,
            condicion_pago=body.condicion_pago,
            notas=body.notas,
        )
        db.add(cliente)
        db.commit()
        db.refresh(cliente)
        return cliente
    finally:
        tenant.close()


@router.put("/{cliente_id}", response_model=ClienteOut)
def actualizar_cliente(
    cliente_id: str,
    body: ClienteUpdate,
    tenant: TenantContext = Depends(get_tenant),
):
    """Actualiza datos de un cliente."""
    try:
        db = tenant.db
        cliente = db.query(Cliente).filter(
            Cliente.id == cliente_id,
            Cliente.empresa_id == tenant.empresa_id,
        ).first()
        if not cliente:
            raise HTTPException(404, "Cliente no encontrado")

        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(cliente, field, value)

        db.commit()
        db.refresh(cliente)
        return cliente
    finally:
        tenant.close()


@router.get("/buscar/rut/{rut}")
def buscar_por_rut(rut: str, tenant: TenantContext = Depends(get_tenant)):
    """Busca un cliente por RUT exacto. Retorna null si no existe."""
    try:
        cliente = tenant.db.query(Cliente).filter(
            Cliente.empresa_id == tenant.empresa_id,
            Cliente.rut == rut,
        ).first()
        if not cliente:
            return None
        return ClienteOut.model_validate(cliente)
    finally:
        tenant.close()
