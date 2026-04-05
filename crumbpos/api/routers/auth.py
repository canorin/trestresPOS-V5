"""Endpoints de autenticación — usa master.db para auth centralizado."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from crumbpos.db.multi_tenant import (
    get_master_db, get_empresa_db_session, UsuarioAuth, EmpresaRegistro,
)
from crumbpos.api.dependencies import (
    get_current_user, get_tenant, TenantContext,
    SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES,
)

import bcrypt
from jose import jwt
from datetime import datetime

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Schemas ──

class LoginRequest(BaseModel):
    email: str
    password: str
    sucursal_id: str | None = None  # POS envía la sucursal donde opera


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    usuario: "UsuarioOut"


class SucursalInfo(BaseModel):
    id: str
    nombre: str
    codigo: str | None = None
    direccion: str
    comuna: str
    ciudad: str


class UsuarioOut(BaseModel):
    id: str
    empresa_rut: str
    email: str
    nombre: str
    rol: str
    activo: bool
    ambiente_activo: str | None = None
    sucursal_id: str | None = None
    sucursal_nombre: str | None = None
    sucursales: list[SucursalInfo] = []

    model_config = {"from_attributes": True}


TokenResponse.model_rebuild()


# ── Helpers ──

def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ── Endpoints ──

@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, master_db: Session = Depends(get_master_db)):
    """Login contra master.db — devuelve JWT con empresa_rut, rol y sucursal.

    Flujo POS: envía sucursal_id → queda en JWT → todos los documentos
    usan automáticamente la dirección de esa sucursal.

    Flujo Admin: no envía sucursal_id → puede elegir después por request.
    """
    from crumbpos.db.models import Sucursal, UsuarioSucursal

    user = master_db.query(UsuarioAuth).filter(
        UsuarioAuth.email == body.email,
    ).first()
    if not user or not _verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
        )
    if not user.activo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario desactivado",
        )

    # Get ambiente activo de la empresa
    ambiente = None
    sucursales_info = []
    sucursal_id = None
    sucursal_nombre = None

    if user.empresa_rut != "SYSTEM":
        registro = master_db.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == user.empresa_rut,
        ).first()
        if registro:
            ambiente = registro.ambiente_activo

        # Cargar sucursales de la empresa para la respuesta
        try:
            emp_db = get_empresa_db_session(user.empresa_rut, ambiente or "certificacion")
            try:
                sucursales = emp_db.query(Sucursal).filter(
                    Sucursal.activa == True,
                ).order_by(Sucursal.nombre).all()

                sucursales_info = [
                    SucursalInfo(
                        id=s.id,
                        nombre=s.nombre,
                        codigo=s.codigo,
                        direccion=s.direccion,
                        comuna=s.comuna,
                        ciudad=s.ciudad,
                    )
                    for s in sucursales
                ]

                # Validar sucursal_id si viene en el login (POS)
                if body.sucursal_id:
                    suc = emp_db.query(Sucursal).filter(
                        Sucursal.id == body.sucursal_id,
                        Sucursal.activa == True,
                    ).first()
                    if not suc:
                        raise HTTPException(400, "Sucursal no encontrada o inactiva")

                    # Verificar acceso del usuario a esta sucursal
                    if user.rol not in ("super_admin", "admin_empresa"):
                        acceso = emp_db.query(UsuarioSucursal).filter(
                            UsuarioSucursal.usuario_id == user.id,
                            UsuarioSucursal.sucursal_id == body.sucursal_id,
                        ).first()
                        if not acceso:
                            raise HTTPException(403, "No tiene acceso a esta sucursal")

                    sucursal_id = suc.id
                    sucursal_nombre = suc.nombre
            finally:
                emp_db.close()
        except HTTPException:
            raise
        except Exception:
            pass  # Si no se puede cargar, continuar sin sucursales

    token = _create_access_token({
        "sub": user.id,
        "empresa_rut": user.empresa_rut,
        "rol": user.rol,
        "sucursal_id": sucursal_id,
    })
    return TokenResponse(
        access_token=token,
        usuario=UsuarioOut(
            id=user.id,
            empresa_rut=user.empresa_rut,
            email=user.email,
            nombre=user.nombre,
            rol=user.rol,
            activo=user.activo,
            ambiente_activo=ambiente,
            sucursal_id=sucursal_id,
            sucursal_nombre=sucursal_nombre,
            sucursales=sucursales_info,
        ),
    )


@router.get("/me", response_model=UsuarioOut)
def me(
    user: UsuarioAuth = Depends(get_current_user),
    master_db: Session = Depends(get_master_db),
):
    """Datos del usuario autenticado, incluyendo sucursales disponibles."""
    from crumbpos.db.models import Sucursal

    ambiente = None
    sucursales_info = []
    if user.empresa_rut != "SYSTEM":
        registro = master_db.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == user.empresa_rut,
        ).first()
        if registro:
            ambiente = registro.ambiente_activo

        try:
            emp_db = get_empresa_db_session(user.empresa_rut, ambiente or "certificacion")
            try:
                sucursales = emp_db.query(Sucursal).filter(
                    Sucursal.activa == True,
                ).order_by(Sucursal.nombre).all()
                sucursales_info = [
                    SucursalInfo(
                        id=s.id, nombre=s.nombre, codigo=s.codigo,
                        direccion=s.direccion, comuna=s.comuna, ciudad=s.ciudad,
                    )
                    for s in sucursales
                ]
            finally:
                emp_db.close()
        except Exception:
            pass

    return UsuarioOut(
        id=user.id,
        empresa_rut=user.empresa_rut,
        email=user.email,
        nombre=user.nombre,
        rol=user.rol,
        activo=user.activo,
        ambiente_activo=ambiente,
        sucursales=sucursales_info,
    )
