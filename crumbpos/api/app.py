"""trestresPOS API — FastAPI application.

Sistema multi-tenant con aislamiento total por empresa y ambiente (certificacion/produccion).
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from crumbpos.db.multi_tenant import init_multi_tenant, ensure_super_admin
from crumbpos.api.routers import (
    auth, articulos, ventas, facturacion, folios, empresas, clientes,
    libros, sii_estado, envio_receptor, rcof, sesion_caja, reportes,
    dashboard, sucursales, usuarios, cajas, inventario, sync,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Super admin credentials (from env or defaults for dev)
SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "matias@trestres.cl")
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "admin123")
SUPER_ADMIN_NOMBRE = os.getenv("SUPER_ADMIN_NOMBRE", "Matías Bañados")


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

    yield


app = FastAPI(
    title="trestresPOS API",
    version="0.2.0",
    description="API multi-tenant para punto de venta con facturación electrónica SII Chile",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# Archivos estáticos (logo, assets)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# CORS: permitir frontend local y panel web
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev (admin)
        "http://localhost:5174",  # Vite dev (POS)
        "http://localhost:3000",  # alt
        "*",  # TODO: restrict in production
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


# ── Swagger UI custom con logo trestresPOS ──────────────────────

SWAGGER_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>trestresPOS API</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: #fafafa; }
        .swagger-ui .topbar { display: none !important; }
        .ttpos-header {
            background: #000;
            padding: 0 24px;
            height: 64px;
            display: flex;
            align-items: center;
            position: sticky;
            top: 0;
            z-index: 1000;
        }
        .ttpos-header img {
            height: 64px;
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
        <img src="/static/logottpos.svg" alt="trestresPOS">
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
    """Swagger UI con branding trestresPOS."""
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


@app.get("/")
def root():
    return {
        "app": "trestresPOS API",
        "version": "0.2.0",
        "docs": "/docs",
        "arquitectura": "multi-tenant",
        "auth": "POST /api/auth/login",
    }


@app.get("/health")
def health():
    return {"status": "ok"}
