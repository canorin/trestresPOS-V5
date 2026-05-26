"""Endpoints de facturación electrónica — emisión DTE real.

Multi-tenant: usa TenantContext para acceder a la BD correcta
según la empresa y ambiente del usuario autenticado.
"""
import logging
import os
import base64
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from crumbpos.api.services.emision_dte import (
    ServicioEmisionDTE,
    EmisorConfig,
    EmisionResult,
    FacturaRequest,
)
from crumbpos.config import settings
from crumbpos.db.models import Empresa, CafFolio, DteEmitido, Sucursal
from crumbpos.core.caf.caf_manager_db import CAFManagerDB, FoliosAgotadosError
from crumbpos.api.dependencies import get_tenant, TenantContext, check_dte_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/facturacion", tags=["facturacion"])


# ── Configuración del emisor ──

def _get_servicio(tenant: TenantContext, sucursal_id: str | None = None) -> tuple[ServicioEmisionDTE, Empresa]:
    """Crea servicio de emisión para la empresa del tenant.

    Si sucursal_id se especifica, el DTE usará la dirección de esa sucursal.
    La empresa se obtiene del TenantContext (ya autenticado y ruteado).
    """
    import tempfile

    db = tenant.db
    empresa = db.query(Empresa).filter(Empresa.rut == tenant.empresa_rut).first()
    if not empresa:
        raise HTTPException(404, f"Empresa no encontrada: {tenant.empresa_rut}")
    if not empresa.fecha_resolucion:
        raise HTTPException(422, f"Empresa {tenant.empresa_rut} sin fecha_resolucion configurada")
    if not empresa.cert_rut_firmante:
        raise HTTPException(422, f"Empresa {tenant.empresa_rut} sin cert_rut_firmante configurado")

    cert_path = None
    cert_password = None
    caf_mgr_db = None
    caf_dir = None

    # Certificado desde DB (prioridad) o archivo local
    if empresa.cert_data:
        pfx_bytes = base64.b64decode(empresa.cert_data)
        fd, tmp_pfx = tempfile.mkstemp(suffix=".pfx")
        os.write(fd, pfx_bytes)
        os.close(fd)
        cert_path = tmp_pfx
        cert_password = empresa.cert_password
    elif empresa.cert_path and Path(empresa.cert_path).exists():
        cert_path = empresa.cert_path
        cert_password = empresa.cert_password

    # Fallback certificado: buscar .pfx local
    if not cert_path:
        base_dir = Path(settings.BASE_DIR)
        for d in [base_dir / "certificados", base_dir / "cert"]:
            if d.is_dir():
                pfx_files = list(d.glob("*.pfx")) + list(d.glob("*.p12"))
                if pfx_files:
                    cert_path = str(pfx_files[0])
                    break

    if not cert_path:
        raise HTTPException(500, "Certificado .pfx no encontrado para esta empresa")

    # CAFs desde DB
    caf_count = db.query(CafFolio).filter(CafFolio.empresa_id == empresa.id).count()
    if caf_count > 0:
        caf_mgr_db = CAFManagerDB(db, empresa.id)

    # Fallback CAFs: archivos locales
    if not caf_mgr_db:
        caf_dir_path = settings.CAF_DIR
        if caf_dir_path.is_dir():
            caf_dir = str(caf_dir_path)

    # Dirección de sucursal para el DTE (si se especifica)
    suc_dir = suc_com = suc_ciu = suc_sii = None
    if sucursal_id:
        sucursal = db.query(Sucursal).filter(
            Sucursal.id == sucursal_id,
            Sucursal.empresa_id == empresa.id,
        ).first()
        if sucursal:
            suc_dir = sucursal.direccion
            suc_com = sucursal.comuna
            suc_ciu = sucursal.ciudad
            suc_sii = sucursal.cdg_sii_sucursal  # Código numérico SII (CdgSIISucur)

    config = EmisorConfig(
        rut=tenant.empresa_rut,
        razon_social=empresa.razon_social,
        giro=empresa.giro,
        acteco=empresa.acteco or 0,
        direccion=empresa.direccion,
        comuna=empresa.comuna,
        ciudad=empresa.ciudad,
        unidad_sii=empresa.unidad_sii,
        fecha_resolucion=empresa.fecha_resolucion,
        numero_resolucion=empresa.numero_resolucion,
        cert_path=cert_path,
        ambiente=empresa.ambiente_sii,
        cert_password=cert_password,
        rut_firmante=empresa.cert_rut_firmante,
        caf_dir=caf_dir,
        sucursal_direccion=suc_dir,
        sucursal_comuna=suc_com,
        sucursal_ciudad=suc_ciu,
        sucursal_sii=suc_sii,
    )
    return ServicioEmisionDTE(config, caf_manager_db=caf_mgr_db), empresa


def _persist_dte_emitido(
    db: Session,
    empresa: Empresa,
    body: "EmitirFacturaIn",
    resultado: EmisionResult,
    sucursal_id: str | None = None,
    usuario_id: str | None = None,
    caja_id: str | None = None,
    ip_origen: str | None = None,
    user_agent: str | None = None,
) -> DteEmitido | None:
    """Persist a DteEmitido record.

    Persiste TANTO emisiones exitosas como rechazadas, para:
    - Auditoría legal (6 años de retención, regla SII).
    - Reintento de envíos pendientes (jobs de polling/reenvio).
    - Diagnóstico de fallas (glosa_sii queda registrada).

    El registro es UPSERT por (empresa_id, tipo_dte, folio): si ya existe
    (caso reintento), se actualizan los campos volátiles (track_id, estado,
    glosa) preservando los inmutables (xml_firmado original, fecha_emisión).

    Args:
        db: sesión SQLAlchemy del tenant.
        empresa: instancia de Empresa.
        body: payload de la emisión (para receptor_rut/razon).
        resultado: EmisionResult devuelto por el servicio de emisión.
        sucursal_id: ID de sucursal (JWT o body).
        usuario_id: ID del usuario que emite (trazabilidad).
        caja_id: ID de la caja POS (si aplica).
        ip_origen: IP del cliente HTTP (auditoría).
        user_agent: User-Agent del cliente HTTP.

    Returns:
        El DteEmitido (nuevo o actualizado), o None si no hay folio.
    """
    if resultado.folio is None:
        # Sin folio = error pre-emisión (CAF agotado, validación fallada).
        # No hay nada que persistir todavía.
        return None

    xml_text = None
    if resultado.xml_firmado:
        xml_text = base64.b64encode(resultado.xml_firmado).decode("ascii")

    # Estado: si ok+track_id → enviado; ok sin track → pendiente; !ok con xml → rechazado
    if resultado.ok and resultado.track_id:
        estado_sii = "enviado"
    elif resultado.ok:
        estado_sii = "pendiente"
    elif resultado.xml_firmado:
        # Falló el envío pero el XML se firmó OK → persistir para reintento
        estado_sii = "rechazado" if resultado.track_id else "error_envio"
    else:
        # Falló antes de firmar → no hay XML para persistir
        return None

    # Usar sucursal específica o buscar la primera activa como fallback
    if sucursal_id:
        sucursal = db.query(Sucursal).filter(Sucursal.id == sucursal_id).first()
    else:
        sucursal = db.query(Sucursal).filter(
            Sucursal.empresa_id == empresa.id,
            Sucursal.activa == True,
        ).first()

    # UPSERT: buscar registro previo del mismo (empresa, tipo, folio)
    existente = db.query(DteEmitido).filter(
        DteEmitido.empresa_id == empresa.id,
        DteEmitido.tipo_dte == body.tipo_dte,
        DteEmitido.folio == resultado.folio,
    ).first()

    if existente is not None:
        # Reintento o actualización post-envío: refrescar campos volátiles,
        # preservar el xml_firmado y timestamp_envio originales (idempotencia).
        existente.track_id = resultado.track_id or existente.track_id
        existente.estado_sii = estado_sii
        if resultado.error:
            existente.glosa_sii = (resultado.error or "")[:255]
        return existente

    dte_record = DteEmitido(
        empresa_id=empresa.id,
        sucursal_id=sucursal.id if sucursal else None,
        usuario_id=usuario_id,
        caja_id=caja_id,
        ip_origen=ip_origen,
        user_agent=(user_agent or "")[:255] if user_agent else None,
        tipo_dte=body.tipo_dte,
        folio=resultado.folio,
        fecha_emision=date.fromisoformat(datetime.now().strftime("%Y-%m-%d")),
        receptor_rut=body.receptor_rut,
        receptor_razon=body.receptor_razon,
        monto_neto=resultado.monto_neto,
        monto_exento=resultado.monto_exento,
        iva=resultado.iva,
        monto_total=resultado.monto_total or 0,
        xml_firmado=xml_text,
        ted_xml=resultado.ted_xml,
        track_id=resultado.track_id,
        estado_sii=estado_sii,
        glosa_sii=(resultado.error or "")[:255] if resultado.error else None,
        timestamp_envio=datetime.now(timezone.utc),
    )
    db.add(dte_record)
    return dte_record


# ── Schemas ──

class ItemIn(BaseModel):
    nombre: str
    cantidad: float | None = None
    precio_unitario: int = 0
    unidad_medida: str | None = None
    exento: bool = False
    descuento_pct: float | None = None
    monto_item: int | None = None


class DescuentoGlobalIn(BaseModel):
    tipo: str = "D"
    descripcion: str = "Descuento global"
    tipo_valor: str = "%"
    valor: float = 0
    indicador_exento: int | None = None


class ReferenciaIn(BaseModel):
    tipo_doc: int | str
    folio: int | str
    fecha: str | None = None
    razon: str | None = None
    codigo: int | str | None = None


class EmitirFacturaIn(BaseModel):
    sucursal_id: str | None = None  # Override manual (admin). Si no se indica, usa la del JWT (POS)
    tipo_dte: int = 33
    receptor_rut: str
    receptor_razon: str
    receptor_giro: str
    receptor_dir: str
    receptor_comuna: str
    receptor_ciudad: str | None = None
    items: list[ItemIn]
    referencias: list[ReferenciaIn] = []
    fma_pago: int | None = None
    fecha_vencimiento: str | None = None
    oc_numero: str | None = None
    oc_fecha: str | None = None
    descuentos_globales: list[DescuentoGlobalIn] = []
    ind_traslado: int | None = None
    tipo_despacho: int | None = None
    # Certificación: identificador del caso del set de pruebas (ej: "CASO 4768464-1")
    # Cuando se indica, se agrega automáticamente una Referencia SET al DTE
    caso_set: str | None = None
    # Validación: False = genera XML firmado sin enviar al SII (para revisión)
    enviar_sii: bool = True


class EmitirFacturaOut(BaseModel):
    ok: bool
    folio: int | None = None
    track_id: str | None = None
    monto_neto: int | None = None
    monto_exento: int | None = None
    iva: int | None = None
    monto_total: int | None = None
    pdf_base64: str | None = None
    error: str | None = None


# ── Endpoints ──

@router.post("/reset", include_in_schema=False)
def reset_servicio():
    """Reset del servicio (recarga certificado y CAFs)."""
    return {"ok": True}


@router.post("/emitir", response_model=EmitirFacturaOut)
def emitir_factura(
    body: EmitirFacturaIn,
    request: Request,
    tenant: TenantContext = Depends(get_tenant),
    _rl: None = Depends(check_dte_rate_limit),
):
    """Emite un DTE completo: genera XML, firma, envía al SII y genera PDF.

    Opera sobre la BD de la empresa y ambiente del usuario autenticado.
    """
    try:
        # Prioridad sucursal: body (admin override) > JWT (POS) > casa matriz
        sucursal_id = body.sucursal_id or tenant.sucursal_id

        # Trazabilidad operacional: capturar quien emite + IP + user-agent.
        # Permite auditoría legal ante disputas multi-cajero.
        ip_origen = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        usuario_id = tenant.user.id if tenant.user else None
        servicio, empresa = _get_servicio(tenant, sucursal_id=sucursal_id)

        # Auto-completar fecha_ref buscando en DB cuando no se proporciona
        # (FchRef es obligatorio en XSD para referencias a documentos reales)
        refs_dicts = None
        if body.referencias:
            refs_dicts = []
            for ref in body.referencias:
                rd = ref.model_dump()
                if not rd.get("fecha") and str(rd.get("tipo_doc", "")).isdigit():
                    # Buscar fecha del documento original en la DB
                    tipo_ref = int(rd["tipo_doc"])
                    folio_ref = int(rd["folio"]) if str(rd.get("folio", "")).isdigit() else None
                    if folio_ref:
                        doc_orig = tenant.db.query(DteEmitido).filter(
                            DteEmitido.empresa_id == empresa.id,
                            DteEmitido.tipo_dte == tipo_ref,
                            DteEmitido.folio == folio_ref,
                        ).first()
                        if doc_orig and doc_orig.fecha_emision:
                            fch = doc_orig.fecha_emision
                            rd["fecha"] = fch.strftime("%Y-%m-%d") if hasattr(fch, 'strftime') else str(fch)
                            logger.info("Auto-completado FchRef: T%s F%s → %s", tipo_ref, folio_ref, rd["fecha"])
                refs_dicts.append(rd)

        req = FacturaRequest(
            tipo_dte=body.tipo_dte,
            receptor_rut=body.receptor_rut,
            receptor_razon=body.receptor_razon,
            receptor_giro=body.receptor_giro,
            receptor_dir=body.receptor_dir,
            receptor_comuna=body.receptor_comuna,
            receptor_ciudad=body.receptor_ciudad,
            items=[item.model_dump() for item in body.items],
            referencias=refs_dicts,
            fma_pago=body.fma_pago,
            fecha_vencimiento=body.fecha_vencimiento,
            oc_numero=body.oc_numero,
            oc_fecha=body.oc_fecha,
            descuentos_globales=[dg.model_dump() for dg in body.descuentos_globales] if body.descuentos_globales else None,
            ind_traslado=body.ind_traslado,
            tipo_despacho=body.tipo_despacho,
            caso_set=body.caso_set,
        )

        # session+empresa_id habilitan enriquecimiento CodRef=3 en el core:
        # NC/ND que modifican monto leen los ítems originales desde el
        # DteEmitido referenciado. Un solo core procesa documentos tanto en
        # producción como en certificación — mismo fix sirve a ambos.
        try:
            resultado = servicio.emitir_factura(
                req,
                enviar_sii=body.enviar_sii,
                session=tenant.db,
                empresa_id=empresa.id,
            )
        except FoliosAgotadosError as e:
            # Sin folios en el slice solicitado — 409 con datos estructurados
            # para que el frontend ofrezca mover folios de otra sucursal/pool.
            raise HTTPException(
                409,
                detail={
                    "error": "folios_agotados",
                    "tipo_dte": e.tipo_dte,
                    "sucursal_id": e.sucursal_id,
                    "sucursales_con_stock": e.sucursales_con_stock,
                    "mensaje": str(e),
                },
            )

        # Persist DteEmitido — SIEMPRE (incluso si ok=False con XML generado).
        # Razón: la regla SII de conservación 6 años aplica a todo DTE
        # firmado, incluso si su envío falló. Y permite reintento.
        try:
            _persist_dte_emitido(
                tenant.db, empresa, body, resultado,
                sucursal_id=sucursal_id,
                usuario_id=usuario_id,
                ip_origen=ip_origen,
                user_agent=user_agent,
            )
        except Exception as exc:
            logger.error("Error persistiendo DteEmitido: %s", exc, exc_info=True)

        tenant.db.commit()

        pdf_b64 = None
        if resultado.pdf_bytes:
            pdf_b64 = base64.b64encode(resultado.pdf_bytes).decode("ascii")

        return EmitirFacturaOut(
            ok=resultado.ok,
            folio=resultado.folio,
            track_id=resultado.track_id,
            monto_neto=resultado.monto_neto,
            monto_exento=resultado.monto_exento,
            iva=resultado.iva,
            monto_total=resultado.monto_total,
            pdf_base64=pdf_b64,
            error=resultado.error,
        )
    finally:
        tenant.close()


@router.post("/emitir/pdf")
def emitir_factura_pdf(
    body: EmitirFacturaIn,
    tenant: TenantContext = Depends(get_tenant),
    _rl: None = Depends(check_dte_rate_limit),
):
    """Emite un DTE y retorna directamente el PDF."""
    try:
        sucursal_id = body.sucursal_id or tenant.sucursal_id
        servicio, empresa = _get_servicio(tenant, sucursal_id=sucursal_id)

        # Auto-completar fecha_ref (misma lógica que /emitir)
        refs_dicts = None
        if body.referencias:
            refs_dicts = []
            for ref in body.referencias:
                rd = ref.model_dump()
                if not rd.get("fecha") and str(rd.get("tipo_doc", "")).isdigit():
                    tipo_ref = int(rd["tipo_doc"])
                    folio_ref = int(rd["folio"]) if str(rd.get("folio", "")).isdigit() else None
                    if folio_ref:
                        doc_orig = tenant.db.query(DteEmitido).filter(
                            DteEmitido.empresa_id == empresa.id,
                            DteEmitido.tipo_dte == tipo_ref,
                            DteEmitido.folio == folio_ref,
                        ).first()
                        if doc_orig and doc_orig.fecha_emision:
                            fch = doc_orig.fecha_emision
                            rd["fecha"] = fch.strftime("%Y-%m-%d") if hasattr(fch, 'strftime') else str(fch)
                refs_dicts.append(rd)

        req = FacturaRequest(
            tipo_dte=body.tipo_dte,
            receptor_rut=body.receptor_rut,
            receptor_razon=body.receptor_razon,
            receptor_giro=body.receptor_giro,
            receptor_dir=body.receptor_dir,
            receptor_comuna=body.receptor_comuna,
            receptor_ciudad=body.receptor_ciudad,
            items=[item.model_dump() for item in body.items],
            referencias=refs_dicts,
            fma_pago=body.fma_pago,
            fecha_vencimiento=body.fecha_vencimiento,
            oc_numero=body.oc_numero,
            oc_fecha=body.oc_fecha,
            descuentos_globales=[dg.model_dump() for dg in body.descuentos_globales] if body.descuentos_globales else None,
            ind_traslado=body.ind_traslado,
            tipo_despacho=body.tipo_despacho,
        )

        # Igual que /emitir: session+empresa_id habilitan enriquecimiento
        # CodRef=3 en el core para NC/ND que modifican monto.
        try:
            resultado = servicio.emitir_factura(
                req,
                session=tenant.db,
                empresa_id=empresa.id,
            )
        except FoliosAgotadosError as e:
            raise HTTPException(
                409,
                detail={
                    "error": "folios_agotados",
                    "tipo_dte": e.tipo_dte,
                    "sucursal_id": e.sucursal_id,
                    "sucursales_con_stock": e.sucursales_con_stock,
                    "mensaje": str(e),
                },
            )

        try:
            _persist_dte_emitido(tenant.db, empresa, body, resultado, sucursal_id=sucursal_id)
        except Exception as exc:
            logger.error("Error persistiendo DteEmitido: %s", exc, exc_info=True)

        tenant.db.commit()

        if not resultado.ok:
            raise HTTPException(400, resultado.error)
        if not resultado.pdf_bytes:
            raise HTTPException(500, "Factura emitida pero error generando PDF")

        return Response(
            content=resultado.pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="DTE_T{body.tipo_dte}_F{resultado.folio}.pdf"',
            },
        )
    finally:
        tenant.close()


@router.get("/pendientes")
def listar_pendientes(tenant: TenantContext = Depends(get_tenant)):
    """Lista DTEs generados pero no enviados al SII (estado=pendiente)."""
    try:
        dtes = tenant.db.query(DteEmitido).filter(
            DteEmitido.empresa_id == tenant.empresa_id,
            DteEmitido.estado_sii == "pendiente",
        ).order_by(DteEmitido.tipo_dte, DteEmitido.folio).all()

        return [{
            "id": d.id,
            "tipo_dte": d.tipo_dte,
            "folio": d.folio,
            "fecha_emision": str(d.fecha_emision),
            "receptor_rut": d.receptor_rut,
            "receptor_razon": d.receptor_razon,
            "monto_total": d.monto_total,
            "tiene_xml": bool(d.xml_firmado),
        } for d in dtes]
    finally:
        tenant.close()


@router.get("/emitidos")
def listar_emitidos(
    tipo_dte: int | None = Query(None, description="Tipo DTE (33, 34, 39, 41, 52, 56, 61)"),
    estado_sii: str | None = Query(None, description="Estado SII (pendiente, enviado, aceptado, rechazado, reparo, error_envio)"),
    fecha_desde: str | None = Query(None, description="Fecha desde YYYY-MM-DD"),
    fecha_hasta: str | None = Query(None, description="Fecha hasta YYYY-MM-DD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: TenantContext = Depends(get_tenant),
):
    """Lista todos los DTEs emitidos con filtros opcionales. Para historial en el panel master cliente."""
    try:
        q = tenant.db.query(DteEmitido).filter(
            DteEmitido.empresa_id == tenant.empresa_id,
        )
        if tipo_dte is not None:
            q = q.filter(DteEmitido.tipo_dte == tipo_dte)
        if estado_sii:
            q = q.filter(DteEmitido.estado_sii == estado_sii)
        if fecha_desde:
            try:
                q = q.filter(DteEmitido.fecha_emision >= date.fromisoformat(fecha_desde))
            except ValueError:
                raise HTTPException(400, "fecha_desde inválida. Use YYYY-MM-DD")
        if fecha_hasta:
            try:
                q = q.filter(DteEmitido.fecha_emision <= date.fromisoformat(fecha_hasta))
            except ValueError:
                raise HTTPException(400, "fecha_hasta inválida. Use YYYY-MM-DD")

        total = q.count()
        dtes = (
            q.order_by(DteEmitido.fecha_emision.desc(), DteEmitido.folio.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": [
                {
                    "id": d.id,
                    "tipo_dte": d.tipo_dte,
                    "folio": d.folio,
                    "fecha_emision": str(d.fecha_emision),
                    "receptor_rut": d.receptor_rut,
                    "receptor_razon": d.receptor_razon,
                    "monto_total": d.monto_total,
                    "estado_sii": d.estado_sii,
                    "glosa_sii": d.glosa_sii,
                    "track_id": d.track_id,
                }
                for d in dtes
            ],
        }
    finally:
        tenant.close()


@router.post("/enviar-pendientes")
async def enviar_pendientes(
    tenant: TenantContext = Depends(get_tenant),
    _rl: None = Depends(check_dte_rate_limit),
):
    """Envía al SII todos los DTEs pendientes (generados pero no enviados).

    Usa el XML firmado ya guardado — NO genera ni consume folios nuevos.
    Agrupa por tipo de DTE en un solo sobre por tipo.
    """
    try:
        from crumbpos.core.sii_client.autenticacion import obtener_token, obtener_token_boleta
        from crumbpos.core.sii_client.envio import enviar_dte_async, enviar_boleta_async
        from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
        from facturacion_electronica.firma import Firma as FirmaLib

        empresa = tenant.db.query(Empresa).filter(
            Empresa.rut == tenant.empresa_rut,
        ).first()
        if not empresa:
            raise HTTPException(404, "Empresa no encontrada")

        dtes = tenant.db.query(DteEmitido).filter(
            DteEmitido.empresa_id == tenant.empresa_id,
            DteEmitido.estado_sii == "pendiente",
            DteEmitido.xml_firmado.isnot(None),
        ).order_by(DteEmitido.tipo_dte, DteEmitido.folio).all()

        if not dtes:
            return {"ok": True, "mensaje": "No hay DTEs pendientes", "enviados": 0}

        # Obtener tokens SII (DTE y Boleta usan tokens distintos)
        cert_path, cert_password = _resolve_cert(empresa)
        private_key, _, cert_der = cargar_certificado_pfx(cert_path, cert_password)
        token_dte = obtener_token(private_key, cert_der, empresa.ambiente_sii)

        # Token boleta: solo si hay boletas pendientes
        token_boleta = None
        tiene_boletas = any(d.tipo_dte in (39, 41) for d in dtes)
        if tiene_boletas:
            pfx_data = open(cert_path, "rb").read()
            firma_lib = FirmaLib({
                "string_firma": base64.b64encode(pfx_data).decode(),
                "string_password": cert_password or "",
                "init_signature": True,
                "rut_firmante": empresa.cert_rut_firmante or empresa.rut,
            })
            token_boleta = obtener_token_boleta(firma_lib, empresa.ambiente_sii)

        resultados = []
        for dte_record in dtes:
            xml_bytes = base64.b64decode(dte_record.xml_firmado)

            rut_envia = empresa.cert_rut_firmante or empresa.rut
            es_boleta = dte_record.tipo_dte in (39, 41)
            if es_boleta:
                resultado_sii = await enviar_boleta_async(
                    xml_bytes=xml_bytes,
                    token=token_boleta,
                    rut_emisor=empresa.rut,
                    ambiente=empresa.ambiente_sii,
                    rut_envia=rut_envia,
                )
            else:
                resultado_sii = await enviar_dte_async(
                    xml_bytes=xml_bytes,
                    token=token_dte,
                    rut_emisor=empresa.rut,
                    ambiente=empresa.ambiente_sii,
                    rut_envia=rut_envia,
                )

            track_id = resultado_sii.get("track_id")
            if track_id:
                dte_record.track_id = track_id
                dte_record.estado_sii = "enviado"
                resultados.append({
                    "tipo_dte": dte_record.tipo_dte,
                    "folio": dte_record.folio,
                    "track_id": track_id,
                    "estado": "enviado",
                })
            else:
                error = resultado_sii.get("glosa", resultado_sii.get("raw", "")[:200])
                resultados.append({
                    "tipo_dte": dte_record.tipo_dte,
                    "folio": dte_record.folio,
                    "track_id": None,
                    "estado": "error",
                    "error": error,
                })

        tenant.db.commit()

        ok_count = sum(1 for r in resultados if r["estado"] == "enviado")
        return {
            "ok": ok_count == len(resultados),
            "enviados": ok_count,
            "total": len(resultados),
            "detalle": resultados,
        }
    finally:
        tenant.close()


class EnviarSetIn(BaseModel):
    """Request para enviar un conjunto de DTEs agrupados en un solo EnvioDTE."""
    tipo_dtes: list[int] | None = None  # Filtrar por tipos (None = todos pendientes)
    folios: list[int] | None = None  # Filtrar por folios específicos


@router.post("/enviar-set")
async def enviar_set(
    body: EnviarSetIn | None = None,
    tenant: TenantContext = Depends(get_tenant),
    _rl: None = Depends(check_dte_rate_limit),
):
    """Agrupa DTEs pendientes en un solo EnvioDTE y envía al SII.

    Para certificación SII: todos los casos de un set deben ir
    en un único sobre EnvioDTE. Este endpoint:
    1. Lee los DTEs pendientes de la DB
    2. Extrae los <DTE> firmados de cada EnvioDTE individual
    3. Construye un nuevo EnvioDTE con todos los DTEs
    4. Firma el sobre y envía al SII
    NO consume folios nuevos — reutiliza los XMLs ya generados.

    Boletas (T39, T41) se envían en un sobre separado (EnvioBOLETA).
    """
    try:
        from lxml import etree
        from crumbpos.core.sii_client.autenticacion import obtener_token, obtener_token_boleta
        from crumbpos.core.sii_client.envio import enviar_dte_async, enviar_boleta_async
        from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
        from crumbpos.core.dte.generador_xml import generar_envio_dte, xml_to_string, SII_NS
        from facturacion_electronica.firma import Firma as FirmaLib

        empresa = tenant.db.query(Empresa).filter(
            Empresa.rut == tenant.empresa_rut,
        ).first()
        if not empresa:
            raise HTTPException(404, "Empresa no encontrada")

        # Filtrar DTEs pendientes
        query = tenant.db.query(DteEmitido).filter(
            DteEmitido.empresa_id == tenant.empresa_id,
            DteEmitido.estado_sii == "pendiente",
            DteEmitido.xml_firmado.isnot(None),
        )
        if body and body.tipo_dtes:
            query = query.filter(DteEmitido.tipo_dte.in_(body.tipo_dtes))
        if body and body.folios:
            query = query.filter(DteEmitido.folio.in_(body.folios))

        dtes = query.order_by(DteEmitido.tipo_dte, DteEmitido.folio).all()
        if not dtes:
            return {"ok": True, "mensaje": "No hay DTEs pendientes", "enviados": 0}

        # ── Protección DTE-3-100: no re-enviar folios ya enviados al SII ──
        ya_enviados = [d for d in dtes if d.track_id is not None]
        if ya_enviados:
            detalle = [f"T{d.tipo_dte} F{d.folio} (track {d.track_id})" for d in ya_enviados]
            raise HTTPException(
                400,
                f"Los siguientes DTEs ya fueron enviados al SII y serían rechazados "
                f"con DTE-3-100 (Repetido): {', '.join(detalle)}. "
                f"Para re-enviar un set, emita folios nuevos."
            )

        # ── Ordenamiento topológico por dependencias reales ──
        # Si un DTE referencia a otro del mismo sobre, el referenciado va primero.
        # Evita error SII REF-3-750 "DTE Referenciado no recibido".
        dtes = _ordenar_por_dependencias(dtes)

        # Separar boletas de DTEs normales
        dtes_normales = [d for d in dtes if d.tipo_dte not in (39, 41)]
        boletas = [d for d in dtes if d.tipo_dte in (39, 41)]

        cert_path, cert_password = _resolve_cert(empresa)
        rut_envia = empresa.cert_rut_firmante or empresa.rut

        resultados = []

        # ── Helper: extraer <DTE>...</DTE> firmado como string ──
        import re as _re
        SII_NS_STR = "http://www.sii.cl/SiiDte"
        XSI_NS_STR = "http://www.w3.org/2001/XMLSchema-instance"

        def _extraer_dte_strings(dte_records):
            """Extrae los <DTE ...>...</DTE> firmados como strings del XML almacenado.

            Verifica la firma de cada DTE individual antes de incluirlo.
            Si un DTE tiene firma inválida, lo excluye y loguea error.
            """
            dte_strings = []
            for d in dte_records:
                xml_text = base64.b64decode(d.xml_firmado).decode("ISO-8859-1", errors="replace")
                # Extraer <DTE ...>...</DTE> (incluye Signature interna)
                match = _re.search(r'(<DTE\b[^>]*>.*?</DTE>)', xml_text, _re.DOTALL)
                if match:
                    dte_str = match.group(1)
                    # Verificar firma del DTE individual antes de incluirlo
                    try:
                        pfx_data_v = open(cert_path, "rb").read()
                        firma_v = FirmaLib({
                            "string_firma": base64.b64encode(pfx_data_v).decode(),
                            "string_password": cert_password or "",
                            "init_signature": True,
                            "rut_firmante": rut_envia,
                        })
                        codigo, msg = firma_v.verificar_firma_xml(dte_str)
                        if codigo != 0:
                            logger.error(
                                "DTE T%s F%s: firma inválida (%s) — sería rechazado con DTE-3-505. Excluido del envío.",
                                d.tipo_dte, d.folio, msg,
                            )
                            d.estado_sii = "error_firma"
                            continue
                        logger.debug("DTE T%s F%s: firma verificada OK", d.tipo_dte, d.folio)
                    except Exception as e:
                        logger.warning("DTE T%s F%s: no se pudo verificar firma: %s", d.tipo_dte, d.folio, e)
                    dte_strings.append(dte_str)
                else:
                    logger.warning("DTE T%s F%s: no se encontró <DTE> en XML", d.tipo_dte, d.folio)
            return dte_strings

        def _build_subtotdte(dte_records):
            """Construye los <SubTotDTE> para la carátula."""
            conteo = {}
            for d in dte_records:
                conteo[d.tipo_dte] = conteo.get(d.tipo_dte, 0) + 1
            parts = []
            for tipo in sorted(conteo.keys()):
                parts.append(
                    f'<SubTotDTE><TpoDTE>{tipo}</TpoDTE>'
                    f'<NroDTE>{conteo[tipo]}</NroDTE></SubTotDTE>'
                )
            return "".join(parts)

        async def _firmar_y_enviar_async(
            dte_records, env_tag, schema, send_fn_async, token, firma_type="env",
        ):
            """Construye sobre, firma y envía (versión asíncrona)."""
            dte_strings = _extraer_dte_strings(dte_records)
            if not dte_strings:
                return None

            timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            subtot = _build_subtotdte(dte_records)
            schema_loc = f"{SII_NS_STR} {schema}"

            caratula = (
                f'<Caratula version="1.0">'
                f'<RutEmisor>{empresa.rut}</RutEmisor>'
                f'<RutEnvia>{rut_envia}</RutEnvia>'
                f'<RutReceptor>60803000-K</RutReceptor>'
                f'<FchResol>{empresa.fecha_resolucion}</FchResol>'
                f'<NroResol>{empresa.numero_resolucion or 0}</NroResol>'
                f'<TmstFirmaEnv>{timestamp}</TmstFirmaEnv>'
                f'{subtot}'
                f'</Caratula>'
            )

            all_dtes = "\n".join(dte_strings)
            env_str = (
                f'<{env_tag} xmlns="{SII_NS_STR}" '
                f'xmlns:xsi="{XSI_NS_STR}" '
                f'xsi:schemaLocation="{schema_loc}" '
                f'version="1.0">'
                f'<SetDTE ID="SetDoc">'
                f'{caratula}'
                f'{all_dtes}'
                f'</SetDTE>'
                f'</{env_tag}>'
            )

            # Firmar sobre (type='env') — mismo flujo que emision_dte.py
            pfx_data = open(cert_path, "rb").read()
            firma = FirmaLib({
                "string_firma": base64.b64encode(pfx_data).decode(),
                "string_password": cert_password or "",
                "init_signature": True,
                "rut_firmante": rut_envia,
            })
            signed_env = firma.firmar(env_str, "SetDoc", type=firma_type)
            if not signed_env:
                return {"error": "Error firmando sobre"}

            # Verificar firma del sobre antes de enviar
            try:
                codigo_env, msg_env = firma.verificar_firma_xml(signed_env)
                if codigo_env != 0:
                    logger.error("Firma sobre %s inválida: %s", env_tag, msg_env)
                    return {"error": f"Firma sobre {env_tag} inválida (pre-verificación): {msg_env}"}
                logger.debug("Firma sobre %s verificada OK", env_tag)
            except Exception as e:
                logger.warning("No se pudo verificar firma sobre %s: %s", env_tag, e)

            xml_final = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed_env
            xml_bytes = xml_final.encode("ISO-8859-1")

            logger.info("Sobre %s construido: %d bytes, %d DTEs", env_tag, len(xml_bytes), len(dte_strings))

            # Llamada asíncrona — libera el event loop durante el upload al SII
            return await send_fn_async(
                xml_bytes=xml_bytes,
                token=token,
                rut_emisor=empresa.rut,
                ambiente=empresa.ambiente_sii,
                rut_envia=rut_envia,
            )

        # ── Enviar DTEs normales agrupados ──
        if dtes_normales:
            private_key, _, cert_der = cargar_certificado_pfx(cert_path, cert_password)
            token_dte = obtener_token(private_key, cert_der, empresa.ambiente_sii)

            resultado_sii = await _firmar_y_enviar_async(
                dtes_normales, "EnvioDTE", "EnvioDTE_v10.xsd",
                enviar_dte_async, token_dte,
            )
            if resultado_sii:
                track_id = resultado_sii.get("track_id")
                if track_id:
                    for d in dtes_normales:
                        d.track_id = track_id
                        d.estado_sii = "enviado"
                    resultados.append({
                        "grupo": "DTE",
                        "tipos": sorted(set(d.tipo_dte for d in dtes_normales)),
                        "cantidad": len(dtes_normales),
                        "track_id": track_id,
                        "estado": "enviado",
                    })
                else:
                    error = resultado_sii.get("glosa") or resultado_sii.get("error") or str(resultado_sii.get("raw", ""))[:500]
                    resultados.append({
                        "grupo": "DTE",
                        "cantidad": len(dtes_normales),
                        "track_id": None,
                        "estado": "error",
                        "error": error,
                    })

        # ── Enviar boletas agrupadas ──
        if boletas:
            pfx_data = open(cert_path, "rb").read()
            firma_bol = FirmaLib({
                "string_firma": base64.b64encode(pfx_data).decode(),
                "string_password": cert_password or "",
                "init_signature": True,
                "rut_firmante": rut_envia,
            })
            token_bol = obtener_token_boleta(firma_bol, empresa.ambiente_sii)

            resultado_sii = await _firmar_y_enviar_async(
                boletas, "EnvioBOLETA", "EnvioBOLETA_v11.xsd",
                enviar_boleta_async, token_bol, firma_type="env_boleta",
            )
            if resultado_sii:
                track_id = resultado_sii.get("track_id")
                if track_id:
                    for d in boletas:
                        d.track_id = track_id
                        d.estado_sii = "enviado"
                    resultados.append({
                        "grupo": "BOLETA",
                        "cantidad": len(boletas),
                        "track_id": track_id,
                        "estado": "enviado",
                    })
                else:
                    error = resultado_sii.get("glosa") or resultado_sii.get("error") or str(resultado_sii.get("raw", ""))[:500]
                    resultados.append({
                        "grupo": "BOLETA",
                        "cantidad": len(boletas),
                        "track_id": None,
                        "estado": "error",
                        "error": error,
                    })

        tenant.db.commit()

        ok_count = sum(1 for r in resultados if r["estado"] == "enviado")
        return {
            "ok": ok_count == len(resultados),
            "enviados": sum(r.get("cantidad", 0) for r in resultados if r["estado"] == "enviado"),
            "total": len(dtes),
            "detalle": resultados,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error en enviar-set")
        raise HTTPException(500, f"Error enviando set: {e}")
    finally:
        tenant.close()


def _ordenar_por_dependencias(dtes: list) -> list:
    """Ordena DTEs por dependencias: si A referencia a B, B va antes que A.

    Extrae las referencias del XML firmado de cada DTE y construye un grafo
    de dependencias. Luego hace un ordenamiento topológico (Kahn's algorithm)
    para que el SII procese los referenciados antes que los que los referencian.

    Ejemplo: ND→NC→Factura se ordena como Factura, NC, ND.
    """
    import re as _re
    import base64
    from collections import defaultdict, deque

    if len(dtes) <= 1:
        return dtes

    # Clave única para cada DTE: (tipo_dte, folio)
    dte_map = {}
    for d in dtes:
        dte_map[(d.tipo_dte, d.folio)] = d

    # Extraer referencias de cada DTE desde su XML firmado
    # Buscamos <TpoDocRef>XX</TpoDocRef> y <FolioRef>YY</FolioRef>
    refs = {}  # (tipo, folio) → set of (tipo_ref, folio_ref)
    for d in dtes:
        key = (d.tipo_dte, d.folio)
        refs[key] = set()
        if not d.xml_firmado:
            continue
        try:
            xml_text = base64.b64decode(d.xml_firmado).decode("ISO-8859-1", errors="replace")
            # Extraer pares TpoDocRef + FolioRef de cada <Referencia>
            for match in _re.finditer(
                r'<TpoDocRef>(\d+)</TpoDocRef>.*?<FolioRef>(\d+)</FolioRef>',
                xml_text, _re.DOTALL,
            ):
                ref_tipo = int(match.group(1))
                ref_folio = int(match.group(2))
                ref_key = (ref_tipo, ref_folio)
                # Solo cuenta si el referenciado está en este mismo sobre
                if ref_key in dte_map:
                    refs[key].add(ref_key)
        except Exception:
            pass

    # Kahn's algorithm — ordenamiento topológico
    # Prioridad: Facturas (T33,T34) → NC (T61) → ND (T56) → Guías (T52)
    # Esto asegura que NC va antes que ND cuando ambos modifican el mismo
    # documento original (requerido por validación CodRef=3 del SII).
    _DTE_PRIORITY = {33: 0, 34: 1, 61: 2, 56: 3, 52: 4, 39: 5, 41: 6}

    def _sort_key(k):
        return (_DTE_PRIORITY.get(k[0], 99), k[1])

    in_degree = defaultdict(int)
    graph = defaultdict(list)  # dependencia → [dependientes]
    all_keys = sorted(dte_map.keys(), key=_sort_key)

    for key in all_keys:
        in_degree.setdefault(key, 0)
        for dep in refs.get(key, set()):
            graph[dep].append(key)
            in_degree[key] += 1

    # Cola ordenada: cuando varios DTEs quedan libres simultáneamente,
    # se procesan en orden de prioridad (NC antes que ND).
    queue = deque(sorted(
        (k for k in all_keys if in_degree[k] == 0),
        key=_sort_key,
    ))
    ordered = []

    while queue:
        node = queue.popleft()
        ordered.append(node)
        # Ordenar dependientes por prioridad antes de agregar a la cola
        released = []
        for dependent in graph[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                released.append(dependent)
        for r in sorted(released, key=_sort_key):
            queue.append(r)

    # Si hay ciclos (no debería), agregar los que falten al final
    remaining = [k for k in all_keys if k not in set(ordered)]
    ordered.extend(remaining)

    result = [dte_map[k] for k in ordered]
    logger.info(
        "Orden DTEs en sobre: %s",
        [(d.tipo_dte, d.folio) for d in result],
    )
    return result


def _resolve_cert(empresa: Empresa) -> tuple[str, str]:
    """Resuelve path y password del certificado."""
    import tempfile, os

    if empresa.cert_data:
        pfx_bytes = base64.b64decode(empresa.cert_data)
        fd, tmp_pfx = tempfile.mkstemp(suffix=".pfx")
        os.write(fd, pfx_bytes)
        os.close(fd)
        return tmp_pfx, empresa.cert_password

    if empresa.cert_path and Path(empresa.cert_path).exists():
        return empresa.cert_path, empresa.cert_password

    base_dir = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))
    for d in [base_dir / "certificados", base_dir / "cert"]:
        if d.is_dir():
            pfx_files = list(d.glob("*.pfx")) + list(d.glob("*.p12"))
            if pfx_files:
                return str(pfx_files[0]), empresa.cert_password

    raise HTTPException(500, "Certificado no encontrado")


# ── Anulación automática ──────────────────────────────────────────────────────

class AnularDteIn(BaseModel):
    tipo_dte: int
    """Tipo del DTE a anular: 33 (Factura) o 34 (Factura Exenta)."""
    motivo: str = "Anula documento"
    """Texto que aparece en el campo Razón de la Referencia del XML."""
    sucursal_id: str | None = None
    enviar_sii: bool = True


class AnularDteOut(BaseModel):
    ok: bool
    folio_nc: int | None = None
    """Folio de la Nota de Crédito T61 emitida."""
    track_id: str | None = None
    monto_total: int | None = None
    error: str | None = None


@router.post("/{folio}/anular", response_model=AnularDteOut)
def anular_dte(
    folio: int,
    body: AnularDteIn,
    request: Request,
    tenant: TenantContext = Depends(get_tenant),
):
    """Emite una Nota de Crédito T61 CodRef=1 anulando el DTE especificado.

    Extrae automáticamente los ítems del XML firmado del DTE original y
    construye la NC con la misma composición de ítems y la referencia
    CódigoRef=1 (Anula Documento Referenciado).

    Solo aplica a T33 (Factura) y T34 (Factura Exenta). Requiere que el
    DTE original tenga XML firmado almacenado.

    C2: endpoint de anulación para producción — no requiere que el master
    arme la NC a mano ni recuerde los ítems originales.
    """
    _TIPOS_ANULABLES = {33, 34}

    if body.tipo_dte not in _TIPOS_ANULABLES:
        raise HTTPException(
            422,
            f"Solo se pueden anular T33 y T34 con este endpoint. "
            f"Recibido: T{body.tipo_dte}.",
        )

    try:
        db = tenant.db
        sucursal_id = body.sucursal_id or tenant.sucursal_id

        empresa = db.query(Empresa).filter(Empresa.rut == tenant.empresa_rut).first()
        if not empresa:
            raise HTTPException(404, "Empresa no encontrada")

        # ── 1. Buscar DTE original ────────────────────────────────────
        original = db.query(DteEmitido).filter(
            DteEmitido.empresa_id == empresa.id,
            DteEmitido.tipo_dte == body.tipo_dte,
            DteEmitido.folio == folio,
        ).first()
        if not original:
            raise HTTPException(
                404,
                f"No se encontró DTE T{body.tipo_dte} folio {folio}.",
            )

        # ── 2. Verificar que tiene XML ────────────────────────────────
        if not original.xml_firmado:
            raise HTTPException(
                422,
                f"El DTE T{body.tipo_dte} folio {folio} no tiene XML firmado "
                f"almacenado. No se puede generar la NC automática.",
            )

        # ── 3. Extraer ítems del XML original ─────────────────────────
        xml_bytes = base64.b64decode(original.xml_firmado)
        try:
            items = ServicioEmisionDTE._extraer_items_del_xml_firmado(xml_bytes)
        except Exception as exc:
            raise HTTPException(
                422,
                f"Error extrayendo ítems del DTE original: {exc}",
            )
        if not items:
            raise HTTPException(
                422,
                f"No se encontraron ítems en el DTE T{body.tipo_dte} folio {folio}.",
            )

        # ── 4. Fecha del DTE original para FchRef ─────────────────────
        fecha_orig = original.fecha_emision
        fecha_ref_str = (
            fecha_orig.strftime("%Y-%m-%d")
            if hasattr(fecha_orig, "strftime")
            else str(fecha_orig)
        )

        # ── 5. Construir FacturaRequest T61 ───────────────────────────
        req = FacturaRequest(
            tipo_dte=61,
            receptor_rut=original.receptor_rut or "66666666-6",
            receptor_razon=original.receptor_razon or "Consumidor Final",
            receptor_giro=empresa.giro,
            receptor_dir=empresa.direccion,
            receptor_comuna=empresa.comuna,
            receptor_ciudad=empresa.ciudad or "",
            items=items,
            referencias=[{
                "tipo_doc": body.tipo_dte,
                "folio": folio,
                "fecha": fecha_ref_str,
                "cod_ref": 1,
                "razon": body.motivo,
            }],
        )

        # ── 6. Emitir via servicio ────────────────────────────────────
        servicio, _ = _get_servicio(tenant, sucursal_id=sucursal_id)
        try:
            resultado = servicio.emitir_factura(
                req,
                enviar_sii=body.enviar_sii,
                session=db,
                empresa_id=empresa.id,
            )
        except FoliosAgotadosError as e:
            raise HTTPException(
                409,
                detail={
                    "error": "folios_agotados",
                    "tipo_dte": 61,
                    "sucursal_id": e.sucursal_id,
                    "mensaje": str(e),
                },
            )

        # ── 7. Persistir DteEmitido para la NC ───────────────────────
        ip_origen = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        usuario_id = tenant.user.id if tenant.user else None
        try:
            _persist_dte_emitido(
                db=db,
                empresa=empresa,
                body=req,
                resultado=resultado,
                sucursal_id=sucursal_id,
                usuario_id=usuario_id,
                ip_origen=ip_origen,
                user_agent=user_agent,
            )
            db.commit()
        except Exception as exc:
            logger.error("Error persistiendo NC T61 (anulación): %s", exc, exc_info=True)

        if not resultado.ok:
            return AnularDteOut(ok=False, error=resultado.error)

        return AnularDteOut(
            ok=True,
            folio_nc=resultado.folio,
            track_id=resultado.track_id,
            monto_total=resultado.monto_total,
        )

    except HTTPException:
        raise
    except Exception as e:
        import secrets
        error_id = secrets.token_hex(8)
        logger.error(
            "Error anulando DTE [error_id=%s] T%s F%s empresa=%s: %s",
            error_id, body.tipo_dte, folio, tenant.empresa_rut, e,
            exc_info=True,
        )
        raise HTTPException(
            500,
            f"Error al anular DTE (error_id={error_id}). "
            f"Consulte los logs del servidor.",
        )
    finally:
        tenant.close()


@router.get("/info")
def info_facturacion(tenant: TenantContext = Depends(get_tenant)):
    """Info del servicio de facturación para la empresa actual."""
    try:
        servicio, _empresa = _get_servicio(tenant)
        info = {
            "emisor_rut": servicio.config.rut,
            "emisor_razon": servicio.config.razon_social,
            "ambiente": tenant.ambiente,
            "resolucion": f"N{servicio.config.numero_resolucion} del {servicio.config.fecha_resolucion}",
        }

        if servicio._caf_manager_db:
            estado = servicio._caf_manager_db.estado_folios()
            info["tipos_disponibles"] = [f["tipo_dte"] for f in estado]
            info["folios_actuales"] = {str(f["tipo_dte"]): f["folio_actual"] for f in estado}
            info["fuente_folios"] = "base_de_datos"
        else:
            servicio._cargar_cafs()
            tipos = servicio._caf_manager.tipos_disponibles() if servicio._caf_manager else []
            info["tipos_disponibles"] = tipos
            info["folios_actuales"] = {
                str(t): servicio._caf_manager._folio_actual.get(t, "?") for t in tipos
            } if servicio._caf_manager else {}
            info["fuente_folios"] = "archivos_locales"

        return info
    except Exception as e:
        return {"error": str(e)}
    finally:
        tenant.close()
