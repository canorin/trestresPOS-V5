"""Crumb API — FastAPI application.

Sistema multi-tenant con aislamiento total por empresa y ambiente (certificacion/produccion).
"""
import logging
import os
import secrets
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from crumbpos.db.multi_tenant import init_multi_tenant, ensure_super_admin
from crumbpos.api.scheduler import iniciar_scheduler, detener_scheduler
from crumbpos.api.routers import (
    auth, articulos, ventas, facturacion, folios, empresas, clientes,
    libros, sii_estado, envio_receptor, rcof, sesion_caja, reportes,
    dashboard, sucursales, usuarios, cajas, inventario, sync, certificacion,
    baja_empresas, consola_cliente, pos, datos_personales, dtes_recibidos,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# ── Middleware de rate limit por IP ───────────────────────────────
from crumbpos.core.security.rate_limit import ip_limiter

# Rutas exentas: health checks, assets estáticos, OpenAPI schema.
_RL_EXENTAS = frozenset({"/health", "/", "/openapi.json"})


class IPRateLimitMiddleware(BaseHTTPMiddleware):
    """Limita solicitudes por IP: 600 req/min (protección DoS basal).

    No afecta rutas de monitoreo ni archivos estáticos.
    Para límites más finos por endpoint ver dependencies.py.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _RL_EXENTAS or path.startswith("/static"):
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        allowed, retry_after = ip_limiter.is_allowed(ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Demasiadas solicitudes. Reintenta en {retry_after}s",
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": "600",
                    "X-RateLimit-Window": "60",
                },
            )
        return await call_next(request)

# Super admin credentials (from env or defaults for dev).
# En producción: SUPER_ADMIN_PASSWORD es obligatoria y NO puede ser el default.
SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "matias@trestres.cl")
_SUPER_ADMIN_DEFAULT = "admin123"
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", _SUPER_ADMIN_DEFAULT)
SUPER_ADMIN_NOMBRE = os.getenv("SUPER_ADMIN_NOMBRE", "Matías Bañados")

# Fail-fast: bloquear arranque en producción si la password del super admin
# es el default conocido.
if (
    SUPER_ADMIN_PASSWORD == _SUPER_ADMIN_DEFAULT
    and os.getenv("CRUMBPOS_ENV", "").lower() == "production"
):
    raise RuntimeError(
        "SUPER_ADMIN_PASSWORD no configurada en producción. "
        "Configurar con valor de alta entropía: "
        "`export SUPER_ADMIN_PASSWORD=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')`."
    )

if SUPER_ADMIN_PASSWORD == _SUPER_ADMIN_DEFAULT:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "⚠️  SUPER_ADMIN_PASSWORD usa valor por defecto. NO USAR EN PRODUCCIÓN."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa sistema multi-tenant al arrancar."""
    # 1. Inicializar master.db
    init_multi_tenant()

    # 2. Asegurar super admin existe
    import bcrypt
    password_hash = bcrypt.hashpw(
        SUPER_ADMIN_PASSWORD.encode(), bcrypt.gensalt(),
    ).decode()
    ensure_super_admin(
        email=SUPER_ADMIN_EMAIL,
        password_hash=password_hash,
        nombre=SUPER_ADMIN_NOMBRE,
    )
    logger.info("Super admin ready: %s", SUPER_ADMIN_EMAIL)

    # 3. Iniciar scheduler RCOF diario (23:00)
    iniciar_scheduler()

    yield

    # Shutdown: detener scheduler
    detener_scheduler()


app = FastAPI(
    title="Crumb API",
    version="0.2.0",
    description="API multi-tenant para punto de venta con facturación electrónica SII Chile",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# ── D3: handler global para excepciones no controladas ────────────────────────
# Cualquier excepción que no sea HTTPException y que no sea capturada por los
# handlers de ruta llega aquí. Se loguea el traceback completo server-side y
# se devuelve un error_id al cliente — nunca el detalle interno.
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, StarletteHTTPException):
        # HTTPExceptions son respuestas intencionadas — no las interceptamos.
        raise exc
    error_id = secrets.token_hex(8)
    logger.error(
        "Excepción no controlada [error_id=%s] %s %s: %s\n%s",
        error_id, request.method, request.url.path,
        exc, traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": f"Error interno del servidor (error_id={error_id}). Consulte los logs."},
    )


# Archivos estáticos (logo, assets)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# CORS: la lista de orígenes permitidos viene de la env var
# CRUMBPOS_ALLOWED_ORIGINS como CSV. En desarrollo, default incluye localhost.
# En producción, configurar SOLO los dominios reales.
# Usar `"*"` con `allow_credentials=True` es inseguro (habilita CSRF
# cross-origin con credenciales) — bloqueado explícitamente abajo.
_DEV_ORIGINS = "http://localhost:5173,http://localhost:5174,http://localhost:3000,http://localhost:8000"
_origins_env = os.getenv("CRUMBPOS_ALLOWED_ORIGINS", _DEV_ORIGINS)
_allowed_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]

if "*" in _allowed_origins:
    if os.getenv("CRUMBPOS_ENV", "").lower() == "production":
        raise RuntimeError(
            "CRUMBPOS_ALLOWED_ORIGINS no puede contener '*' en producción "
            "(habilita CSRF con credenciales). Configurar dominios explícitos."
        )
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "⚠️  CORS permite '*' — habilita CSRF cross-origin. NO USAR EN PRODUCCIÓN."
    )

app.add_middleware(IPRateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Empresa-Rut", "X-Sucursal-Id"],
)

# Routers
app.include_router(auth.router)
app.include_router(empresas.router)
app.include_router(articulos.router)
app.include_router(ventas.router)
app.include_router(facturacion.router)
app.include_router(folios.router)
app.include_router(clientes.router)
app.include_router(libros.router)
app.include_router(sii_estado.router)
app.include_router(envio_receptor.router)
app.include_router(rcof.router)
app.include_router(sesion_caja.router)
app.include_router(reportes.router)
app.include_router(dashboard.router)
app.include_router(sucursales.router)
app.include_router(usuarios.router)
app.include_router(cajas.router)
app.include_router(inventario.router)
app.include_router(sync.router)
app.include_router(certificacion.router)
app.include_router(baja_empresas.router)
app.include_router(pos.router)
app.include_router(datos_personales.router)
app.include_router(dtes_recibidos.router)
# ── consola_cliente: include_router se hace AL FINAL del archivo ──
# Sus rutas son /{empresa_rut}/login y /{empresa_rut}/dashboard con path
# param regex-validado. Debe registrarse después de todos los @app.get
# fijos (/admin/*, /docs, /factura, /folios, /certificacion, /health, /)
# porque FastAPI/Starlette matchea por orden de registro: una URL como
# /admin/login matchearía primero /{empresa_rut}/login y — aunque el
# regex no valide — FastAPI devuelve 422 en vez de seguir buscando.
# Ver: `app.include_router(consola_cliente.router)` al final del archivo.


# ── Swagger UI custom con logo Crumb ──────────────────────────

SWAGGER_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Crumb API</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: #fafafa; }
        .swagger-ui .topbar { display: none !important; }
        .ttpos-header {
            background: #000;
            padding: 0 24px;
            height: 83px;
            display: flex;
            align-items: center;
            position: sticky;
            top: 0;
            z-index: 1000;
        }
        .ttpos-header img {
            height: 83px;
            width: auto;
            display: block;
        }
        .ttpos-header .version {
            color: #f40d63;
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            font-size: 12px;
            font-weight: 600;
            background: rgba(244, 13, 99, 0.15);
            padding: 3px 10px;
            border-radius: 12px;
            margin-left: 16px;
        }
        .ttpos-header .ambiente {
            color: #4CAF50;
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            font-size: 11px;
            font-weight: 600;
            background: rgba(76, 175, 80, 0.15);
            padding: 3px 10px;
            border-radius: 12px;
            margin-left: 8px;
        }
        .swagger-ui .info { margin: 20px 0 !important; }
        .swagger-ui .info hgroup.main h2.title { font-size: 0 !important; }
    </style>
</head>
<body>
    <div class="ttpos-header">
        <img src="/static/logo-light.svg" alt="Crumb">
        <span class="version">API v0.2.0</span>
        <span class="ambiente">Multi-Tenant</span>
    </div>
    <div id="swagger-ui"></div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
        SwaggerUIBundle({
            url: "/openapi.json",
            dom_id: "#swagger-ui",
            presets: [
                SwaggerUIBundle.presets.apis,
                SwaggerUIBundle.SwaggerUIStandalonePreset
            ],
            layout: "BaseLayout",
            defaultModelsExpandDepth: -1,
            docExpansion: "list",
        });
    </script>
</body>
</html>
"""


@app.get("/docs", include_in_schema=False)
async def custom_docs():
    """Swagger UI con branding Crumb."""
    return HTMLResponse(SWAGGER_HTML)


@app.get("/factura", include_in_schema=False)
async def factura_page():
    """Frontend de emisión de facturas."""
    html_path = STATIC_DIR / "factura.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/folios", include_in_schema=False)
async def folios_page():
    """Panel de gestión de folios CAF."""
    html_path = STATIC_DIR / "folios.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/certificacion", include_in_schema=False)
async def certificacion_wizard_page():
    """Wizard de certificación SII — solo super admin."""
    html_path = STATIC_DIR / "certificacion" / "wizard.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ── Consola Super Admin ────────────────────────────────────────

def _admin_page(filename: str) -> HTMLResponse:
    html_path = STATIC_DIR / "admin" / filename
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/admin/login", include_in_schema=False)
async def admin_login_page():
    """Welcome page del super admin."""
    return _admin_page("login.html")


@app.get("/admin", include_in_schema=False)
async def admin_main_page():
    """Main page del super admin — grid de opciones."""
    return _admin_page("main.html")


@app.get("/admin/clientes", include_in_schema=False)
async def admin_clientes_page():
    """Lista de clientes del super admin."""
    return _admin_page("clientes.html")


@app.get("/admin/clientes/nuevo", include_in_schema=False)
async def admin_cliente_nuevo_page():
    """Formulario de alta de cliente."""
    return _admin_page("cliente_nuevo.html")


@app.get("/")
def root():
    return {
        "app": "Crumb API",
        "version": "0.2.0",
        "docs": "/docs",
        "admin": "/admin/login",
        "arquitectura": "multi-tenant",
        "auth": "POST /api/auth/login",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Router catch-all por RUT empresa (registrado AL FINAL) ────────
# Ver la nota cerca de app.include_router(baja_empresas.router): este
# router usa path param /{empresa_rut} y debe ir después de todas las
# rutas con prefijo fijo para que el matching por orden no convierta
# segmentos como "admin" o "docs" en RUTs inválidos (422).
app.include_router(consola_cliente.router)
