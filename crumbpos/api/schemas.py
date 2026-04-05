"""Pydantic schemas para validación de request/response."""
from datetime import datetime
from pydantic import BaseModel, EmailStr


# ═══════════ AUTH ═══════════

class LoginRequest(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    usuario: "UsuarioOut"


# ═══════════ EMPRESA ═══════════

class EmpresaOut(BaseModel):
    id: str
    rut: str
    razon_social: str
    nombre_fantasia: str | None
    giro: str
    direccion: str
    comuna: str
    ciudad: str
    activa: bool

    model_config = {"from_attributes": True}


# ═══════════ SUCURSAL ═══════════

class SucursalOut(BaseModel):
    id: str
    empresa_id: str
    nombre: str
    codigo: str | None
    direccion: str
    comuna: str
    ciudad: str
    activa: bool

    model_config = {"from_attributes": True}


# ═══════════ USUARIO ═══════════

class UsuarioOut(BaseModel):
    id: str
    empresa_id: str | None = None
    empresa_rut: str | None = None
    email: str
    nombre: str
    rol: str
    activo: bool

    model_config = {"from_attributes": True}


class UsuarioCreate(BaseModel):
    email: str
    nombre: str
    password: str
    rol: str = "cajero"
    sucursal_ids: list[str] = []


# ═══════════ FAMILIA ═══════════

class FamiliaCreate(BaseModel):
    nombre: str
    codigo: str | None = None
    color: str | None = None
    icono: str | None = None
    parent_id: str | None = None
    orden: int = 0

class FamiliaOut(BaseModel):
    id: str
    nombre: str
    codigo: str | None
    color: str | None
    icono: str | None
    parent_id: str | None
    orden: int
    activa: bool

    model_config = {"from_attributes": True}

class FamiliaSucursalUpdate(BaseModel):
    activa: bool
    orden: int = 0


# ═══════════ ARTÍCULO ═══════════

class ArticuloCreate(BaseModel):
    familia_id: str | None = None
    sku: str | None = None
    codigo_barras: str | None = None
    nombre: str
    nombre_corto: str | None = None
    unidad_medida: str = "UN"
    precio_default: int = 0
    costo_default: int = 0
    es_exento: bool = False

class ArticuloUpdate(BaseModel):
    familia_id: str | None = None
    sku: str | None = None
    codigo_barras: str | None = None
    nombre: str | None = None
    nombre_corto: str | None = None
    unidad_medida: str | None = None
    precio_default: int | None = None
    costo_default: int | None = None
    es_exento: bool | None = None
    activo: bool | None = None

class ArticuloOut(BaseModel):
    id: str
    familia_id: str | None
    sku: str | None
    codigo_barras: str | None
    nombre: str
    nombre_corto: str | None
    unidad_medida: str
    precio_default: int
    costo_default: int
    es_exento: bool
    activo: bool

    model_config = {"from_attributes": True}

class ArticuloSucursalUpdate(BaseModel):
    activo: bool = True
    precio_venta: int | None = None
    costo: int | None = None

class ArticuloSucursalOut(BaseModel):
    articulo: ArticuloOut
    activo: bool
    precio_venta: int | None
    precio_efectivo: int  # el que realmente se usa (sucursal o default)

    model_config = {"from_attributes": True}


# ═══════════ VENTA (POS) ═══════════

class VentaItemIn(BaseModel):
    articulo_id: str
    cantidad: float = 1
    precio_unitario: int
    descuento_pct: float | None = None
    es_exento: bool = False

class PagoIn(BaseModel):
    medio: str  # efectivo, debito, credito, transferencia
    monto: int
    referencia: str | None = None

class VentaCreate(BaseModel):
    sucursal_id: str
    sesion_caja_id: str | None = None
    tipo_dte: int = 39  # boleta por defecto
    receptor_rut: str | None = None
    receptor_razon: str | None = None
    receptor_giro: str | None = None
    receptor_dir: str | None = None
    receptor_comuna: str | None = None
    items: list[VentaItemIn]
    pagos: list[PagoIn]

class VentaOut(BaseModel):
    id: str
    sucursal_id: str
    tipo_dte: int
    folio: int | None
    fecha: datetime
    monto_neto: int | None
    monto_exento: int | None
    iva: int | None
    monto_total: int
    estado: str
    sync_status: str
    receptor_rut: str | None
    receptor_razon: str | None

    model_config = {"from_attributes": True}


# ═══════════ DASHBOARD ═══════════

class DashboardSucursal(BaseModel):
    sucursal_id: str
    sucursal_nombre: str
    ventas_hoy: int
    transacciones_hoy: int
    ticket_promedio: int


# Resolver forward reference
TokenResponse.model_rebuild()
