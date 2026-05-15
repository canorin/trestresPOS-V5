"""FastAPI dependencies — multi-tenant DB routing.

Este módulo provee las dependencies centrales para:
  1. Autenticación contra master.db
  2. Routing al DB correcto (empresa + ambiente)
  3. Control de acceso por rol
"""
import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Header, Query, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.orm import Session

from crumbpos.db.multi_tenant import (
    get_master_db,
    get_empresa_db_session,
    get_empresa_registro,
    UsuarioAuth,
    EmpresaRegistro,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# JWT CONFIG (shared with auth router)
# ══════════════════════════════════════════════════════════════════

import os
import secrets

# JWT_SECRET: 32+ bytes random (high entropy). En producción debe venir
# de un secret manager. Generar con: secrets.token_urlsafe(64).
_JWT_DEFAULT = "dev-secret-change-in-production"
SECRET_KEY = os.getenv("JWT_SECRET", _JWT_DEFAULT)

# Fail-fast: si estamos en producción con el default inseguro, el proceso
# NO debe arrancar. Esto evita despliegues con secret comprometido.
if SECRET_KEY == _JWT_DEFAULT and os.getenv("CRUMBPOS_ENV", "").lower() == "production":
    raise RuntimeError(
        "JWT_SECRET no configurado en producción. "
        "Configurar variable de entorno con un valor de alta entropía "
        "(ej: `export JWT_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(64))')`)."
    )

# Aviso adicional para no-producción si dejan el default
if SECRET_KEY == _JWT_DEFAULT:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "⚠️  JWT_SECRET tiene valor por defecto. NO USAR EN PRODUCCIÓN. "
        "Configurar CRUMBPOS_ENV=production junto con JWT_SECRET seguro."
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480  # 8 horas

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


# ══════════════════════════════════════════════════════════════════
# CURRENT USER (from master.db)
# ══════════════════════════════════════════════════════════════════

def _decode_token(token: str) -> dict:
    """Decode JWT and return payload dict."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(
    token: str = Depends(oauth2_scheme),
    master_db: Session = Depends(get_master_db),
) -> UsuarioAuth:
    """Extrae y valida usuario del JWT token contra master.db."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido o expirado",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = _decode_token(token)
    user_id: str = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    user = master_db.query(UsuarioAuth).filter(
        UsuarioAuth.id == user_id,
    ).first()
    if user is None or not user.activo:
        raise credentials_exception
    return user


# ══════════════════════════════════════════════════════════════════
# TENANT DB — routes to correct empresa + ambiente database
# ══════════════════════════════════════════════════════════════════

class TenantContext:
    """Contexto del tenant actual — empresa + ambiente + sucursal + sesión DB.

    sucursal_id: viene del JWT (login POS) o None (admin sin sucursal fija).
    Cuando un POS se loguea con sucursal_id, TODOS los documentos
    emitidos llevan automáticamente la dirección de esa sucursal.
    """

    def __init__(
        self,
        db: Session,
        empresa_rut: str,
        ambiente: str,
        empresa_id: str,
        user: UsuarioAuth,
        sucursal_id: str | None = None,
    ):
        self.db = db
        self.empresa_rut = empresa_rut
        self.ambiente = ambiente
        self.empresa_id = empresa_id
        self.user = user
        self.sucursal_id = sucursal_id

    def close(self):
        self.db.close()


def get_tenant(
    token: str = Depends(oauth2_scheme),
    user: UsuarioAuth = Depends(get_current_user),
    master_db: Session = Depends(get_master_db),
    x_empresa_rut: str | None = Header(None, alias="X-Empresa-Rut"),
) -> TenantContext:
    """FastAPI dependency: resuelve el tenant (empresa + ambiente + sucursal).

    Para master_client / administrador / administrador_tienda / cajero:
    usa empresa_rut del JWT.
    Para super_admin: puede override con header X-Empresa-Rut.

    sucursal_id viene del JWT (establecido en login del POS).
    Si el POS se logueó con sucursal_id, todos los documentos
    emitidos usarán automáticamente la dirección de esa sucursal.
    """
    # Extraer sucursal_id del JWT
    payload = _decode_token(token)
    sucursal_id = payload.get("sucursal_id")

    # Determinar empresa_rut
    if user.rol == "super_admin":
        # Prioridad: header X-Empresa-Rut (override explícito desde la
        # consola super admin) > claim ``empresa_rut`` del JWT (shadow
        # session creada por POST /api/admin/empresas/{rut}/entrar).
        # El JWT de login normal del super admin trae "SYSTEM" en ese
        # claim, que es un sentinel interno y no un RUT real; lo
        # ignoramos y exigimos header.
        jwt_rut = payload.get("empresa_rut")
        if jwt_rut == "SYSTEM":
            jwt_rut = None
        empresa_rut = x_empresa_rut or jwt_rut
        if not empresa_rut:
            raise HTTPException(
                400,
                "Super admin debe especificar header X-Empresa-Rut o "
                "usar una shadow session (POST /api/admin/empresas/{rut}/entrar)",
            )
    else:
        empresa_rut = user.empresa_rut

    # Validar formato del RUT antes de usar en path/query — bloquea path traversal
    # vía header X-Empresa-Rut malicioso (ej: "../../etc").
    from crumbpos.utils.rut import RUTInvalidoError, validar_formato_rut
    try:
        empresa_rut = validar_formato_rut(empresa_rut)
    except RUTInvalidoError as exc:
        raise HTTPException(400, f"empresa_rut inválido: {exc}")
    # SYSTEM no es un RUT real: rechazar para tenants
    if empresa_rut == "SYSTEM":
        raise HTTPException(400, "Namespace SYSTEM no es un tenant válido")

    # Look up empresa en master
    registro = master_db.query(EmpresaRegistro).filter(
        EmpresaRegistro.rut == empresa_rut,
    ).first()
    if not registro:
        raise HTTPException(404, f"Empresa {empresa_rut} no encontrada")
    if not registro.activa:
        raise HTTPException(403, f"Empresa {empresa_rut} está desactivada")
    # Si la empresa fue dada de baja (papelera o eliminación definitiva),
    # ningún JWT debe poder operar contra ella — los archivos en disco
    # ya no están en data/{rut}/ sino en data/.trash/ o borrados.
    if registro.estado != "activa":
        raise HTTPException(
            410,  # Gone
            f"Empresa {empresa_rut} está dada de baja (estado: "
            f"{registro.estado}). No acepta operaciones.",
        )

    ambiente = registro.ambiente_activo

    # Get empresa DB session
    db = get_empresa_db_session(empresa_rut, ambiente)

    # Look up empresa_id from empresa DB
    from crumbpos.db.models import Empresa
    empresa = db.query(Empresa).filter(Empresa.rut == empresa_rut).first()
    if not empresa:
        db.close()
        raise HTTPException(500, f"Empresa {empresa_rut} no inicializada en BD {ambiente}")

    return TenantContext(
        db=db,
        empresa_rut=empresa_rut,
        ambiente=ambiente,
        empresa_id=empresa.id,
        user=user,
        sucursal_id=sucursal_id,
    )


# ══════════════════════════════════════════════════════════════════
# ROLE-BASED ACCESS
# ══════════════════════════════════════════════════════════════════

def require_role(*roles: str):
    """Dependency factory: requiere uno de los roles especificados."""
    def checker(user: UsuarioAuth = Depends(get_current_user)) -> UsuarioAuth:
        if user.rol not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Se requiere rol: {', '.join(roles)}",
            )
        return user
    return checker


def require_super_admin(user: UsuarioAuth = Depends(get_current_user)) -> UsuarioAuth:
    """Dependency: solo super_admin."""
    if user.rol != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el super administrador puede realizar esta acción",
        )
    return user
