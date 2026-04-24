"""Rutas HTML de la consola master cliente — namespaced por RUT empresa.

Patrón de URL::

    /{empresa_rut}/login       → formulario de login del master cliente
    /{empresa_rut}/dashboard   → consola principal tras autenticarse

El RUT empresa actúa como "slug raíz" del tenant: todo lo que vive bajo
``/{rut}/...`` pertenece exclusivamente a esa empresa. Alinea la URL con el
namespacing del filesystem (``data/{rut}/``) y del auth (``UsuarioAuth`` con
``UNIQUE (empresa_rut, email)``), y resuelve el caso de un master con el
mismo correo en múltiples empresas: el scope viene del path.

**Evitar colisiones con rutas fijas.** FastAPI resuelve por orden de
registro y por matching de path param. Este router debe incluirse al final
de ``app.py`` y el path param ``empresa_rut`` se valida con un pattern
estricto de formato RUT chileno (``^\\d{1,8}-[0-9kK]$``); cualquier otro
segmento (``admin``, ``static``, ``api``, ``docs``, etc.) no matchea el
pattern y FastAPI continúa buscando rutas.

**Control de acceso.** Este router solo sirve las páginas HTML; la auth la
hace el JS del cliente contra ``POST /api/auth/login`` enviando
``empresa_rut`` en el body. La validación server-side de "¿esta empresa
existe?" la hacemos acá para devolver 404 antes de servir el HTML.
"""
from pathlib import Path as FsPath

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from crumbpos.db.multi_tenant import EmpresaRegistro, get_master_db
from fastapi import Depends

router = APIRouter(tags=["consola-cliente"])

# Path param con formato RUT chileno validado por FastAPI.
# Garantiza que solo entran strings como "77829149-5" o "5-k"; cualquier
# otro segmento es 422 y FastAPI continúa hacia rutas alternativas.
_RUT_PATTERN = r"^\d{1,8}-[0-9kK]$"

_STATIC_CLIENTE = FsPath(__file__).resolve().parent.parent / "static" / "cliente"


def _validar_empresa_existe(empresa_rut: str, master_db: Session) -> EmpresaRegistro:
    """404 si la empresa no está registrada; 410 si está dada de baja.

    No valida estado ``activa``/suspendida — eso lo hace el endpoint de
    login. Acá queremos que la página de login se sirva incluso para
    empresas suspendidas (para que el master entienda qué pasó), pero no
    para empresas inexistentes o eliminadas.
    """
    registro = master_db.query(EmpresaRegistro).filter(
        EmpresaRegistro.rut == empresa_rut,
    ).first()
    if not registro:
        raise HTTPException(404, f"Empresa {empresa_rut} no registrada")
    if registro.estado != "activa":
        raise HTTPException(
            410,
            f"Empresa {empresa_rut} está dada de baja (estado: "
            f"{registro.estado})",
        )
    return registro


def _servir_html(nombre_archivo: str) -> HTMLResponse:
    """Sirve un HTML estático del directorio ``static/cliente/``."""
    path = _STATIC_CLIENTE / nombre_archivo
    if not path.exists():
        raise HTTPException(
            500,
            f"Template {nombre_archivo} no encontrado en static/cliente/",
        )
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/{empresa_rut}/login", include_in_schema=False)
async def login_master_cliente(
    empresa_rut: str = Path(..., pattern=_RUT_PATTERN),
    master_db: Session = Depends(get_master_db),
):
    """Página de login del master cliente de una empresa específica."""
    _validar_empresa_existe(empresa_rut, master_db)
    return _servir_html("login.html")


@router.get("/{empresa_rut}/dashboard", include_in_schema=False)
async def dashboard_master_cliente(
    empresa_rut: str = Path(..., pattern=_RUT_PATTERN),
    master_db: Session = Depends(get_master_db),
):
    """Consola principal del master cliente — placeholder con los 6 módulos.

    La protección de sesión vive en el JS del HTML (requiere JWT válido
    con ``empresa_rut`` matcheando el path + rol adecuado). El backend
    se limita a validar que la empresa exista.
    """
    _validar_empresa_existe(empresa_rut, master_db)
    return _servir_html("dashboard.html")


@router.get("/{empresa_rut}/negocio", include_in_schema=False)
async def negocio_master_cliente(
    empresa_rut: str = Path(..., pattern=_RUT_PATTERN),
    master_db: Session = Depends(get_master_db),
):
    """Módulo "Información del negocio" — logo, identidad, dirección.

    Primer módulo de la consola del master cliente. Permite subir el
    logo que sale impreso en los DTEs, editar razón social / giro /
    ACTECO / nombre fantasía y la dirección de casa matriz. Los datos
    SII (RUT, resolución) son readonly.
    """
    _validar_empresa_existe(empresa_rut, master_db)
    return _servir_html("negocio.html")


@router.get("/{empresa_rut}/info-publica", include_in_schema=False)
async def info_publica_empresa(
    empresa_rut: str = Path(..., pattern=_RUT_PATTERN),
    master_db: Session = Depends(get_master_db),
):
    """Info mínima de la empresa para mostrar en la página de login.

    Endpoint público (sin JWT): expone solo razón social y RUT. Sirve
    para que el formulario ``/{rut}/login`` salude por nombre del negocio
    antes de que el master cliente se autentique.
    """
    reg = _validar_empresa_existe(empresa_rut, master_db)
    return {"rut": reg.rut, "razon_social": reg.razon_social}


@router.get("/{empresa_rut}/facturacion", include_in_schema=False)
async def facturacion_master_cliente(
    empresa_rut: str = Path(..., pattern=_RUT_PATTERN),
    master_db: Session = Depends(get_master_db),
):
    """Índice del módulo Facturación — CTA de emisión + tiles.

    Landing del módulo: desde acá el master cliente ve el botón para
    emitir un DTE nuevo y los sub-módulos pendientes (documentos
    emitidos, facturas recibidas, libros/RCOF).
    """
    _validar_empresa_existe(empresa_rut, master_db)
    return _servir_html("facturacion.html")


@router.get("/{empresa_rut}/facturacion/emision", include_in_schema=False)
async def facturacion_emision_master_cliente(
    empresa_rut: str = Path(..., pattern=_RUT_PATTERN),
    master_db: Session = Depends(get_master_db),
):
    """Pantalla de emisión de DTEs — factura, exenta, NC, ND.

    Porta la UI legacy de ``/factura`` al namespace del tenant. Incluye
    búsqueda y guardado de clientes, referencias para NC/ND, condiciones
    de pago, orden de compra y selector de sucursal para sobreescribir
    la dirección del emisor respecto de casa matriz.
    """
    _validar_empresa_existe(empresa_rut, master_db)
    return _servir_html("facturacion_emision.html")
