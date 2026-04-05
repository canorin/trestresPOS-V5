"""CRUD de usuarios — multi-tenant con sync master.db + empresa DB."""
import uuid
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from crumbpos.db.models import Usuario, UsuarioSucursal, Sucursal
from crumbpos.db.multi_tenant import get_master_session, UsuarioAuth
from crumbpos.api.dependencies import get_tenant, TenantContext


router = APIRouter(prefix="/api/usuarios", tags=["usuarios"])


# ─── SCHEMAS ───

class UsuarioCreateIn(BaseModel):
    email: str
    nombre: str
    password: str
    rol: str  # admin_empresa, admin_sucursal, cajero
    sucursal_ids: list[str] = []


class UsuarioUpdateIn(BaseModel):
    email: Optional[str] = None
    nombre: Optional[str] = None
    rol: Optional[str] = None
    activo: Optional[bool] = None


class PasswordChangeIn(BaseModel):
    new_password: str


class SucursalAssignIn(BaseModel):
    sucursal_ids: list[str]


class SucursalBrief(BaseModel):
    sucursal_id: str
    sucursal_nombre: Optional[str] = None

    model_config = {"from_attributes": True}


class UsuarioOut(BaseModel):
    id: str
    empresa_id: str
    email: str
    nombre: str
    rol: str
    activo: bool
    sucursales: list[SucursalBrief] = []

    model_config = {"from_attributes": True}


# ─── HELPERS ───

def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _build_usuario_out(user: Usuario) -> dict:
    """Build UsuarioOut dict including sucursales."""
    sucursales = []
    for us in user.sucursales_acceso:
        sucursales.append(SucursalBrief(
            sucursal_id=us.sucursal_id,
            sucursal_nombre=us.sucursal.nombre if us.sucursal else None,
        ))
    return UsuarioOut(
        id=user.id,
        empresa_id=user.empresa_id,
        email=user.email,
        nombre=user.nombre,
        rol=user.rol,
        activo=user.activo,
        sucursales=sucursales,
    )


def _validate_sucursales(db: Session, empresa_id: str, sucursal_ids: list[str]):
    """Validate all sucursal_ids belong to the empresa."""
    if not sucursal_ids:
        return
    valid = db.query(Sucursal.id).filter(
        Sucursal.id.in_(sucursal_ids),
        Sucursal.empresa_id == empresa_id,
        Sucursal.activa == True,
    ).all()
    valid_ids = {r[0] for r in valid}
    invalid = set(sucursal_ids) - valid_ids
    if invalid:
        raise HTTPException(400, f"Sucursales no válidas: {', '.join(invalid)}")


# ─── ENDPOINTS ───

@router.get("/", response_model=list[UsuarioOut])
def listar_usuarios(tenant: TenantContext = Depends(get_tenant)):
    """Lista usuarios de la empresa."""
    try:
        if tenant.user.rol not in ("super_admin", "admin_empresa"):
            raise HTTPException(403, "No tiene permisos para listar usuarios")

        usuarios = tenant.db.query(Usuario).filter(
            Usuario.empresa_id == tenant.empresa_id,
        ).order_by(Usuario.nombre).all()
        return [_build_usuario_out(u) for u in usuarios]
    finally:
        tenant.close()


@router.get("/{usuario_id}", response_model=UsuarioOut)
def obtener_usuario(usuario_id: str, tenant: TenantContext = Depends(get_tenant)):
    """Obtiene un usuario con sus sucursales asignadas."""
    try:
        user = tenant.db.query(Usuario).filter(
            Usuario.id == usuario_id,
            Usuario.empresa_id == tenant.empresa_id,
        ).first()
        if not user:
            raise HTTPException(404, "Usuario no encontrado")
        return _build_usuario_out(user)
    finally:
        tenant.close()


@router.post("/", response_model=UsuarioOut, status_code=201)
def crear_usuario(body: UsuarioCreateIn, tenant: TenantContext = Depends(get_tenant)):
    """Crea usuario en AMBAS bases de datos (master.db + empresa DB)."""
    master_session = None
    try:
        if tenant.user.rol not in ("super_admin", "admin_empresa"):
            raise HTTPException(403, "No tiene permisos para crear usuarios")

        if body.rol not in ("admin_empresa", "admin_sucursal", "cajero"):
            raise HTTPException(400, "Rol inválido. Opciones: admin_empresa, admin_sucursal, cajero")

        # Check email uniqueness in empresa DB
        existing = tenant.db.query(Usuario).filter(
            Usuario.email == body.email,
        ).first()
        if existing:
            raise HTTPException(409, f"Ya existe un usuario con email {body.email}")

        # Validate sucursales
        _validate_sucursales(tenant.db, tenant.empresa_id, body.sucursal_ids)

        password_hash = _hash_password(body.password)
        user_id = str(uuid.uuid4())

        # 1. Create in master.db
        master_session = get_master_session()
        master_existing = master_session.query(UsuarioAuth).filter(
            UsuarioAuth.email == body.email,
        ).first()
        if master_existing:
            master_session.close()
            raise HTTPException(409, f"Ya existe un usuario con email {body.email} en el sistema")

        auth_user = UsuarioAuth(
            id=user_id,
            empresa_rut=tenant.empresa_rut,
            email=body.email,
            nombre=body.nombre,
            password_hash=password_hash,
            rol=body.rol,
            activo=True,
        )
        master_session.add(auth_user)
        master_session.commit()

        # 2. Create in empresa DB
        empresa_user = Usuario(
            id=user_id,
            empresa_id=tenant.empresa_id,
            email=body.email,
            nombre=body.nombre,
            password_hash=password_hash,
            rol=body.rol,
            activo=True,
        )
        tenant.db.add(empresa_user)

        # 3. Assign sucursales
        for suc_id in body.sucursal_ids:
            tenant.db.add(UsuarioSucursal(
                usuario_id=user_id,
                sucursal_id=suc_id,
            ))

        tenant.db.commit()
        tenant.db.refresh(empresa_user)

        return _build_usuario_out(empresa_user)
    except HTTPException:
        raise
    except Exception as e:
        # Rollback both if something fails
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise HTTPException(500, f"Error creando usuario: {str(e)}")
    finally:
        if master_session:
            master_session.close()
        tenant.close()


@router.put("/{usuario_id}", response_model=UsuarioOut)
def actualizar_usuario(
    usuario_id: str,
    body: UsuarioUpdateIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Actualiza datos del usuario en AMBAS bases de datos."""
    master_session = None
    try:
        if tenant.user.rol not in ("super_admin", "admin_empresa"):
            raise HTTPException(403, "No tiene permisos para editar usuarios")

        user = tenant.db.query(Usuario).filter(
            Usuario.id == usuario_id,
            Usuario.empresa_id == tenant.empresa_id,
        ).first()
        if not user:
            raise HTTPException(404, "Usuario no encontrado")

        updates = body.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(400, "No se enviaron campos para actualizar")

        # Update empresa DB
        for key, value in updates.items():
            setattr(user, key, value)

        # Update master.db
        master_session = get_master_session()
        auth_user = master_session.query(UsuarioAuth).filter(
            UsuarioAuth.id == usuario_id,
        ).first()
        if auth_user:
            for key, value in updates.items():
                if hasattr(auth_user, key):
                    setattr(auth_user, key, value)
            master_session.commit()

        tenant.db.commit()
        tenant.db.refresh(user)
        return _build_usuario_out(user)
    except HTTPException:
        raise
    except Exception as e:
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise HTTPException(500, f"Error actualizando usuario: {str(e)}")
    finally:
        if master_session:
            master_session.close()
        tenant.close()


@router.put("/{usuario_id}/password")
def cambiar_password(
    usuario_id: str,
    body: PasswordChangeIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Cambia la contraseña en AMBAS bases de datos."""
    master_session = None
    try:
        if tenant.user.rol not in ("super_admin", "admin_empresa"):
            # Allow users to change their own password
            if tenant.user.id != usuario_id:
                raise HTTPException(403, "No tiene permisos para cambiar esta contraseña")

        user = tenant.db.query(Usuario).filter(
            Usuario.id == usuario_id,
            Usuario.empresa_id == tenant.empresa_id,
        ).first()
        if not user:
            raise HTTPException(404, "Usuario no encontrado")

        password_hash = _hash_password(body.new_password)

        # Update empresa DB
        user.password_hash = password_hash

        # Update master.db
        master_session = get_master_session()
        auth_user = master_session.query(UsuarioAuth).filter(
            UsuarioAuth.id == usuario_id,
        ).first()
        if auth_user:
            auth_user.password_hash = password_hash
            master_session.commit()

        tenant.db.commit()
        return {"ok": True, "detail": "Contraseña actualizada"}
    except HTTPException:
        raise
    except Exception as e:
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise HTTPException(500, f"Error cambiando contraseña: {str(e)}")
    finally:
        if master_session:
            master_session.close()
        tenant.close()


@router.delete("/{usuario_id}")
def desactivar_usuario(usuario_id: str, tenant: TenantContext = Depends(get_tenant)):
    """Soft delete: desactiva usuario en AMBAS bases de datos."""
    master_session = None
    try:
        if tenant.user.rol not in ("super_admin", "admin_empresa"):
            raise HTTPException(403, "No tiene permisos para desactivar usuarios")

        user = tenant.db.query(Usuario).filter(
            Usuario.id == usuario_id,
            Usuario.empresa_id == tenant.empresa_id,
        ).first()
        if not user:
            raise HTTPException(404, "Usuario no encontrado")

        # Deactivate in empresa DB
        user.activo = False

        # Deactivate in master.db
        master_session = get_master_session()
        auth_user = master_session.query(UsuarioAuth).filter(
            UsuarioAuth.id == usuario_id,
        ).first()
        if auth_user:
            auth_user.activo = False
            master_session.commit()

        tenant.db.commit()
        return {"ok": True, "detail": f"Usuario '{user.nombre}' desactivado"}
    except HTTPException:
        raise
    except Exception as e:
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise HTTPException(500, f"Error desactivando usuario: {str(e)}")
    finally:
        if master_session:
            master_session.close()
        tenant.close()


@router.put("/{usuario_id}/sucursales")
def asignar_sucursales(
    usuario_id: str,
    body: SucursalAssignIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Asigna sucursales a un usuario (reemplaza asignaciones previas)."""
    try:
        if tenant.user.rol not in ("super_admin", "admin_empresa"):
            raise HTTPException(403, "No tiene permisos para asignar sucursales")

        user = tenant.db.query(Usuario).filter(
            Usuario.id == usuario_id,
            Usuario.empresa_id == tenant.empresa_id,
        ).first()
        if not user:
            raise HTTPException(404, "Usuario no encontrado")

        # Validate sucursales belong to empresa
        _validate_sucursales(tenant.db, tenant.empresa_id, body.sucursal_ids)

        # Remove existing assignments
        tenant.db.query(UsuarioSucursal).filter(
            UsuarioSucursal.usuario_id == usuario_id,
        ).delete()

        # Add new assignments
        for suc_id in body.sucursal_ids:
            tenant.db.add(UsuarioSucursal(
                usuario_id=usuario_id,
                sucursal_id=suc_id,
            ))

        tenant.db.commit()
        tenant.db.refresh(user)
        return _build_usuario_out(user)
    finally:
        tenant.close()
