"""CRUD de usuarios — permisos canónicos según ``crumbpos.core.roles``.

Este router mantiene dos BDs en sync para cada user:

* ``master.db:usuario_auth`` — identidad/auth global (email,
  password_hash, rol, empresa_rut). El login vive aquí.
* ``data/{rut}/{ambiente}.db:usuario`` — replica para queries
  internas del tenant (joins con ``usuario_sucursal``, etc).

Toda creación / cambio / desactivación escribe en ambas. El ``id``
(UUID) es el mismo en las dos para que FKs y JWTs resuelvan parejo.

Matriz de permisos — fuente canónica en ``crumbpos.core.roles``:

* **Listar/ver:** super_admin ve todo; master_client ve su empresa
  entera; administrador/administrador_tienda/cajero solo ven roles
  menores o iguales al suyo dentro de su empresa.
* **Crear:** ver ``CAN_CREATE`` en el módulo de roles.
  (super_admin crea todos menos otro super_admin; master_client crea
  cualquiera incluido otro master_client; etc.)
* **Cambiar password:** siempre la propia; para la de otros, hay que
  estar por encima en la jerarquía. Peer rule: master_client no pisa
  a otro master_client.
* **Desactivar:** misma regla que cambiar password — desactivar a
  alguien es tan sensible como rotarle la credencial.

Todos los chequeos de permisos se hacen server-side; el frontend
solo los usa para mostrar/ocultar controles, nunca como gate real.
"""
import uuid
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from crumbpos.core.roles import (
    ROLES_JERARQUIA,
    ROLES_LABEL,
    es_valido,
    normalizar,
    puede_cambiar_password,
    puede_crear,
    puede_ver_usuario,
)
from crumbpos.db.models import Usuario, UsuarioSucursal, Sucursal
from crumbpos.db.multi_tenant import get_master_session, UsuarioAuth
from crumbpos.api.dependencies import get_tenant, TenantContext, check_password_rate_limit
from crumbpos.api.error_utils import raise_safe_500


router = APIRouter(prefix="/api/usuarios", tags=["usuarios"])


# ─── SCHEMAS ───


class UsuarioCreateIn(BaseModel):
    email: str
    nombre: str
    password: str  # el creador la define; el user puede rotarla luego
    rol: str       # debe ser un rol que el actor está autorizado a crear
    rut_personal: Optional[str] = None
    sucursal_ids: list[str] = []


class UsuarioUpdateIn(BaseModel):
    email: Optional[str] = None
    nombre: Optional[str] = None
    rol: Optional[str] = None
    rut_personal: Optional[str] = None
    activo: Optional[bool] = None


class PasswordChangeIn(BaseModel):
    """Cambio de password que hace otra persona sobre el usuario target.

    El endpoint self-service (cambiar la propia) vive en
    ``/api/auth/me/password`` y pide la password actual además de la
    nueva, como capa extra de seguridad.
    """
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
    rol_label: str
    activo: bool
    sucursales: list[SucursalBrief] = []

    model_config = {"from_attributes": True}


# ─── HELPERS ───


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _build_usuario_out(user: Usuario, rut_personal: str | None = None) -> UsuarioOut:
    """Arma un UsuarioOut con sucursales + label humano del rol.

    ``rut_personal`` se pasa explícito porque vive en master.db
    (``UsuarioAuth``), no en tenant.db (``Usuario``) — el caller
    inyecta el valor cuando lo necesita en la respuesta.
    """
    sucursales = [
        SucursalBrief(
            sucursal_id=us.sucursal_id,
            sucursal_nombre=us.sucursal.nombre if us.sucursal else None,
        )
        for us in user.sucursales_acceso
    ]
    rol_canonico = normalizar(user.rol) or user.rol
    return UsuarioOut(
        id=user.id,
        empresa_id=user.empresa_id,
        email=user.email,
        nombre=user.nombre,
        rol=rol_canonico,
        rol_label=ROLES_LABEL.get(rol_canonico, rol_canonico),
        activo=user.activo,
        sucursales=sucursales,
    )


def _validate_sucursales(db: Session, empresa_id: str, sucursal_ids: list[str]):
    """Valida que todas las sucursales pertenezcan a la empresa."""
    if not sucursal_ids:
        return
    valid = db.query(Sucursal.id).filter(
        Sucursal.id.in_(sucursal_ids),
        Sucursal.empresa_id == empresa_id,
        Sucursal.activa == True,  # noqa: E712 (SQL truthy)
    ).all()
    valid_ids = {r[0] for r in valid}
    invalid = set(sucursal_ids) - valid_ids
    if invalid:
        raise HTTPException(
            400, f"Sucursales no válidas: {', '.join(sorted(invalid))}",
        )


def _tenant_user_or_404(tenant: TenantContext, usuario_id: str) -> Usuario:
    user = tenant.db.query(Usuario).filter(
        Usuario.id == usuario_id,
        Usuario.empresa_id == tenant.empresa_id,
    ).first()
    if not user:
        raise HTTPException(404, "Usuario no encontrado")
    return user


# ─── ENDPOINTS ───


@router.get("/", response_model=list[UsuarioOut])
def listar_usuarios(tenant: TenantContext = Depends(get_tenant)):
    """Lista usuarios visibles para el actor en su empresa.

    Visibilidad según ``puede_ver_usuario``: cada rol filtra a los
    que puede ver. Así el cajero ve solo cajeros, el admin_tienda ve
    cajeros + otros admin_tienda, y así sucesivamente.
    """
    try:
        actor_rol = tenant.user.rol
        actor_empresa = tenant.empresa_rut
        usuarios = tenant.db.query(Usuario).filter(
            Usuario.empresa_id == tenant.empresa_id,
        ).order_by(Usuario.nombre).all()
        return [
            _build_usuario_out(u)
            for u in usuarios
            if puede_ver_usuario(actor_rol, actor_empresa, u.rol, actor_empresa)
        ]
    finally:
        tenant.close()


@router.get("/{usuario_id}", response_model=UsuarioOut)
def obtener_usuario(usuario_id: str, tenant: TenantContext = Depends(get_tenant)):
    try:
        user = _tenant_user_or_404(tenant, usuario_id)
        if not puede_ver_usuario(
            tenant.user.rol, tenant.empresa_rut,
            user.rol, tenant.empresa_rut,
        ):
            raise HTTPException(403, "Sin permisos para ver este usuario")
        return _build_usuario_out(user)
    finally:
        tenant.close()


@router.post("/", response_model=UsuarioOut, status_code=201)
def crear_usuario(body: UsuarioCreateIn, tenant: TenantContext = Depends(get_tenant)):
    """Crea un sub-usuario de la empresa.

    Sincroniza master.db + tenant.db y valida contra la matriz de roles.
    El uniqueness de correo se chequea SCOPEADO POR RUT: un mismo correo
    puede existir como usuario de N empresas, pero no dos veces en la
    misma empresa (UNIQUE(empresa_rut, email) en master.db).
    """
    master_session = None
    try:
        rol_nuevo = normalizar(body.rol)
        if not es_valido(rol_nuevo):
            raise HTTPException(
                400,
                f"Rol inválido. Opciones: {', '.join(ROLES_JERARQUIA)}",
            )
        if not puede_crear(tenant.user.rol, rol_nuevo):
            raise HTTPException(
                403,
                f"Tu rol no puede crear usuarios con rol {rol_nuevo}",
            )

        # Uniqueness dentro del tenant (tenant.db usa UNIQUE global por email).
        existing = tenant.db.query(Usuario).filter(
            Usuario.email == body.email,
            Usuario.empresa_id == tenant.empresa_id,
        ).first()
        if existing:
            raise HTTPException(
                409, f"Ya existe un usuario con email {body.email} en esta empresa",
            )

        _validate_sucursales(tenant.db, tenant.empresa_id, body.sucursal_ids)

        password_hash = _hash_password(body.password)
        user_id = str(uuid.uuid4())

        # ── master.db: check de unicidad SCOPED por empresa_rut ──
        #
        # Antes el check era ``UsuarioAuth.email == body.email`` global,
        # alineado con UNIQUE(email). Después de migrar a
        # UNIQUE(empresa_rut, email) esa query rechaza correctamente
        # duplicados SOLO dentro del mismo tenant, así que el filtro
        # tiene que incluir empresa_rut. Si no, un master_client que
        # existe en otra empresa con el mismo correo bloquea la
        # creación aquí falsamente.
        master_session = get_master_session()
        master_existing = master_session.query(UsuarioAuth).filter(
            UsuarioAuth.empresa_rut == tenant.empresa_rut,
            UsuarioAuth.email == body.email,
        ).first()
        if master_existing:
            master_session.close()
            master_session = None
            raise HTTPException(
                409,
                f"Ya existe un usuario con email {body.email} en esta empresa (auth)",
            )

        master_session.add(UsuarioAuth(
            id=user_id,
            empresa_rut=tenant.empresa_rut,
            email=body.email,
            nombre=body.nombre,
            rut_personal=body.rut_personal,
            password_hash=password_hash,
            rol=rol_nuevo,
            activo=True,
            # Password creada por admin: forzar cambio en primer login.
            must_change_password=True,
        ))
        master_session.commit()

        # ── tenant.db: Usuario + asignaciones de sucursal ──
        empresa_user = Usuario(
            id=user_id,
            empresa_id=tenant.empresa_id,
            email=body.email,
            nombre=body.nombre,
            password_hash=password_hash,
            rol=rol_nuevo,
            activo=True,
        )
        tenant.db.add(empresa_user)
        for suc_id in body.sucursal_ids:
            tenant.db.add(UsuarioSucursal(
                usuario_id=user_id, sucursal_id=suc_id,
            ))
        tenant.db.commit()
        tenant.db.refresh(empresa_user)
        return _build_usuario_out(empresa_user, rut_personal=body.rut_personal)
    except HTTPException:
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise
    except Exception as e:
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise_safe_500(e, "crear usuario")
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
    """Edita un usuario. Permisos: las mismas reglas que crear.

    El cambio de rol es especialmente sensible: solo permitido si el
    actor puede "crear" al rol destino (regla ``puede_crear``). Así un
    administrador no puede auto-promoverse a master_client editando.
    """
    master_session = None
    try:
        user = _tenant_user_or_404(tenant, usuario_id)

        # Para tocar a alguien, hay que al menos poder verlo.
        if not puede_ver_usuario(
            tenant.user.rol, tenant.empresa_rut,
            user.rol, tenant.empresa_rut,
        ):
            raise HTTPException(403, "Sin permisos sobre este usuario")

        # Y estar por encima en la jerarquía (o ser el mismo user).
        if not puede_cambiar_password(
            tenant.user.rol, tenant.user.id,
            user.rol, user.id,
        ):
            raise HTTPException(
                403, "Sin permisos para editar este usuario",
            )

        updates = body.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(400, "No se enviaron campos para actualizar")

        # Si viene cambio de rol, aplicar regla de "puede crear ese rol"
        # al actor — un administrador no puede subirle el rol a nadie a
        # master_client.
        if "rol" in updates:
            nuevo = normalizar(updates["rol"])
            if not es_valido(nuevo):
                raise HTTPException(400, f"Rol inválido: {updates['rol']}")
            if not puede_crear(tenant.user.rol, nuevo):
                raise HTTPException(
                    403, f"No puedes asignar el rol {nuevo}",
                )
            updates["rol"] = nuevo

        for key, value in updates.items():
            if hasattr(user, key):
                setattr(user, key, value)

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
        return _build_usuario_out(
            user,
            rut_personal=auth_user.rut_personal if auth_user else None,
        )
    except HTTPException:
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise
    except Exception as e:
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise_safe_500(e, "actualizar usuario")
    finally:
        if master_session:
            master_session.close()
        tenant.close()


@router.put("/{usuario_id}/password")
def cambiar_password(
    usuario_id: str,
    body: PasswordChangeIn,
    tenant: TenantContext = Depends(get_tenant),
    _rl: None = Depends(check_password_rate_limit),
):
    """Cambia la password de otro user (o la propia).

    Regla canónica: ``puede_cambiar_password`` en ``core.roles``.
    Si alguien cambia la propia, hay un endpoint más estricto
    ``/api/auth/me/password`` que pide la password actual.
    """
    master_session = None
    try:
        user = _tenant_user_or_404(tenant, usuario_id)

        if not puede_cambiar_password(
            tenant.user.rol, tenant.user.id,
            user.rol, user.id,
        ):
            raise HTTPException(
                403, "Sin permisos para cambiar esta contraseña",
            )

        password_hash = _hash_password(body.new_password)
        user.password_hash = password_hash

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
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise
    except Exception as e:
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise_safe_500(e, "cambiar contraseña")
    finally:
        if master_session:
            master_session.close()
        tenant.close()


@router.delete("/{usuario_id}")
def desactivar_usuario(usuario_id: str, tenant: TenantContext = Depends(get_tenant)):
    """Soft delete. Misma regla que cambiar password (mismo nivel de
    sensibilidad: sacarle el acceso a alguien equivale a rotarle la
    credencial). No permite auto-desactivación — el user tiene que
    ser desactivado por alguien por encima."""
    master_session = None
    try:
        user = _tenant_user_or_404(tenant, usuario_id)

        if tenant.user.id == usuario_id:
            raise HTTPException(
                400, "No puedes desactivarte a ti mismo. "
                "Pídele a alguien por encima que lo haga.",
            )
        if not puede_cambiar_password(
            tenant.user.rol, tenant.user.id,
            user.rol, user.id,
        ):
            raise HTTPException(
                403, "Sin permisos para desactivar este usuario",
            )

        user.activo = False
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
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise
    except Exception as e:
        if master_session:
            master_session.rollback()
        tenant.db.rollback()
        raise_safe_500(e, "desactivar usuario")
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
    """Asigna sucursales a un usuario (reemplaza asignaciones previas).

    Permisos: misma regla que cambiar password — quien puede rotar la
    credencial puede también decidir qué sucursales tocar.
    """
    try:
        user = _tenant_user_or_404(tenant, usuario_id)
        if not puede_cambiar_password(
            tenant.user.rol, tenant.user.id,
            user.rol, user.id,
        ):
            raise HTTPException(403, "Sin permisos para asignar sucursales")

        _validate_sucursales(tenant.db, tenant.empresa_id, body.sucursal_ids)

        tenant.db.query(UsuarioSucursal).filter(
            UsuarioSucursal.usuario_id == usuario_id,
        ).delete()
        for suc_id in body.sucursal_ids:
            tenant.db.add(UsuarioSucursal(
                usuario_id=usuario_id, sucursal_id=suc_id,
            ))

        tenant.db.commit()
        tenant.db.refresh(user)
        return _build_usuario_out(user)
    finally:
        tenant.close()
