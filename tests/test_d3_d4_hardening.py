"""Tests para D3/D4 — stack traces + tamaño de upload cert .pfx.

D3:
- raise_safe_500 loguea traceback y lanza HTTPException(500) con error_id
- error_id presente en la respuesta (nunca el mensaje interno)
- Handler global captura excepciones no controladas y retorna error_id

D4:
- Upload .pfx > 100 KB → 400
- Upload archivo que no es .pfx/.p12 → 400
- Upload vacío → 400
"""
from __future__ import annotations

import io
import logging
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from crumbpos.api.app import app
from crumbpos.api.dependencies import get_current_user, get_tenant, TenantContext
from crumbpos.api.error_utils import raise_safe_500
from crumbpos.db.multi_tenant import UsuarioAuth


# ──────────────────────────────────────────────────────────────────────────────
# D3 — raise_safe_500
# ──────────────────────────────────────────────────────────────────────────────

def test_raise_safe_500_lanza_http_exception():
    """raise_safe_500 siempre lanza HTTPException con status_code 500."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        raise_safe_500(ValueError("detalle interno secreto"), "operación test")

    assert exc_info.value.status_code == 500


def test_raise_safe_500_no_expone_detalle_interno():
    """El detail de la HTTPException no incluye el mensaje de la excepción original."""
    from fastapi import HTTPException

    mensaje_secreto = "tabla_usuarios constraint UNIQUE failed"
    with pytest.raises(HTTPException) as exc_info:
        raise_safe_500(ValueError(mensaje_secreto), "crear usuario")

    detail = str(exc_info.value.detail)
    assert mensaje_secreto not in detail, (
        "El detalle interno NO debe llegar al cliente"
    )


def test_raise_safe_500_incluye_error_id():
    """El detail incluye un error_id alfanumérico para correlación con logs."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        raise_safe_500(RuntimeError("boom"), "test")

    detail = str(exc_info.value.detail)
    assert "error_id=" in detail


def test_raise_safe_500_loguea_traceback(caplog):
    """raise_safe_500 loguea el traceback completo a nivel ERROR."""
    from fastapi import HTTPException

    with caplog.at_level(logging.ERROR, logger="crumbpos.api.error_utils"):
        with pytest.raises(HTTPException):
            try:
                raise ValueError("mensaje secreto de prueba")
            except ValueError as e:
                raise_safe_500(e, "contexto test")

    # Verificar que el log incluye el mensaje original (para que el admin pueda correlacionar)
    assert any("mensaje secreto de prueba" in record.message for record in caplog.records), (
        "El mensaje original debe estar en los logs del servidor"
    )


def test_raise_safe_500_acepta_logger_externo(caplog):
    """raise_safe_500 usa el logger pasado como parámetro si se provee."""
    from fastapi import HTTPException

    custom_logger = logging.getLogger("test.custom")
    with caplog.at_level(logging.ERROR, logger="test.custom"):
        with pytest.raises(HTTPException):
            try:
                raise RuntimeError("error con logger externo")
            except RuntimeError as e:
                raise_safe_500(e, "accion", log=custom_logger)

    assert any("error con logger externo" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────────────
# D3 — Handler global: excepciones no controladas
# ──────────────────────────────────────────────────────────────────────────────

def test_handler_global_captura_excepcion_no_controlada():
    """Excepción no controlada en una ruta devuelve 500 con error_id (no traceback)."""
    from fastapi import APIRouter

    # Agregar ruta de prueba que lanza una excepción pura (no HTTPException)
    test_router = APIRouter()

    @test_router.get("/test-unhandled-exception")
    def _ruta_que_explota():
        raise RuntimeError("este traceback NO debe llegar al cliente")

    app.include_router(test_router)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/test-unhandled-exception")

    assert resp.status_code == 500
    data = resp.json()
    # No debe contener el mensaje interno
    assert "traceback" not in str(data).lower()
    assert "ruta_que_explota" not in str(data)
    assert "este traceback" not in str(data)
    # Debe contener error_id
    assert "error_id=" in data.get("detail", "")


def test_handler_global_no_intercepta_http_exception():
    """HTTPException(404) del router no es transformada por el handler global."""
    from fastapi import APIRouter, HTTPException as FastHTTPException

    test_router = APIRouter()

    @test_router.get("/test-404-route")
    def _ruta_404():
        raise FastHTTPException(404, "recurso no encontrado")

    app.include_router(test_router)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/test-404-route")

    # Debe llegar el 404 original, no ser transformado en 500
    assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# D4 — Upload cert .pfx tamaño y tipo
# ──────────────────────────────────────────────────────────────────────────────

def _mock_tenant(rut: str = "76354771-K") -> TenantContext:
    tenant = MagicMock(spec=TenantContext)
    tenant.empresa_rut = rut
    tenant.empresa_id = str(uuid.uuid4())
    tenant.ambiente = "certificacion"
    user = MagicMock(spec=UsuarioAuth)
    user.id = str(uuid.uuid4())
    user.rol = "master_client"
    user.empresa_rut = rut
    tenant.user = user
    tenant.db = MagicMock()
    return tenant


def test_upload_pfx_archivo_demasiado_grande():
    """PFX > 100 KB → 400."""
    from crumbpos.api.dependencies import get_tenant

    tenant = _mock_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    # 101 KB de datos aleatorios
    datos_grandes = b"A" * (101 * 1024)
    resp = client.post(
        "/api/empresas/mi-empresa/certificado",
        data={"password": "pw123", "rut_firmante": "12345678-9"},
        files={"archivo": ("cert.pfx", io.BytesIO(datos_grandes), "application/octet-stream")},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 400
    assert "100" in resp.json().get("detail", "")


def test_upload_pfx_extension_invalida():
    """Archivo con extensión .exe → 400."""
    from crumbpos.api.dependencies import get_tenant

    tenant = _mock_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/empresas/mi-empresa/certificado",
        data={"password": "pw123", "rut_firmante": "12345678-9"},
        files={"archivo": ("virus.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 400
    detail = resp.json().get("detail", "").lower()
    assert "pfx" in detail or "p12" in detail


def test_upload_pfx_archivo_vacio():
    """Archivo vacío → 400."""
    from crumbpos.api.dependencies import get_tenant

    tenant = _mock_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/empresas/mi-empresa/certificado",
        data={"password": "pw123", "rut_firmante": "12345678-9"},
        files={"archivo": ("cert.pfx", io.BytesIO(b""), "application/octet-stream")},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 400


def test_upload_pfx_tamano_valido_llega_a_validacion_crypto():
    """PFX de tamaño válido pasa la validación de tamaño y llega a la de crypto."""
    from crumbpos.api.dependencies import get_tenant

    tenant = _mock_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    # 5 KB — dentro del límite, pero contenido no es PFX real → 400 de crypto
    datos_validos = b"no_es_un_pfx_real" * 300  # ~5 KB
    resp = client.post(
        "/api/empresas/mi-empresa/certificado",
        data={"password": "pw123", "rut_firmante": "12345678-9"},
        files={"archivo": ("cert.pfx", io.BytesIO(datos_validos), "application/octet-stream")},
    )
    app.dependency_overrides.pop(get_tenant, None)

    # No debe ser 400 por tamaño (debería ser 400 por error crypto)
    detail = resp.json().get("detail", "").lower()
    assert "grande" not in detail, "No debe rechazarse por tamaño"
    # Sí debe ser 400 (por contenido inválido de PFX)
    assert resp.status_code == 400
