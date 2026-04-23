"""Endpoints para Libros de Compras y Ventas Electrónicos (IECV) — multi-tenant."""
import json
import logging
import os
import re
import base64
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from crumbpos.api.services.emision_libros import ServicioLibros
from crumbpos.config import settings
from crumbpos.db.models import Empresa, LibroGenerado
from crumbpos.api.dependencies import get_tenant, TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/libros", tags=["libros"])


# ── Helper: create ServicioLibros ──

def _get_servicio_libros(tenant: TenantContext) -> tuple[ServicioLibros, Empresa]:
    """Creates a ServicioLibros for the tenant's empresa."""
    db = tenant.db
    empresa = db.query(Empresa).filter(Empresa.rut == tenant.empresa_rut).first()
    if not empresa:
        raise HTTPException(404, f"Empresa no encontrada: {tenant.empresa_rut}")
    if not empresa.fecha_resolucion:
        raise HTTPException(422, f"Empresa {tenant.empresa_rut} sin fecha_resolucion")
    if not empresa.cert_rut_firmante:
        raise HTTPException(422, f"Empresa {tenant.empresa_rut} sin cert_rut_firmante")

    base_dir = Path(settings.BASE_DIR)
    cert_path = None
    cert_password = None

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

    if not cert_path:
        for d in [base_dir / "certificados", base_dir / "cert"]:
            if d.is_dir():
                pfx_files = list(d.glob("*.pfx")) + list(d.glob("*.p12"))
                if pfx_files:
                    cert_path = str(pfx_files[0])
                    break

    if not cert_path:
        raise HTTPException(500, "Certificado .pfx no encontrado")

    servicio = ServicioLibros(
        empresa=empresa,
        cert_path=cert_path,
        cert_password=cert_password,
    )
    return servicio, empresa


# ── Schemas ──

class GenerarLibroGuiasIn(BaseModel):
    periodo: str  # "YYYY-MM"
    folio_notificacion: int = 0
    enviar: bool = True
    guias_anuladas: list[int] = []
    # Filtro opcional: solo incluir estos folios T52 (para certificación)
    folios: list[int] | None = None


class GenerarLibroGuiasOut(BaseModel):
    ok: bool
    libro_id: str | None = None
    track_id: str | None = None
    estado_sii: str | None = None
    total_dtes: int | None = None
    resumen: dict | None = None
    error: str | None = None


class GenerarLibroVentasIn(BaseModel):
    periodo: str  # "YYYY-MM"
    folio_notificacion: int = 0
    enviar: bool = True
    # Filtro opcional: solo incluir estos DTEs específicos (para certificación)
    # Formato: {"33": [123,124], "61": [97,98,99]}
    folios: dict[str, list[int]] | None = None


class GenerarLibroVentasOut(BaseModel):
    ok: bool
    libro_id: str | None = None
    track_id: str | None = None
    estado_sii: str | None = None
    total_dtes: int | None = None
    resumen: dict | None = None
    error: str | None = None


class LibroOut(BaseModel):
    id: str
    tipo_libro: str
    periodo: str
    track_id: str | None = None
    estado_sii: str
    resumen: dict | None = None
    created_at: str


# ── Endpoints ──

@router.post("/ventas/generar", response_model=GenerarLibroVentasOut)
def generar_libro_ventas(
    body: GenerarLibroVentasIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Genera, firma y envía el Libro de Ventas para un periodo."""
    try:
        if not re.match(r"^\d{4}-\d{2}$", body.periodo):
            raise HTTPException(400, "Formato de periodo inválido. Use YYYY-MM")

        servicio, empresa = _get_servicio_libros(tenant)

        resultado = servicio.generar_libro_ventas(
            db=tenant.db,
            periodo=body.periodo,
            folio_notificacion=body.folio_notificacion,
            enviar=body.enviar,
            folios_filter=body.folios,
        )

        return GenerarLibroVentasOut(
            ok=resultado.get("ok", False),
            libro_id=resultado.get("libro_id"),
            track_id=resultado.get("track_id"),
            estado_sii=resultado.get("estado_sii"),
            total_dtes=resultado.get("total_dtes"),
            resumen=resultado.get("resumen"),
            error=resultado.get("error"),
        )
    finally:
        tenant.close()


@router.get("/ventas/{periodo}", response_model=LibroOut | None)
def obtener_libro_ventas(
    periodo: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Obtiene el libro de ventas generado para un periodo."""
    try:
        if not re.match(r"^\d{4}-\d{2}$", periodo):
            raise HTTPException(400, "Formato de periodo inválido. Use YYYY-MM")

        libro = (
            tenant.db.query(LibroGenerado)
            .filter(
                LibroGenerado.empresa_id == tenant.empresa_id,
                LibroGenerado.tipo_libro == "VENTA",
                LibroGenerado.periodo == periodo,
            )
            .first()
        )

        if not libro:
            raise HTTPException(404, f"No hay libro de ventas para {periodo}")

        resumen = None
        if libro.resumen_json:
            try:
                resumen = json.loads(libro.resumen_json)
            except json.JSONDecodeError:
                pass

        return LibroOut(
            id=libro.id,
            tipo_libro=libro.tipo_libro,
            periodo=libro.periodo,
            track_id=libro.track_id,
            estado_sii=libro.estado_sii,
            resumen=resumen,
            created_at=libro.created_at.isoformat() if libro.created_at else "",
        )
    finally:
        tenant.close()


@router.get("/ventas", response_model=list[LibroOut])
def listar_libros_ventas(tenant: TenantContext = Depends(get_tenant)):
    """Lista todos los libros de ventas generados."""
    try:
        libros = (
            tenant.db.query(LibroGenerado)
            .filter(
                LibroGenerado.empresa_id == tenant.empresa_id,
                LibroGenerado.tipo_libro == "VENTA",
            )
            .order_by(LibroGenerado.periodo.desc())
            .all()
        )

        result = []
        for libro in libros:
            resumen = None
            if libro.resumen_json:
                try:
                    resumen = json.loads(libro.resumen_json)
                except json.JSONDecodeError:
                    pass

            result.append(LibroOut(
                id=libro.id,
                tipo_libro=libro.tipo_libro,
                periodo=libro.periodo,
                track_id=libro.track_id,
                estado_sii=libro.estado_sii,
                resumen=resumen,
                created_at=libro.created_at.isoformat() if libro.created_at else "",
            ))

        return result
    finally:
        tenant.close()


# ── Endpoints: Libro de Compras ──


class EntradaCompraIn(BaseModel):
    TpoDoc: int
    NroDoc: int
    FchDoc: str  # YYYY-MM-DD
    RUTDoc: str
    RznSoc: str
    MntExe: int = 0
    MntNeto: int = 0
    MntIVA: int = 0
    MntTotal: int = 0
    TpoImp: int | None = None
    TasaImp: int | None = None
    IVANoRec: dict | None = None
    IVAUsoComun: int | None = None
    FctProp: float | None = None
    OtrosImp: dict | None = None
    IVARetTotal: int | None = None


class GenerarLibroComprasIn(BaseModel):
    periodo: str  # "YYYY-MM"
    folio_notificacion: int = 0
    enviar: bool = True
    entradas: list[EntradaCompraIn]


class GenerarLibroComprasOut(BaseModel):
    ok: bool
    libro_id: str | None = None
    track_id: str | None = None
    estado_sii: str | None = None
    total_dtes: int | None = None
    resumen: dict | None = None
    error: str | None = None


@router.post("/compras/generar", response_model=GenerarLibroComprasOut)
def generar_libro_compras(
    body: GenerarLibroComprasIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Genera, firma y envía el Libro de Compras para un periodo con datos manuales."""
    try:
        if not re.match(r"^\d{4}-\d{2}$", body.periodo):
            raise HTTPException(400, "Formato de periodo inválido. Use YYYY-MM")

        servicio, empresa = _get_servicio_libros(tenant)

        entradas = [e.model_dump() for e in body.entradas]

        resultado = servicio.generar_libro_compras(
            db=tenant.db,
            periodo=body.periodo,
            entradas=entradas,
            folio_notificacion=body.folio_notificacion,
            enviar=body.enviar,
        )

        return GenerarLibroComprasOut(
            ok=resultado.get("ok", False),
            libro_id=resultado.get("libro_id"),
            track_id=resultado.get("track_id"),
            estado_sii=resultado.get("estado_sii"),
            total_dtes=resultado.get("total_dtes"),
            resumen=resultado.get("resumen"),
            error=resultado.get("error"),
        )
    finally:
        tenant.close()


# ── Endpoints: Libro de Guías de Despacho ──


@router.post("/guias/generar", response_model=GenerarLibroGuiasOut)
def generar_libro_guias(
    body: GenerarLibroGuiasIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Genera, firma y envía el Libro de Guías de Despacho para un periodo."""
    try:
        if not re.match(r"^\d{4}-\d{2}$", body.periodo):
            raise HTTPException(400, "Formato de periodo inválido. Use YYYY-MM")

        servicio, empresa = _get_servicio_libros(tenant)

        resultado = servicio.generar_libro_guias(
            db=tenant.db,
            periodo=body.periodo,
            folio_notificacion=body.folio_notificacion,
            enviar=body.enviar,
            guias_anuladas=body.guias_anuladas or None,
            folios_filter=body.folios,
        )

        return GenerarLibroGuiasOut(
            ok=resultado.get("ok", False),
            libro_id=resultado.get("libro_id"),
            track_id=resultado.get("track_id"),
            estado_sii=resultado.get("estado_sii"),
            total_dtes=resultado.get("total_dtes"),
            resumen=resultado.get("resumen"),
            error=resultado.get("error"),
        )
    finally:
        tenant.close()


@router.get("/guias/{periodo}", response_model=LibroOut | None)
def obtener_libro_guias(
    periodo: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Obtiene el libro de guías de despacho generado para un periodo."""
    try:
        if not re.match(r"^\d{4}-\d{2}$", periodo):
            raise HTTPException(400, "Formato de periodo inválido. Use YYYY-MM")

        libro = (
            tenant.db.query(LibroGenerado)
            .filter(
                LibroGenerado.empresa_id == tenant.empresa_id,
                LibroGenerado.tipo_libro == "GUIA",
                LibroGenerado.periodo == periodo,
            )
            .first()
        )

        if not libro:
            raise HTTPException(404, f"No hay libro de guías para {periodo}")

        resumen = None
        if libro.resumen_json:
            try:
                resumen = json.loads(libro.resumen_json)
            except json.JSONDecodeError:
                pass

        return LibroOut(
            id=libro.id,
            tipo_libro=libro.tipo_libro,
            periodo=libro.periodo,
            track_id=libro.track_id,
            estado_sii=libro.estado_sii,
            resumen=resumen,
            created_at=libro.created_at.isoformat() if libro.created_at else "",
        )
    finally:
        tenant.close()


@router.get("/guias", response_model=list[LibroOut])
def listar_libros_guias(tenant: TenantContext = Depends(get_tenant)):
    """Lista todos los libros de guías de despacho generados."""
    try:
        libros = (
            tenant.db.query(LibroGenerado)
            .filter(
                LibroGenerado.empresa_id == tenant.empresa_id,
                LibroGenerado.tipo_libro == "GUIA",
            )
            .order_by(LibroGenerado.periodo.desc())
            .all()
        )

        result = []
        for libro in libros:
            resumen = None
            if libro.resumen_json:
                try:
                    resumen = json.loads(libro.resumen_json)
                except json.JSONDecodeError:
                    pass

            result.append(LibroOut(
                id=libro.id,
                tipo_libro=libro.tipo_libro,
                periodo=libro.periodo,
                track_id=libro.track_id,
                estado_sii=libro.estado_sii,
                resumen=resumen,
                created_at=libro.created_at.isoformat() if libro.created_at else "",
            ))

        return result
    finally:
        tenant.close()
