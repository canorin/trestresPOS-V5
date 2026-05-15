"""Endpoints de autenticación — usa master.db para auth centralizado."""
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from crumbpos.core.security.rate_limit import login_limiter

from crumbpos.core.roles import puede_gestionar_empresa
from crumbpos.db.multi_tenant import (
    get_master_db, get_empresa_db_session, UsuarioAuth, EmpresaRegistro,
)
from crumbpos.api.dependencies import (
    get_current_user, get_tenant, TenantContext,
    SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES,
)

import bcrypt
from jose import jwt
from datetime import datetime, timezone

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Schemas ──

class LoginRequest(BaseModel):
    """Payload del login.

    ``empresa_rut`` namespaces la búsqueda: el par (empresa_rut, email) es
    único en ``usuario_auth``, así que un mismo correo puede ser master de
    varias empresas sin colisión. Los formularios ``/{rut}/login`` envían
    este campo; la consola super admin (``/admin/login``) lo omite y la
    búsqueda cae al namespace especial ``SYSTEM`` reservado para
    super_admin.
    """
    email: str
    password: str
    empresa_rut: str | None = None  # None = super_admin (namespace SYSTEM)
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
    # Si True, el frontend debe forzar el cambio de password antes de
    # permitir cualquier acción operativa. Lo emite el endpoint /login y
    # se limpia tras /cambiar-password exitoso.
    must_change_password: bool = False

    model_config = {"from_attributes": True}


TokenResponse.model_rebuild()


# ── Helpers ──

def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ── Endpoints ──

@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    master_db: Session = Depends(get_master_db),
):
    """Login contra master.db — devuelve JWT con empresa_rut, rol y sucursal.

    Flujo POS: envía sucursal_id → queda en JWT → todos los documentos
    usan automáticamente la dirección de esa sucursal.

    Flujo Admin: no envía sucursal_id → puede elegir después por request.

    Rate limiting: 5 intentos fallidos en 60s → lockout exponencial
    (60s, 120s, 240s, ..., cap 1 hora). Clave del rate limiter:
    `IP:empresa_rut:email`.
    """
    from crumbpos.db.models import Sucursal, UsuarioSucursal

    # Scope de búsqueda: si el caller no manda empresa_rut, asumimos login
    # super_admin (namespace reservado "SYSTEM"). El form de la consola
    # master cliente siempre manda el RUT de la empresa desde el path
    # ``/{rut}/login``.
    empresa_rut_scope = (body.empresa_rut or "SYSTEM").strip() or "SYSTEM"

    # Rate limiting: clave = IP + scope + email para evitar:
    #   - brute force por IP única
    #   - brute force por email/scope desde IPs rotativas (mitigado parcialmente)
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"{client_ip}:{empresa_rut_scope}:{body.email.lower()}"
    retry_after = login_limiter.check(rate_key)
    if retry_after > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Demasiados intentos fallidos. Reintenta en {retry_after} segundos.",
            headers={"Retry-After": str(retry_after)},
        )

    user = master_db.query(UsuarioAuth).filter(
        UsuarioAuth.empresa_rut == empresa_rut_scope,
        UsuarioAuth.email == body.email,
    ).first()
    if not user or not _verify_password(body.password, user.password_hash):
        # Registrar intento fallido para rate limiting
        login_limiter.fail(rate_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
        )
    if not user.activo:
        # No registrar como rate limit (no es brute force de password)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario desactivado",
        )

    # Login exitoso: limpiar el contador de rate limit
    login_limiter.success(rate_key)

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
                    # (admin de empresa tiene acceso automático a todas).
                    if not puede_gestionar_empresa(user.rol):
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
            must_change_password=bool(getattr(user, "must_change_password", False)),
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


# ── Self-service: cambio de password del user autenticado ──

class PasswordChangeIn(BaseModel):
    """Todo rol puede cambiar su propia password — incluso ``cajero``.

    Exige la password actual para confirmar identidad; el JWT no basta
    porque podría venir de una sesión olvidada en un dispositivo
    compartido.
    """
    password_actual: str
    password_nueva: str


@router.put("/me/password")
def cambiar_mi_password(
    body: PasswordChangeIn,
    user: UsuarioAuth = Depends(get_current_user),
    master_db: Session = Depends(get_master_db),
):
    """Cambia la contraseña del usuario autenticado.

    Actualiza primero ``master.db`` (autoritativo para login). Si el
    usuario pertenece a una empresa (no ``SYSTEM``), replica el nuevo
    hash en la tabla ``usuario`` del tenant para que ambas bases queden
    sincronizadas — el ``id`` es el mismo UUID entre master y tenant.
    """
    from crumbpos.db.models import Usuario as UsuarioTenant

    # 1. Verificar password actual.
    if not _verify_password(body.password_actual, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La contraseña actual es incorrecta",
        )

    if len(body.password_nueva) < 8:
        raise HTTPException(400, "La contraseña nueva debe tener al menos 8 caracteres")
    if body.password_nueva == body.password_actual:
        raise HTTPException(400, "La contraseña nueva debe ser distinta de la actual")

    # 2. Hashear y actualizar en master.db.
    nuevo_hash = bcrypt.hashpw(
        body.password_nueva.encode(), bcrypt.gensalt(),
    ).decode()

    user_master = master_db.query(UsuarioAuth).filter(
        UsuarioAuth.id == user.id,
    ).first()
    if not user_master:
        # Paranoia: el JWT validó pero el user desapareció.
        raise HTTPException(404, "Usuario no encontrado")
    user_master.password_hash = nuevo_hash
    # Cambio exitoso: limpiar flag y registrar timestamp.
    user_master.must_change_password = False
    user_master.password_changed_at = datetime.now(timezone.utc)
    master_db.commit()

    # 3. Replicar en tenant.db (solo si no es super_admin / SYSTEM).
    if user.empresa_rut != "SYSTEM":
        registro = master_db.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == user.empresa_rut,
        ).first()
        if registro:
            try:
                emp_db = get_empresa_db_session(
                    user.empresa_rut, registro.ambiente_activo,
                )
                try:
                    user_tenant = emp_db.query(UsuarioTenant).filter(
                        UsuarioTenant.id == user.id,
                    ).first()
                    if user_tenant:
                        user_tenant.password_hash = nuevo_hash
                        emp_db.commit()
                finally:
                    emp_db.close()
            except Exception:
                # master.db ya quedó al día; el tenant se re-sincroniza
                # en el próximo login/acción si hiciera falta. No
                # bloqueamos al usuario por un fallo de replicación.
                pass

    return {"ok": True, "detail": "Contraseña actualizada"}
