"""Endpoints de gestión de empresas — multi-tenant.

Super admin: puede crear, listar, modificar empresas y cambiar ambiente.
Admin empresa: puede ver y modificar solo su empresa.
"""
import base64
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from crumbpos.api.services.logo_empresa import (
    LogoValidationError,
    MIME_ALLOWED,
    eliminar_logo,
    guardar_logo,
    path_absoluto_logo,
)
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
    """Datos para crear una nueva empresa (solo super_admin).

    El admin que se crea aquí ES el master cliente de la empresa: el
    representante legal/dueño. Se guarda su RUT personal en
    ``UsuarioAuth.rut_personal`` (slug de login secundario) y se copia
    a ``EmpresaRegistro.representante_legal_*``. La password la genera
    el backend y se devuelve solo en la respuesta de creación.
    """
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
    # Admin/master cliente de la empresa (= representante legal)
    admin_email: str
    admin_nombre: str
    admin_rut_personal: str
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
    # Path relativo a DATA_DIR (``{rut}/logo.png``) si la empresa subió
    # logo. ``None`` significa que usa el logo default del sistema al
    # imprimir DTEs. El front usa la presencia para decidir si muestra
    # preview o prompt de upload.
    logo_url: str | None = None
    activa: bool = True


class EmpresaCreadaOut(BaseModel):
    """Respuesta de creación de empresa con credenciales del master cliente.

    La password inicial se devuelve UNA vez, en esta respuesta — el
    backend no la persiste en texto claro. El super admin la copia y
    la comparte con el cliente por correo (ID ``mail_credencial``).
    """
    empresa: EmpresaOut
    master_cliente: dict  # {email, nombre, rut_personal, password_inicial, login_url}


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
            logo_url=empresa.logo_url if empresa else None,
            activa=reg.activa,
        ))
    return result


@router.post("/", response_model=EmpresaCreadaOut, status_code=201)
def crear_empresa(
    body: EmpresaCreateIn,
    user: UsuarioAuth = Depends(require_super_admin),
):
    """Crea una nueva empresa con su master cliente y BDs aisladas.

    Esto crea:
    - Registro en master.db (etapa: pendiente_certificacion, plan: full_free,
      representante_legal_* = master cliente)
    - data/{rut}/certificacion.db con todas las tablas
    - data/{rut}/produccion.db con todas las tablas
    - UsuarioAuth (master.db) + Usuario (tenant.db) con rol ``master_client``
    - Sucursal default "Casa Matriz" en ambas BDs

    La password inicial se genera aquí (``secrets.token_urlsafe(12)`` →
    ~16 caracteres URL-safe, 96 bits de entropía) y se devuelve UNA
    sola vez en la respuesta. Se hashea con bcrypt antes de persistir,
    así no queda en claro en ninguna BD. El super admin copia la
    password y la envía al cliente por correo.
    """
    if body.plan not in PLANES_DISPONIBLES:
        raise HTTPException(400, f"Plan inválido: {body.plan}")

    password_inicial = secrets.token_urlsafe(12)
    password_hash = bcrypt.hashpw(
        password_inicial.encode(), bcrypt.gensalt(),
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
            admin_rut_personal=body.admin_rut_personal,
            acteco=body.acteco,
            sucursales=sucursales_data,
            plan=body.plan,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    empresa_out = EmpresaOut(
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
    return EmpresaCreadaOut(
        empresa=empresa_out,
        master_cliente={
            "email": body.admin_email,
            "nombre": body.admin_nombre,
            "rut_personal": body.admin_rut_personal,
            "password_inicial": password_inicial,
            "login_url": f"/{body.rut}/login",
        },
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
            logo_url=empresa.logo_url,
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
            logo_url=empresa.logo_url,
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


# ── Logo de la empresa ──────────────────────────────────────────────
#
# El logo se imprime en la representación visual de los DTEs (factura,
# boleta, guía). Se guarda en ``DATA_DIR/{rut}/logo.png`` y la columna
# ``empresa.logo_url`` apunta al path relativo. Las tres operaciones
# (subir, descargar, eliminar) sincronizan la columna en ambos
# ambientes — certificación y producción — porque el archivo físico es
# compartido y queremos que el valor en DB refleje la realidad del
# filesystem sin importar desde qué ambiente esté navegando el usuario.


class LogoOut(BaseModel):
    """Respuesta común de POST/DELETE del logo."""
    ok: bool
    logo_url: str | None
    mensaje: str


def _persistir_logo_url_ambos_ambientes(rut: str, nuevo_valor: str | None) -> None:
    """Escribe ``empresa.logo_url`` en certificación y producción.

    ``nuevo_valor=None`` limpia la referencia (borrado). No toca el
    archivo físico — eso lo hace el caller. Errores de un ambiente no
    abortan el otro, solo se loguean. El mismo patrón que
    ``subir_certificado`` usa para el cert digital.
    """
    for ambiente in ("certificacion", "produccion"):
        try:
            db = get_empresa_db_session(rut, ambiente)
            try:
                empresa = db.query(Empresa).filter(Empresa.rut == rut).first()
                if empresa:
                    empresa.logo_url = nuevo_valor
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error("Error actualizando logo_url en %s: %s", ambiente, e)


@router.post("/mi-empresa/logo", response_model=LogoOut)
async def subir_logo(
    archivo: UploadFile = File(...),
    tenant: TenantContext = Depends(get_tenant),
):
    """Sube el logo de la empresa (PNG o JPG, máx 2 MB).

    Normaliza a PNG ≤ 800 px por lado con PIL — así el renderer consume
    siempre el mismo formato y tamaño. Sobrescribe el logo anterior si
    existía. Devuelve el nuevo ``logo_url`` para que el front refresque
    la preview.
    """
    try:
        # La heurística por content-type es orientativa (cualquiera
        # puede falsificarlo); la validación real la hace PIL al abrir.
        if archivo.content_type and archivo.content_type not in MIME_ALLOWED:
            raise HTTPException(
                400,
                f"Tipo no permitido: {archivo.content_type}. "
                f"Aceptados: {', '.join(sorted(MIME_ALLOWED))}.",
            )

        contenido = await archivo.read()
        try:
            logo_url = guardar_logo(tenant.empresa_rut, contenido)
        except LogoValidationError as e:
            raise HTTPException(400, str(e))

        _persistir_logo_url_ambos_ambientes(tenant.empresa_rut, logo_url)

        return LogoOut(
            ok=True,
            logo_url=logo_url,
            mensaje=f"Logo subido para {tenant.empresa_rut}",
        )
    finally:
        tenant.close()


@router.get("/mi-empresa/logo", include_in_schema=False)
def descargar_logo(tenant: TenantContext = Depends(get_tenant)):
    """Devuelve el archivo PNG del logo, o 404 si aún no subió.

    Usado por el front de la consola para mostrar la preview y por el
    módulo de sync al cliente Windows para replicar el logo localmente.
    """
    try:
        destino = path_absoluto_logo(tenant.empresa_rut)
        if not destino.exists():
            raise HTTPException(404, "La empresa no tiene logo cargado")
        return FileResponse(
            path=str(destino),
            media_type="image/png",
            filename="logo.png",
        )
    finally:
        tenant.close()


@router.delete("/mi-empresa/logo", response_model=LogoOut)
def borrar_logo(tenant: TenantContext = Depends(get_tenant)):
    """Elimina el logo de la empresa (archivo + referencia en DB).

    Operación idempotente: si no había logo, devuelve ok=True igual.
    Tras borrar, los DTEs siguientes caen al logo default del sistema.
    """
    try:
        eliminar_logo(tenant.empresa_rut)
        _persistir_logo_url_ambos_ambientes(tenant.empresa_rut, None)
        return LogoOut(
            ok=True,
            logo_url=None,
            mensaje=f"Logo eliminado para {tenant.empresa_rut}",
        )
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
        logo_url=empresa.logo_url if empresa else None,
        activa=reg.activa,
    )


# ── Helpers ──

def _apply_updates(empresa: Empresa, body: EmpresaUpdateIn):
    """Apply non-None fields from body to empresa model."""
    for field, value in body.model_dump(exclude_none=True).items():
        if hasattr(empresa, field):
            setattr(empresa, field, value)
