"""CRUD de artículos y familias — multi-tenant."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from crumbpos.core.roles import puede_gestionar_sucursal
from crumbpos.db.models import (
    Familia, FamiliaSucursal, Articulo, ArticuloSucursal,
    PrecioHistorial,
)
from crumbpos.api.dependencies import get_tenant, require_role, TenantContext
from crumbpos.api.schemas import (
    FamiliaCreate, FamiliaOut, FamiliaSucursalUpdate,
    ArticuloCreate, ArticuloUpdate, ArticuloOut,
    ArticuloSucursalUpdate, ArticuloSucursalOut,
)

router = APIRouter(prefix="/api/articulos", tags=["articulos"])


# ─── FAMILIAS ───

@router.get("/familias", response_model=list[FamiliaOut])
def listar_familias(tenant: TenantContext = Depends(get_tenant)):
    try:
        return tenant.db.query(Familia).filter(
            Familia.empresa_id == tenant.empresa_id,
        ).order_by(Familia.orden, Familia.nombre).all()
    finally:
        tenant.close()


@router.post("/familias", response_model=FamiliaOut, status_code=201)
def crear_familia(
    body: FamiliaCreate,
    tenant: TenantContext = Depends(get_tenant),
):
    try:
        if not puede_gestionar_sucursal(tenant.user.rol):
            raise HTTPException(403, "No tiene permisos para crear familias")
        familia = Familia(empresa_id=tenant.empresa_id, **body.model_dump())
        tenant.db.add(familia)
        tenant.db.commit()
        tenant.db.refresh(familia)
        return familia
    finally:
        tenant.close()


@router.put("/familias/{familia_id}/sucursal/{sucursal_id}")
def config_familia_sucursal(
    familia_id: str,
    sucursal_id: str,
    body: FamiliaSucursalUpdate,
    tenant: TenantContext = Depends(get_tenant),
):
    """Activar/desactivar una familia en una sucursal."""
    try:
        db = tenant.db
        fs = db.query(FamiliaSucursal).filter_by(
            familia_id=familia_id, sucursal_id=sucursal_id,
        ).first()

        if fs:
            fs.activa = body.activa
            fs.orden = body.orden
        else:
            fs = FamiliaSucursal(
                familia_id=familia_id,
                sucursal_id=sucursal_id,
                activa=body.activa,
                orden=body.orden,
            )
            db.add(fs)

        db.commit()
        return {"ok": True}
    finally:
        tenant.close()


# ─── ARTÍCULOS ───

@router.get("/", response_model=list[ArticuloOut])
def listar_articulos(
    familia_id: str | None = None,
    activo: bool | None = None,
    q: str | None = Query(None, description="Buscar por nombre o SKU"),
    tenant: TenantContext = Depends(get_tenant),
):
    try:
        db = tenant.db
        query = db.query(Articulo).filter(Articulo.empresa_id == tenant.empresa_id)
        if familia_id:
            query = query.filter(Articulo.familia_id == familia_id)
        if activo is not None:
            query = query.filter(Articulo.activo == activo)
        if q:
            query = query.filter(
                (Articulo.nombre.ilike(f"%{q}%")) | (Articulo.sku.ilike(f"%{q}%"))
            )
        return query.order_by(Articulo.nombre).limit(100).all()
    finally:
        tenant.close()


@router.post("/", response_model=ArticuloOut, status_code=201)
def crear_articulo(
    body: ArticuloCreate,
    tenant: TenantContext = Depends(get_tenant),
):
    try:
        if not puede_gestionar_sucursal(tenant.user.rol):
            raise HTTPException(403, "No tiene permisos para crear artículos")
        articulo = Articulo(empresa_id=tenant.empresa_id, **body.model_dump())
        tenant.db.add(articulo)
        tenant.db.commit()
        tenant.db.refresh(articulo)
        return articulo
    finally:
        tenant.close()


@router.put("/{articulo_id}", response_model=ArticuloOut)
def actualizar_articulo(
    articulo_id: str,
    body: ArticuloUpdate,
    tenant: TenantContext = Depends(get_tenant),
):
    try:
        db = tenant.db
        art = db.query(Articulo).filter(
            Articulo.id == articulo_id,
            Articulo.empresa_id == tenant.empresa_id,
        ).first()
        if not art:
            raise HTTPException(404, "Artículo no encontrado")

        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(art, field, value)

        db.commit()
        db.refresh(art)
        return art
    finally:
        tenant.close()


# ─── ARTÍCULO × SUCURSAL ───

@router.get("/sucursal/{sucursal_id}", response_model=list[ArticuloSucursalOut])
def listar_articulos_sucursal(
    sucursal_id: str,
    familia_id: str | None = None,
    tenant: TenantContext = Depends(get_tenant),
):
    """Artículos disponibles en una sucursal con precio efectivo."""
    try:
        db = tenant.db
        query = (
            db.query(Articulo, ArticuloSucursal)
            .outerjoin(ArticuloSucursal, (
                (ArticuloSucursal.articulo_id == Articulo.id)
                & (ArticuloSucursal.sucursal_id == sucursal_id)
            ))
            .join(Familia, Articulo.familia_id == Familia.id)
            .join(FamiliaSucursal, (
                (FamiliaSucursal.familia_id == Familia.id)
                & (FamiliaSucursal.sucursal_id == sucursal_id)
                & (FamiliaSucursal.activa == True)
            ))
            .filter(
                Articulo.empresa_id == tenant.empresa_id,
                Articulo.activo == True,
                Familia.activa == True,
            )
        )
        if familia_id:
            query = query.filter(Articulo.familia_id == familia_id)

        results = []
        for art, art_suc in query.order_by(Articulo.nombre).all():
            activo_suc = art_suc.activo if art_suc else False
            precio_suc = art_suc.precio_venta if art_suc else None
            results.append(ArticuloSucursalOut(
                articulo=ArticuloOut.model_validate(art),
                activo=activo_suc,
                precio_venta=precio_suc,
                precio_efectivo=precio_suc if precio_suc is not None else art.precio_default,
            ))
        return results
    finally:
        tenant.close()


@router.put("/{articulo_id}/sucursal/{sucursal_id}")
def config_articulo_sucursal(
    articulo_id: str,
    sucursal_id: str,
    body: ArticuloSucursalUpdate,
    tenant: TenantContext = Depends(get_tenant),
):
    """Activar/desactivar artículo en sucursal y setear precio."""
    try:
        db = tenant.db
        art = db.query(Articulo).filter(
            Articulo.id == articulo_id, Articulo.empresa_id == tenant.empresa_id,
        ).first()
        if not art:
            raise HTTPException(404, "Artículo no encontrado")

        art_suc = db.query(ArticuloSucursal).filter_by(
            articulo_id=articulo_id, sucursal_id=sucursal_id,
        ).first()

        precio_anterior = None
        if art_suc:
            precio_anterior = art_suc.precio_venta
            art_suc.activo = body.activo
            if body.precio_venta is not None:
                art_suc.precio_venta = body.precio_venta
            if body.costo is not None:
                art_suc.costo = body.costo
        else:
            art_suc = ArticuloSucursal(
                articulo_id=articulo_id,
                sucursal_id=sucursal_id,
                activo=body.activo,
                precio_venta=body.precio_venta,
                costo=body.costo,
            )
            db.add(art_suc)

        if body.precio_venta is not None and body.precio_venta != precio_anterior:
            historial = PrecioHistorial(
                articulo_id=articulo_id,
                sucursal_id=sucursal_id,
                precio_anterior=precio_anterior or art.precio_default,
                precio_nuevo=body.precio_venta,
                usuario_id=tenant.user.id,
            )
            db.add(historial)

        db.commit()
        return {"ok": True}
    finally:
        tenant.close()
