"""Endpoints de recepción de DTEs de proveedores (Ley 19.983).

C3 — módulo de recepción:

  POST /api/dtes-recibidos/upload
       Recibe un EnvioDTE de un proveedor. Parsea, valida firma (best-effort),
       persiste en dte_recibido y genera EnvioRecibos (acuse comercial).

  GET  /api/dtes-recibidos/
       Lista los DTEs recibidos con filtros de estado y fecha.

  POST /api/dtes-recibidos/{id}/reclamar
       Registra reclamo de contenido (plazo 8 días) o aceptación (30 días)
       según Ley 19.983.

Plazos legales:
  - Acuse de recibo: no hay plazo fijo pero se genera inmediatamente.
  - Reclamo de contenido: 8 días corridos desde recepción.
  - Aceptación mercadería: 30 días corridos o silencio implícito.
"""
from __future__ import annotations

import base64
import logging
import os
import tempfile
from datetime import datetime, date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel

from crumbpos.api.dependencies import get_tenant, TenantContext
from crumbpos.db.models import DteRecibido, Empresa
from crumbpos.core.intercambio.parser import parsear_envio_dte_sii

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dtes-recibidos", tags=["dtes-recibidos"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class DteRecibidoOut(BaseModel):
    id: str
    tipo_dte: int
    folio: int
    rut_emisor: str
    razon_social_emisor: str | None
    fecha_emision_doc: str | None
    monto_total: int | None
    estado_recepcion: str
    firma_valida: bool
    firma_error: str | None
    acuse_enviado_at: str | None
    motivo_reclamo: str | None
    reclamado_at: str | None
    aceptado_at: str | None
    created_at: str

    model_config = {"from_attributes": True}


class UploadDteRecibidoOut(BaseModel):
    ok: bool
    recibidos: int
    """Cantidad de DTEs persistidos en esta subida."""
    duplicados: int
    """DTEs ignorados por ya existir en la DB."""
    acuse_ok: bool
    """True si se generó y firmó el EnvioRecibos correctamente."""
    acuse_error: str | None
    """Detalle del error si el acuse falló."""
    dtes: list[DteRecibidoOut]


class ReclamarIn(BaseModel):
    accion: str
    """Acción a registrar: ``aceptar`` | ``reclamar`` | ``rechazar``."""
    motivo: str | None = None
    """Obligatorio si accion == 'reclamar' o 'rechazar'."""


class ReclamarOut(BaseModel):
    ok: bool
    id: str
    nuevo_estado: str
    mensaje: str


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_cert_empresa(empresa: Empresa) -> tuple[bytes, bytes] | None:
    """Retorna (private_key_pem, cert_der) del certificado de la empresa.

    Devuelve None si la empresa no tiene certificado configurado.
    """
    if not empresa.cert_data:
        return None
    try:
        pfx_bytes = base64.b64decode(empresa.cert_data)
        password = (empresa.cert_password or "").encode()
        from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
        fd, tmp = tempfile.mkstemp(suffix=".pfx")
        try:
            os.write(fd, pfx_bytes)
            os.close(fd)
            private_key, _, cert_der = cargar_certificado_pfx(tmp, empresa.cert_password)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
        private_key_pem = private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=NoEncryption(),
        )
        return private_key_pem, cert_der
    except Exception as e:
        logger.warning("No se pudo cargar cert de empresa para acuse: %s", e)
        return None


def _intentar_verificar_firma(xml_bytes: bytes) -> tuple[bool, str | None]:
    """Intenta verificar la firma del XML recibido (best-effort).

    Retorna (firma_valida, error_msg_o_None).
    No lanza excepciones — cualquier fallo se reporta como firma inválida.
    """
    try:
        from facturacion_electronica.firma import Firma as FirmaLib
        # Firma de verificación — no necesitamos cert propio para verificar
        # la firma del DTE del proveedor; FirmaLib lo verifica con el cert
        # embebido en el XML.
        # Usamos una instancia vacía (sin init_signature) solo para verificar.
        firma = FirmaLib({"init_signature": False})
        codigo, msg = firma.verificar_firma_xml(xml_bytes.decode("ISO-8859-1", errors="replace"))
        if codigo == 0:
            return True, None
        return False, msg
    except Exception as e:
        return False, str(e)[:200]


def _dte_recibido_to_out(dte: DteRecibido) -> DteRecibidoOut:
    return DteRecibidoOut(
        id=dte.id,
        tipo_dte=dte.tipo_dte,
        folio=dte.folio,
        rut_emisor=dte.rut_emisor,
        razon_social_emisor=dte.razon_social_emisor,
        fecha_emision_doc=str(dte.fecha_emision_doc) if dte.fecha_emision_doc else None,
        monto_total=dte.monto_total,
        estado_recepcion=dte.estado_recepcion,
        firma_valida=dte.firma_valida,
        firma_error=dte.firma_error,
        acuse_enviado_at=dte.acuse_enviado_at.isoformat() if dte.acuse_enviado_at else None,
        motivo_reclamo=dte.motivo_reclamo,
        reclamado_at=dte.reclamado_at.isoformat() if dte.reclamado_at else None,
        aceptado_at=dte.aceptado_at.isoformat() if dte.aceptado_at else None,
        created_at=dte.created_at.isoformat() if dte.created_at else "",
    )


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/upload", response_model=UploadDteRecibidoOut, status_code=201)
async def upload_dte_recibido(
    archivo: UploadFile = File(..., description="XML del EnvioDTE del proveedor"),
    tenant: TenantContext = Depends(get_tenant),
):
    """Recibe un sobre EnvioDTE de un proveedor.

    Proceso:
    1. Lee XML (ISO-8859-1) con parser seguro (S9).
    2. Verifica firma del sobre (best-effort).
    3. Persiste cada DTE en ``dte_recibido`` (ignora duplicados).
    4. Genera y firma ``EnvioRecibos`` (acuse comercial Ley 19.983).
    5. Retorna resumen de lo procesado.

    El acuse (``EnvioRecibos``) queda guardado en la DB; el frontend
    puede descargarlo vía ``GET /api/dtes-recibidos/{id}/acuse``.
    """
    try:
        xml_bytes = await archivo.read()
        if not xml_bytes:
            raise HTTPException(400, "Archivo XML vacío")
        if len(xml_bytes) > 5 * 1024 * 1024:  # 5 MB
            raise HTTPException(400, "Archivo demasiado grande (máximo 5 MB)")

        # Paso 1: parsear sobre
        try:
            nombre = archivo.filename or "ENVIO_DTE.xml"
            sobre = parsear_envio_dte_sii(xml_bytes, nombre_archivo=nombre)
        except Exception as e:
            raise HTTPException(400, f"Error parseando XML: {e}")

        if not sobre.dtes:
            raise HTTPException(400, "El sobre no contiene DTEs")

        # Paso 2: verificar firma del sobre (best-effort)
        firma_sobre_valida, firma_sobre_error = _intentar_verificar_firma(xml_bytes)
        logger.info(
            "Sobre de %s: %d DTEs, firma_valida=%s",
            sobre.rut_emisor, len(sobre.dtes), firma_sobre_valida,
        )

        # Paso 3: persistir cada DTE
        empresa = tenant.db.query(Empresa).filter(
            Empresa.rut == tenant.empresa_rut,
        ).first()
        if not empresa:
            raise HTTPException(404, "Empresa no encontrada")

        xml_b64 = base64.b64encode(xml_bytes).decode("ascii")
        recibidos: list[DteRecibido] = []
        duplicados = 0

        for dte in sobre.dtes:
            # Verificar si ya existe
            existente = tenant.db.query(DteRecibido).filter(
                DteRecibido.empresa_id == empresa.id,
                DteRecibido.tipo_dte == dte.tipo_dte,
                DteRecibido.folio == dte.folio,
                DteRecibido.rut_emisor == dte.rut_emisor,
            ).first()
            if existente:
                duplicados += 1
                continue

            try:
                fecha_doc = date.fromisoformat(dte.fch_emis) if dte.fch_emis else None
            except ValueError:
                fecha_doc = None

            nuevo = DteRecibido(
                empresa_id=empresa.id,
                tipo_dte=dte.tipo_dte,
                folio=dte.folio,
                rut_emisor=dte.rut_emisor,
                fecha_emision_doc=fecha_doc,
                monto_total=dte.mnt_total,
                estado_recepcion="pendiente",
                xml_recibido=xml_b64,
                firma_valida=firma_sobre_valida,
                firma_error=firma_sobre_error,
            )
            tenant.db.add(nuevo)
            recibidos.append(nuevo)

        tenant.db.flush()

        # Paso 4: generar acuse EnvioRecibos (si hay certificado + DTEs nuevos)
        acuse_ok = False
        acuse_error: str | None = None
        xml_acuse_b64: str | None = None

        if recibidos and empresa.cert_data:
            try:
                from crumbpos.core.intercambio.generador import (
                    armar_envio_recibos, ContactoIntercambio,
                )
                cert_tuple = _resolve_cert_empresa(empresa)
                if cert_tuple:
                    private_key_pem, cert_der = cert_tuple
                    contacto = ContactoIntercambio(
                        nombre=empresa.razon_social or "",
                        email=empresa.email_dte or "",
                        rut_firma=empresa.cert_rut_firmante or empresa.rut,
                        rut_responde=empresa.rut,
                    )
                    xml_acuse = armar_envio_recibos(sobre, contacto, private_key_pem, cert_der)
                    xml_acuse_b64 = base64.b64encode(xml_acuse).decode("ascii")
                    now = datetime.utcnow()
                    for dte_row in recibidos:
                        dte_row.xml_acuse = xml_acuse_b64
                        dte_row.acuse_enviado_at = now
                        dte_row.estado_recepcion = "acuse_enviado"
                    acuse_ok = True
                    logger.info(
                        "EnvioRecibos generado para %d DTEs de %s",
                        len(recibidos), sobre.rut_emisor,
                    )
                else:
                    acuse_error = "Certificado no disponible"
            except ValueError as e:
                # armar_envio_recibos lanza ValueError si no hay DTEs aceptados
                acuse_error = f"Acuse no generado: {e}"
                logger.warning("No se pudo generar EnvioRecibos: %s", e)
            except Exception as e:
                acuse_error = f"Error generando acuse: {type(e).__name__}"
                logger.error("Error generando EnvioRecibos: %s", e, exc_info=True)
        elif not empresa.cert_data:
            acuse_error = "Empresa sin certificado configurado — acuse pendiente"

        tenant.db.commit()
        for r in recibidos:
            tenant.db.refresh(r)

        return UploadDteRecibidoOut(
            ok=True,
            recibidos=len(recibidos),
            duplicados=duplicados,
            acuse_ok=acuse_ok,
            acuse_error=acuse_error,
            dtes=[_dte_recibido_to_out(r) for r in recibidos],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error en upload_dte_recibido: %s", e, exc_info=True)
        raise HTTPException(500, f"Error interno procesando DTE recibido: {type(e).__name__}")
    finally:
        tenant.close()


@router.get("/", response_model=list[DteRecibidoOut])
def listar_dtes_recibidos(
    estado: str | None = Query(None, description="Filtrar por estado_recepcion"),
    rut_emisor: str | None = Query(None, description="Filtrar por RUT del proveedor"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tenant: TenantContext = Depends(get_tenant),
):
    """Lista los DTEs recibidos de proveedores con filtros opcionales."""
    try:
        empresa = tenant.db.query(Empresa).filter(
            Empresa.rut == tenant.empresa_rut,
        ).first()
        if not empresa:
            raise HTTPException(404, "Empresa no encontrada")

        query = tenant.db.query(DteRecibido).filter(
            DteRecibido.empresa_id == empresa.id,
        )
        if estado:
            query = query.filter(DteRecibido.estado_recepcion == estado)
        if rut_emisor:
            query = query.filter(DteRecibido.rut_emisor == rut_emisor)

        dtes = query.order_by(DteRecibido.created_at.desc()).offset(offset).limit(limit).all()
        return [_dte_recibido_to_out(d) for d in dtes]
    finally:
        tenant.close()


@router.post("/{dte_id}/reclamar", response_model=ReclamarOut)
def reclamar_dte_recibido(
    dte_id: str,
    body: ReclamarIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Registra la decisión comercial sobre un DTE recibido.

    Acciones:
    - ``aceptar``: acepta mercadería/servicio (Ley 19.983, Art. 5b).
      Plazo: 30 días desde la fecha de recepción.
    - ``reclamar``: reclama el contenido del DTE (mercadería no recibida,
      precio incorrecto, etc.). Plazo: 8 días corridos (Art. 5c).
    - ``rechazar``: rechaza el DTE (RUT receptor incorrecto, DTE dirigido
      a otro contribuyente).

    Una vez en estado ``aceptado``, ``reclamado`` o ``rechazado`` no se
    puede cambiar (decisiones irrevocables según la ley).
    """
    try:
        empresa = tenant.db.query(Empresa).filter(
            Empresa.rut == tenant.empresa_rut,
        ).first()
        if not empresa:
            raise HTTPException(404, "Empresa no encontrada")

        dte = tenant.db.query(DteRecibido).filter(
            DteRecibido.id == dte_id,
            DteRecibido.empresa_id == empresa.id,
        ).first()
        if not dte:
            raise HTTPException(404, f"DTE recibido {dte_id} no encontrado")

        # Verificar que no esté ya resuelto
        _ESTADOS_FINALES = {"aceptado", "reclamado", "rechazado"}
        if dte.estado_recepcion in _ESTADOS_FINALES:
            raise HTTPException(
                409,
                f"El DTE ya tiene estado final '{dte.estado_recepcion}'. "
                f"No se puede cambiar.",
            )

        _ACCIONES_VALIDAS = {"aceptar", "reclamar", "rechazar"}
        if body.accion not in _ACCIONES_VALIDAS:
            raise HTTPException(
                422,
                f"Acción '{body.accion}' no reconocida. "
                f"Valores válidos: {sorted(_ACCIONES_VALIDAS)}.",
            )

        if body.accion in {"reclamar", "rechazar"} and not body.motivo:
            raise HTTPException(422, f"El campo 'motivo' es obligatorio para acción '{body.accion}'")

        now = datetime.utcnow()

        if body.accion == "aceptar":
            dte.estado_recepcion = "aceptado"
            dte.aceptado_at = now
            nuevo_estado = "aceptado"
            mensaje = "Mercadería/servicio aceptado correctamente."
        elif body.accion == "reclamar":
            dte.estado_recepcion = "reclamado"
            dte.motivo_reclamo = body.motivo
            dte.reclamado_at = now
            nuevo_estado = "reclamado"
            mensaje = f"Reclamo registrado: {body.motivo}"
        else:  # rechazar
            dte.estado_recepcion = "rechazado"
            dte.motivo_reclamo = body.motivo
            dte.reclamado_at = now
            nuevo_estado = "rechazado"
            mensaje = f"DTE rechazado: {body.motivo}"

        tenant.db.commit()

        logger.info(
            "DTE recibido %s: acción=%s, nuevo_estado=%s",
            dte_id, body.accion, nuevo_estado,
        )

        return ReclamarOut(
            ok=True,
            id=dte_id,
            nuevo_estado=nuevo_estado,
            mensaje=mensaje,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error en reclamar_dte_recibido: %s", e, exc_info=True)
        raise HTTPException(500, f"Error interno: {type(e).__name__}")
    finally:
        tenant.close()


@router.get("/{dte_id}/acuse")
def descargar_acuse(
    dte_id: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Descarga el XML del EnvioRecibos (acuse comercial Ley 19.983) generado.

    Retorna el XML en base64 para que el frontend pueda descargarlo o
    enviarlo manualmente al emisor del DTE.
    """
    try:
        empresa = tenant.db.query(Empresa).filter(
            Empresa.rut == tenant.empresa_rut,
        ).first()
        if not empresa:
            raise HTTPException(404, "Empresa no encontrada")

        dte = tenant.db.query(DteRecibido).filter(
            DteRecibido.id == dte_id,
            DteRecibido.empresa_id == empresa.id,
        ).first()
        if not dte:
            raise HTTPException(404, f"DTE recibido {dte_id} no encontrado")

        if not dte.xml_acuse:
            raise HTTPException(404, "Acuse aún no generado para este DTE")

        return {
            "dte_id": dte_id,
            "tipo_dte": dte.tipo_dte,
            "folio": dte.folio,
            "rut_emisor": dte.rut_emisor,
            "xml_acuse_base64": dte.xml_acuse,
            "acuse_enviado_at": dte.acuse_enviado_at.isoformat() if dte.acuse_enviado_at else None,
        }
    finally:
        tenant.close()
