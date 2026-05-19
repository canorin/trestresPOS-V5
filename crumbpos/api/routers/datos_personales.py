"""Endpoints ARCO — derechos sobre datos personales (Ley 19.628, Chile).

B3: cumplimiento mínimo para datos de usuarios del sistema. Dos endpoints:

  GET  /api/datos-personales/me
       Devuelve los datos personales almacenados del usuario autenticado.

  POST /api/datos-personales/solicitud-cancelacion
       Registra una solicitud de cancelación (baja de datos) en master.db.
       El operador debe procesarla en máximo 5 días hábiles.

Alcance: datos del usuario empleado/admin (su propia cuenta). NO aplica a
datos de receptores de DTEs — ese tratamiento es distinto y cae dentro de las
excepciones contables y tributarias de la ley.
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from crumbpos.api.dependencies import get_current_user, get_master_db
from crumbpos.db.multi_tenant import UsuarioAuth, SolicitudArco, get_master_session

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/datos-personales", tags=["datos-personales"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class DatosPersonalesOut(BaseModel):
    """Datos personales del usuario autenticado."""
    usuario_id: str
    nombre: str
    email: str
    rut_personal: str | None
    empresa_rut: str
    rol: str
    activo: bool
    created_at: str
    # Lista de campos que almacenamos y su propósito (para la ficha ARCO)
    campos_almacenados: list[dict]


class SolicitudCancelacionIn(BaseModel):
    tipo: str = "cancelacion"
    """Tipo de derecho ARCO: acceso | rectificacion | cancelacion | oposicion"""
    motivo: str | None = None
    """Descripción libre de la solicitud. Puede incluir qué datos específicos
    quiere eliminar o rectificar el titular."""


class SolicitudCancelacionOut(BaseModel):
    solicitud_id: str
    tipo: str
    estado: str
    mensaje: str
    created_at: str


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/me", response_model=DatosPersonalesOut)
def mis_datos_personales(
    user: UsuarioAuth = Depends(get_current_user),
):
    """Devuelve los datos personales almacenados del usuario autenticado.

    Endpoint de derecho de **acceso** (Art. 12 Ley 19.628). El usuario puede
    conocer exactamente qué información personal tiene el sistema sobre él.
    """
    campos = [
        {
            "campo": "nombre",
            "descripcion": "Nombre completo del usuario.",
            "finalidad": "Identificación interna en la plataforma.",
        },
        {
            "campo": "email",
            "descripcion": "Correo electrónico.",
            "finalidad": "Autenticación y comunicaciones del sistema.",
        },
        {
            "campo": "rut_personal",
            "descripcion": "RUT personal del titular (si fue registrado).",
            "finalidad": "Identificación tributaria para documentos legales.",
        },
        {
            "campo": "empresa_rut",
            "descripcion": "RUT de la empresa a la que pertenece el usuario.",
            "finalidad": "Segmentación multi-tenant del sistema.",
        },
        {
            "campo": "rol",
            "descripcion": "Rol de acceso en la plataforma.",
            "finalidad": "Control de acceso y permisos.",
        },
        {
            "campo": "password_hash",
            "descripcion": "Hash bcrypt de la contraseña (no reversible).",
            "finalidad": "Autenticación segura. No se revela el valor.",
        },
        {
            "campo": "created_at",
            "descripcion": "Fecha de creación de la cuenta.",
            "finalidad": "Registro de alta en el sistema.",
        },
    ]

    return DatosPersonalesOut(
        usuario_id=user.id,
        nombre=user.nombre,
        email=user.email,
        rut_personal=user.rut_personal,
        empresa_rut=user.empresa_rut,
        rol=user.rol,
        activo=user.activo,
        created_at=user.created_at.isoformat() if user.created_at else "",
        campos_almacenados=campos,
    )


@router.post(
    "/solicitud-cancelacion",
    response_model=SolicitudCancelacionOut,
    status_code=201,
)
def solicitar_cancelacion(
    body: SolicitudCancelacionIn,
    user: UsuarioAuth = Depends(get_current_user),
):
    """Registra una solicitud de derechos ARCO del usuario autenticado.

    El operador de la plataforma (Crumb) queda obligado a procesar la
    solicitud en un plazo máximo de 5 días hábiles (Art. 13 Ley 19.628).

    Tipos aceptados: ``cancelacion`` (baja de datos), ``acceso`` (exportar),
    ``rectificacion`` (corregir datos), ``oposicion`` (oponerse al tratamiento).

    La solicitud queda registrada en master.db con estado ``pendiente``.
    """
    _TIPOS_VALIDOS = {"acceso", "rectificacion", "cancelacion", "oposicion"}
    if body.tipo not in _TIPOS_VALIDOS:
        raise HTTPException(
            422,
            f"Tipo de solicitud no reconocido: '{body.tipo}'. "
            f"Tipos válidos: {sorted(_TIPOS_VALIDOS)}.",
        )

    solicitud_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    session = get_master_session()
    try:
        solicitud = SolicitudArco(
            id=solicitud_id,
            usuario_id=user.id,
            empresa_rut=user.empresa_rut,
            tipo=body.tipo,
            motivo=body.motivo,
            estado="pendiente",
            created_at=now,
        )
        session.add(solicitud)
        session.commit()
        logger.info(
            "ARCO solicitud_id=%s tipo=%s usuario=%s empresa=%s",
            solicitud_id, body.tipo, user.email, user.empresa_rut,
        )
    except Exception as exc:
        session.rollback()
        logger.error("Error registrando solicitud ARCO: %s", exc, exc_info=True)
        raise HTTPException(500, "Error al registrar la solicitud. Intente nuevamente.")
    finally:
        session.close()

    return SolicitudCancelacionOut(
        solicitud_id=solicitud_id,
        tipo=body.tipo,
        estado="pendiente",
        mensaje=(
            f"Su solicitud de {body.tipo} fue registrada con ID {solicitud_id}. "
            f"Será procesada en un plazo máximo de 5 días hábiles, "
            f"conforme a lo establecido en la Ley 19.628."
        ),
        created_at=now.isoformat(),
    )
