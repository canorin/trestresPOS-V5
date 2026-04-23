"""Endpoints de gestión de empresas — multi-tenant.

Super admin: puede crear, listar, modificar empresas y cambiar ambiente.
Admin empresa: puede ver y modificar solo su empresa.
"""
import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.orm import Session

from crumbpos.db.multi_tenant import (
    get_master_db,
    provision_empresa,
    cambiar_ambiente,
    cambiar_etapa,
    EmpresaRegistro,
    UsuarioAuth,
    get_empresa_db_session,
    ETAPAS_VALIDAS,
    PLANES_DISPONIBLES,
)
from crumbpos.db.models import Empresa, DteEmitido
from crumbpos.api.dependencies import (
    get_current_user,
    get_tenant,
    require_super_admin,
    TenantContext,
)

import bcrypt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/empresas", tags=["empresas"])


# ── Schemas ──

class SucursalCreateIn(BaseModel):
    """Datos de una sucursal al crear empresa."""
    nombre: str
    codigo: str | None = None
    direccion: str
    comuna: str
    ciudad: str
    sii_sucursal: str = "SANTIAGO ORIENTE"


class EmpresaCreateIn(BaseModel):
    """Datos para crear una nueva empresa (solo super_admin)."""
    rut: str
    razon_social: str
    giro: str
    acteco: int | None = None
    # Casa Matriz
    direccion: str
    comuna: str
    ciudad: str
    # Sucursales adicionales (aparte de casa matriz)
    sucursales: list[SucursalCreateIn] = []
    # Admin de la empresa
    admin_email: str
    admin_nombre: str
    admin_password: str
    # Plan comercial
    plan: str = "full_free"


class EmpresaUpdateIn(BaseModel):
    """Datos para actualizar config de empresa."""
    razon_social: str | None = None
    nombre_fantasia: str | None = None
    giro: str | None = None
    acteco: int | None = None
    direccion: str | None = None
    comuna: str | None = None
    ciudad: str | None = None
    fecha_resolucion: str | None = None
    numero_resolucion: int | None = None
    cert_rut_firmante: str | None = None
    tasa_iva: int | None = None


class EmpresaOut(BaseModel):
    rut: str
    razon_social: str
    nombre_fantasia: str | None = None
    giro: str
    acteco: int | None = None
    direccion: str
    comuna: str
    ciudad: str
    ambiente_activo: str
    ambiente_sii: str | None = None
    etapa: str = "pendiente_certificacion"
    plan: str = "full_free"
    total_documentos: int = 0
    fecha_resolucion: str | None = None
    numero_resolucion: int = 0
    cert_rut_firmante: str | None = None
    tiene_certificado: bool = False
    activa: bool = True


class CambiarAmbienteIn(BaseModel):
    ambiente: str  # "certificacion" | "produccion"


class CambiarEtapaIn(BaseModel):
    etapa: str  # "pendiente_certificacion" | "proceso_certificacion" | "produccion"


# ── Endpoints: Super Admin ──

@router.get("/", response_model=list[EmpresaOut])
def listar_empresas(
    user: UsuarioAuth = Depends(require_super_admin),
    master_db: Session = Depends(get_master_db),
):
    """Lista todas las empresas registradas (solo super_admin).

    Incluye plan, etapa y conteo de documentos emitidos. Excluye las que
    estén en papelera (estado='eliminada_soft') o eliminadas definitivamente
    (estado='eliminada_hard'). Esas viven en GET /api/admin/empresas/papelera.
    """
    registros = master_db.query(EmpresaRegistro).filter(
        EmpresaRegistro.estado == "activa",
    ).order_by(
        EmpresaRegistro.razon_social,
    ).all()

    result = []
    for reg in registros:
        # Leer config + contar DTEs desde la BD de la empresa
        empresa = None
        total_documentos = 0
        try:
            db = get_empresa_db_session(reg.rut, reg.ambiente_activo)
            try:
                empresa = db.query(Empresa).filter(Empresa.rut == reg.rut).first()
                total_documentos = db.query(DteEmitido).count()
            finally:
                db.close()
        except Exception:
            pass

        result.append(EmpresaOut(
            rut=reg.rut,
            razon_social=reg.razon_social,
            ambiente_activo=reg.ambiente_activo,
            ambiente_sii=empresa.ambiente_sii if empresa else None,
            etapa=reg.etapa,
            plan=reg.plan,
            total_documentos=total_documentos,
            giro=empresa.giro if empresa else "Sin configurar",
            acteco=empresa.acteco if empresa else None,
            direccion=empresa.direccion if empresa else "",
            comuna=empresa.comuna if empresa else "",
            ciudad=empresa.ciudad if empresa else "",
            fecha_resolucion=empresa.fecha_resolucion if empresa else None,
            numero_resolucion=empresa.numero_resolucion if empresa else 0,
            cert_rut_firmante=empresa.cert_rut_firmante if empresa else None,
            tiene_certificado=bool(empresa.cert_data or empresa.cert_path) if empresa else False,
            activa=reg.activa,
        ))
    return result


@router.post("/", response_model=EmpresaOut, status_code=201)
def crear_empresa(
    body: EmpresaCreateIn,
    user: UsuarioAuth = Depends(require_super_admin),
):
    """Crea una nueva empresa con su admin y bases de datos aisladas.

    Esto crea:
    - Registro en master.db (etapa: pendiente_certificacion, plan: full_free)
    - data/{rut}/certificacion.db con todas las tablas
    - data/{rut}/produccion.db con todas las tablas
    - Usuario admin en master + ambas BDs
    - Sucursal default "Casa Matriz" en ambas BDs
    """
    if body.plan not in PLANES_DISPONIBLES:
        raise HTTPException(400, f"Plan inválido: {body.plan}")

    password_hash = bcrypt.hashpw(
        body.admin_password.encode(), bcrypt.gensalt(),
    ).decode()

    sucursales_data = [s.model_dump() for s in body.sucursales] if body.sucursales else None

    try:
        empresa_id, user_id = provision_empresa(
            rut=body.rut,
            razon_social=body.razon_social,
            giro=body.giro,
            direccion=body.direccion,
            comuna=body.comuna,
            ciudad=body.ciudad,
            admin_email=body.admin_email,
            admin_password_hash=password_hash,
            admin_nombre=body.admin_nombre,
            acteco=body.acteco,
            sucursales=sucursales_data,
            plan=body.plan,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return EmpresaOut(
        rut=body.rut,
        razon_social=body.razon_social,
        giro=body.giro,
        acteco=body.acteco,
        direccion=body.direccion,
        comuna=body.comuna,
        ciudad=body.ciudad,
        ambiente_activo="certificacion",
        etapa="pendiente_certificacion",
        plan=body.plan,
        total_documentos=0,
        activa=True,
    )


@router.put("/{rut}/ambiente", response_model=dict)
def cambiar_ambiente_empresa(
    rut: str,
    body: CambiarAmbienteIn,
    user: UsuarioAuth = Depends(require_super_admin),
):
    """Cambia el ambiente activo de una empresa (certificacion ↔ produccion).

    Solo super_admin. Esto redirige todas las operaciones al otro ambiente
    sin perder los datos de ninguno.
    """
    try:
        nuevo = cambiar_ambiente(rut, body.ambiente)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "ok": True,
        "empresa_rut": rut,
        "ambiente_activo": nuevo,
        "mensaje": f"Empresa {rut} ahora opera en modo {nuevo}",
    }


@router.put("/{rut}/etapa", response_model=dict)
def cambiar_etapa_empresa(
    rut: str,
    body: CambiarEtapaIn,
    user: UsuarioAuth = Depends(require_super_admin),
):
    """Cambia la etapa del ciclo de vida de una empresa.

    Transiciones típicas:
      pendiente_certificacion → proceso_certificacion → produccion
    """
    if body.etapa not in ETAPAS_VALIDAS:
        raise HTTPException(
            400, f"Etapa inválida: {body.etapa}. Válidas: {ETAPAS_VALIDAS}",
        )
    try:
        nueva = cambiar_etapa(rut, body.etapa)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "ok": True,
        "empresa_rut": rut,
        "etapa": nueva,
        "mensaje": f"Empresa {rut} ahora está en etapa {nueva}",
    }


# ── Endpoints: Admin Empresa + Super Admin ──

@router.get("/mi-empresa", response_model=EmpresaOut)
def mi_empresa(tenant: TenantContext = Depends(get_tenant)):
    """Datos de la empresa del usuario autenticado."""
    try:
        empresa = tenant.db.query(Empresa).filter(
            Empresa.rut == tenant.empresa_rut,
        ).first()
        if not empresa:
            raise HTTPException(404, "Empresa no encontrada")

        return EmpresaOut(
            rut=empresa.rut,
            razon_social=empresa.razon_social,
            nombre_fantasia=empresa.nombre_fantasia,
            giro=empresa.giro,
            acteco=empresa.acteco,
            direccion=empresa.direccion,
            comuna=empresa.comuna,
            ciudad=empresa.ciudad,
            ambiente_activo=tenant.ambiente,
            ambiente_sii=empresa.ambiente_sii,
            fecha_resolucion=empresa.fecha_resolucion,
            numero_resolucion=empresa.numero_resolucion,
            cert_rut_firmante=empresa.cert_rut_firmante,
            tiene_certificado=bool(empresa.cert_data or empresa.cert_path),
            activa=empresa.activa,
        )
    finally:
        tenant.close()


@router.put("/mi-empresa", response_model=EmpresaOut)
def actualizar_mi_empresa(
    body: EmpresaUpdateIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Actualiza configuración de la empresa del usuario autenticado.

    Actualiza en AMBAS BDs (certificacion + produccion) para mantener
    consistencia de datos que son compartidos (razon_social, giro, etc).
    """
    try:
        # Actualizar en el ambiente activo
        empresa = tenant.db.query(Empresa).filter(
            Empresa.rut == tenant.empresa_rut,
        ).first()
        if not empresa:
            raise HTTPException(404, "Empresa no encontrada")

        _apply_updates(empresa, body)
        tenant.db.commit()
        tenant.db.refresh(empresa)

        # También actualizar en el otro ambiente
        otro_ambiente = "produccion" if tenant.ambiente == "certificacion" else "certificacion"
        try:
            otro_db = get_empresa_db_session(tenant.empresa_rut, otro_ambiente)
            otra_empresa = otro_db.query(Empresa).filter(
                Empresa.rut == tenant.empresa_rut,
            ).first()
            if otra_empresa:
                _apply_updates(otra_empresa, body)
                otro_db.commit()
            otro_db.close()
        except Exception as e:
            logger.warning("No se pudo actualizar en %s: %s", otro_ambiente, e)

        return EmpresaOut(
            rut=empresa.rut,
            razon_social=empresa.razon_social,
            nombre_fantasia=empresa.nombre_fantasia,
            giro=empresa.giro,
            acteco=empresa.acteco,
            direccion=empresa.direccion,
            comuna=empresa.comuna,
            ciudad=empresa.ciudad,
            ambiente_activo=tenant.ambiente,
            ambiente_sii=empresa.ambiente_sii,
            fecha_resolucion=empresa.fecha_resolucion,
            numero_resolucion=empresa.numero_resolucion,
            cert_rut_firmante=empresa.cert_rut_firmante,
            tiene_certificado=bool(empresa.cert_data or empresa.cert_path),
            activa=empresa.activa,
        )
    finally:
        tenant.close()


@router.post("/mi-empresa/certificado")
async def subir_certificado(
    password: str = Form(...),
    rut_firmante: str = Form(...),
    archivo: UploadFile = File(...),
    tenant: TenantContext = Depends(get_tenant),
):
    """Sube certificado digital (.pfx/.p12) para la empresa.

    Se guarda en AMBAS BDs (certificacion + produccion).
    """
    try:
        if not archivo.filename or not (
            archivo.filename.endswith(".pfx") or archivo.filename.endswith(".p12")
        ):
            raise HTTPException(400, "El archivo debe ser .pfx o .p12")

        contenido = await archivo.read()
        if len(contenido) == 0:
            raise HTTPException(400, "Archivo vacío")

        # Validar certificado
        try:
            from cryptography.hazmat.primitives.serialization.pkcs12 import (
                load_key_and_certificates,
            )
            from cryptography.hazmat.backends import default_backend
            load_key_and_certificates(contenido, password.encode(), default_backend())
        except Exception as e:
            raise HTTPException(
                400, f"Error abriendo certificado: {e}. Verifique la password.",
            )

        cert_b64 = base64.b64encode(contenido).decode("ascii")

        # Guardar en ambas BDs
        for ambiente in ("certificacion", "produccion"):
            try:
                db = get_empresa_db_session(tenant.empresa_rut, ambiente)
                empresa = db.query(Empresa).filter(
                    Empresa.rut == tenant.empresa_rut,
                ).first()
                if empresa:
                    empresa.cert_data = cert_b64
                    empresa.cert_password = password
                    empresa.cert_rut_firmante = rut_firmante
                    db.commit()
                db.close()
            except Exception as e:
                logger.error("Error guardando cert en %s: %s", ambiente, e)

        return {
            "ok": True,
            "mensaje": f"Certificado subido para {tenant.empresa_rut}",
            "rut_firmante": rut_firmante,
            "guardado_en": ["certificacion", "produccion"],
        }
    finally:
        tenant.close()


@router.get("/{rut}", response_model=EmpresaOut)
def obtener_empresa(
    rut: str,
    user: UsuarioAuth = Depends(require_super_admin),
    master_db: Session = Depends(get_master_db),
):
    """Datos completos de una empresa específica (solo super_admin).

    Usado por el wizard de certificación para prefillar el form del Paso 1
    al abrirse con ?rut=... en la URL.
    """
    reg = master_db.query(EmpresaRegistro).filter_by(rut=rut).first()
    if not reg:
        raise HTTPException(404, f"Empresa {rut} no registrada")
    if reg.estado != "activa":
        # Empresa en papelera o eliminada definitivamente: no prefill.
        raise HTTPException(
            410,
            f"Empresa {rut} está dada de baja (estado: {reg.estado}).",
        )

    empresa = None
    total_documentos = 0
    try:
        db = get_empresa_db_session(reg.rut, reg.ambiente_activo)
        try:
            empresa = db.query(Empresa).filter(Empresa.rut == reg.rut).first()
            total_documentos = db.query(DteEmitido).count()
        finally:
            db.close()
    except Exception as e:
        logger.warning("No se pudo cargar empresa %s de la BD tenant: %s", rut, e)

    return EmpresaOut(
        rut=reg.rut,
        razon_social=reg.razon_social,
        nombre_fantasia=empresa.nombre_fantasia if empresa else None,
        giro=empresa.giro if empresa else "Sin configurar",
        acteco=empresa.acteco if empresa else None,
        direccion=empresa.direccion if empresa else "",
        comuna=empresa.comuna if empresa else "",
        ciudad=empresa.ciudad if empresa else "",
        ambiente_activo=reg.ambiente_activo,
        ambiente_sii=empresa.ambiente_sii if empresa else None,
        etapa=reg.etapa,
        plan=reg.plan,
        total_documentos=total_documentos,
        fecha_resolucion=empresa.fecha_resolucion if empresa else None,
        numero_resolucion=empresa.numero_resolucion if empresa else 0,
        cert_rut_firmante=empresa.cert_rut_firmante if empresa else None,
        tiene_certificado=bool(empresa.cert_data or empresa.cert_path) if empresa else False,
        activa=reg.activa,
    )


# ── Helpers ──

def _apply_updates(empresa: Empresa, body: EmpresaUpdateIn):
    """Apply non-None fields from body to empresa model."""
    for field, value in body.model_dump(exclude_none=True).items():
        if hasattr(empresa, field):
            setattr(empresa, field, value)
