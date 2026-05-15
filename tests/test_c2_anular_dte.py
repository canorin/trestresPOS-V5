"""Tests para C2 — endpoint POST /api/facturacion/{folio}/anular.

Cubre:
- 404 si DTE no existe
- 422 si tipo_dte no es 33/34
- 422 si DTE no tiene XML firmado
- Construye FacturaRequest T61 con ítems del original y CodRef=1
- Devuelve folio_nc en respuesta exitosa
- FoliosAgotadosError → 409
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from crumbpos.api.app import app
from crumbpos.api.dependencies import get_tenant, TenantContext
from crumbpos.db.models import DteEmitido, Empresa


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures helpers
# ──────────────────────────────────────────────────────────────────────────────

def _xml_sobre_minimo(receptor_rut: str = "76354771-K") -> bytes:
    """XML de EnvioDTE mínimo con un ítem para probar _extraer_items."""
    return (
        b'<?xml version="1.0" encoding="ISO-8859-1"?>'
        b'<EnvioDTE xmlns="http://www.sii.cl/SiiDte">'
        b'<SetDTE><DTE><Documento><Detalle>'
        b'<NroLinDet>1</NroLinDet>'
        b'<NmbItem>Producto A</NmbItem>'
        b'<QtyItem>2</QtyItem>'
        b'<PrcItem>5000</PrcItem>'
        b'<MontoItem>10000</MontoItem>'
        b'</Detalle></Documento></DTE></SetDTE>'
        b'</EnvioDTE>'
    )


def _make_empresa(**kwargs) -> Empresa:
    defaults = {
        "id": str(uuid.uuid4()),
        "rut": "76354771-K",
        "razon_social": "Test SA",
        "giro": "Comercio",
        "direccion": "Calle 1",
        "comuna": "Santiago",
        "ciudad": "Santiago",
        "ambiente_sii": "certificacion",
        "fecha_resolucion": "2020-01-01",
        "numero_resolucion": 0,
        "cert_rut_firmante": "12345678-9",
    }
    defaults.update(kwargs)
    emp = MagicMock(spec=Empresa)
    for k, v in defaults.items():
        setattr(emp, k, v)
    return emp


def _make_dte(empresa_id: str, tipo_dte: int = 33, folio: int = 100,
              xml_firmado: str | None = None) -> DteEmitido:
    dte = MagicMock(spec=DteEmitido)
    dte.empresa_id = empresa_id
    dte.tipo_dte = tipo_dte
    dte.folio = folio
    dte.receptor_rut = "76354771-K"
    dte.receptor_razon = "Cliente Test"
    dte.fecha_emision = date(2025, 1, 15)
    dte.xml_firmado = xml_firmado
    return dte


def _tenant_mock(empresa: Empresa) -> TenantContext:
    tenant = MagicMock(spec=TenantContext)
    tenant.empresa_rut = empresa.rut
    tenant.ambiente = empresa.ambiente_sii
    tenant.sucursal_id = None
    tenant.user = MagicMock(id="usr-1")
    db = MagicMock()
    # db.query(...).filter(...).first() → empresa
    db.query.return_value.filter.return_value.first.return_value = empresa
    tenant.db = db
    tenant.close = MagicMock()
    return tenant


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

def test_anular_tipo_dte_no_anulable():
    """Tipos distintos de T33/T34 devuelven 422."""
    emp = _make_empresa()
    tenant = _tenant_mock(emp)

    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/facturacion/1/anular",
        json={"tipo_dte": 39, "motivo": "test"},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 422


def test_anular_dte_no_encontrado():
    """404 si el DTE no existe en la DB."""
    emp = _make_empresa()
    tenant = _tenant_mock(emp)

    # Primera query: empresa OK; segunda query (DteEmitido): None
    call_count = [0]
    def fake_first():
        call_count[0] += 1
        if call_count[0] == 1:
            return emp  # empresa
        return None  # DteEmitido

    tenant.db.query.return_value.filter.return_value.first = fake_first

    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/facturacion/100/anular",
        json={"tipo_dte": 33, "motivo": "test"},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 404


def test_anular_dte_sin_xml_firmado():
    """422 si el DTE existe pero no tiene XML firmado."""
    emp = _make_empresa()
    tenant = _tenant_mock(emp)

    dte = _make_dte(emp.id, xml_firmado=None)

    call_count = [0]
    def fake_first():
        call_count[0] += 1
        if call_count[0] == 1:
            return emp
        return dte

    tenant.db.query.return_value.filter.return_value.first = fake_first

    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/facturacion/100/anular",
        json={"tipo_dte": 33},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 422
    assert "xml firmado" in resp.json()["detail"].lower()


def test_anular_emite_nc_t61():
    """Happy path: construye T61 con ítems del original y devuelve folio_nc."""
    emp = _make_empresa()
    tenant = _tenant_mock(emp)

    xml_b64 = base64.b64encode(_xml_sobre_minimo()).decode()
    dte = _make_dte(emp.id, tipo_dte=33, folio=100, xml_firmado=xml_b64)

    call_count = [0]
    def fake_first():
        call_count[0] += 1
        if call_count[0] == 1:
            return emp
        return dte

    tenant.db.query.return_value.filter.return_value.first = fake_first

    resultado_mock = MagicMock()
    resultado_mock.ok = True
    resultado_mock.folio = 200
    resultado_mock.track_id = "TRACK-NC"
    resultado_mock.monto_total = 10000
    resultado_mock.error = None
    resultado_mock.xml_firmado = None

    # Capturar qué FacturaRequest se construye
    req_capturado = []

    def fake_emitir_factura(req, enviar_sii=True, session=None, empresa_id=None):
        req_capturado.append(req)
        return resultado_mock

    servicio_mock = MagicMock()
    servicio_mock.emitir_factura = fake_emitir_factura

    with patch(
        "crumbpos.api.routers.facturacion._get_servicio",
        return_value=(servicio_mock, emp),
    ), patch(
        "crumbpos.api.routers.facturacion._persist_dte_emitido",
        return_value=MagicMock(),
    ):
        app.dependency_overrides[get_tenant] = lambda: tenant
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/facturacion/100/anular",
            json={"tipo_dte": 33, "motivo": "Error en factura"},
        )
        app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["folio_nc"] == 200
    assert data["track_id"] == "TRACK-NC"

    # Verificar que el FacturaRequest es T61 con CodRef=1
    assert len(req_capturado) == 1
    req = req_capturado[0]
    assert req.tipo_dte == 61
    assert req.referencias is not None and len(req.referencias) == 1
    ref = req.referencias[0]
    assert ref["tipo_doc"] == 33
    assert ref["folio"] == 100
    assert ref["cod_ref"] == 1
    assert ref["razon"] == "Error en factura"
    # Ítems copiados del original
    assert len(req.items) == 1
    assert req.items[0]["nombre"] == "Producto A"


def test_anular_folios_agotados_retorna_409():
    """FoliosAgotadosError → 409 con error='folios_agotados'."""
    from crumbpos.core.caf.caf_manager_db import FoliosAgotadosError

    emp = _make_empresa()
    tenant = _tenant_mock(emp)

    xml_b64 = base64.b64encode(_xml_sobre_minimo()).decode()
    dte = _make_dte(emp.id, tipo_dte=33, folio=100, xml_firmado=xml_b64)

    call_count = [0]
    def fake_first():
        call_count[0] += 1
        if call_count[0] == 1:
            return emp
        return dte

    tenant.db.query.return_value.filter.return_value.first = fake_first

    servicio_mock = MagicMock()
    servicio_mock.emitir_factura.side_effect = FoliosAgotadosError(61, None)

    with patch(
        "crumbpos.api.routers.facturacion._get_servicio",
        return_value=(servicio_mock, emp),
    ):
        app.dependency_overrides[get_tenant] = lambda: tenant
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/facturacion/100/anular",
            json={"tipo_dte": 33},
        )
        app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "folios_agotados"


def test_anular_t34_exenta():
    """T34 también es anulable — comprueba que no se rechaza en validación."""
    emp = _make_empresa()
    tenant = _tenant_mock(emp)

    xml_b64 = base64.b64encode(_xml_sobre_minimo()).decode()
    dte = _make_dte(emp.id, tipo_dte=34, folio=50, xml_firmado=xml_b64)

    call_count = [0]
    def fake_first():
        call_count[0] += 1
        if call_count[0] == 1:
            return emp
        return dte

    tenant.db.query.return_value.filter.return_value.first = fake_first

    resultado_mock = MagicMock()
    resultado_mock.ok = True
    resultado_mock.folio = 300
    resultado_mock.track_id = None
    resultado_mock.monto_total = 5000
    resultado_mock.error = None
    resultado_mock.xml_firmado = None

    servicio_mock = MagicMock()
    servicio_mock.emitir_factura.return_value = resultado_mock

    with patch(
        "crumbpos.api.routers.facturacion._get_servicio",
        return_value=(servicio_mock, emp),
    ), patch(
        "crumbpos.api.routers.facturacion._persist_dte_emitido",
        return_value=MagicMock(),
    ):
        app.dependency_overrides[get_tenant] = lambda: tenant
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/facturacion/50/anular",
            json={"tipo_dte": 34},
        )
        app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 200
    assert resp.json()["folio_nc"] == 300
