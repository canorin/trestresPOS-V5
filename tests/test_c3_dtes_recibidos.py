"""Tests para C3 — endpoints de recepción de DTEs de proveedores.

Cubre:
- POST /api/dtes-recibidos/upload persiste DteRecibido rows
- Upload sin auth → 401
- Upload con XML vacío → 400
- Upload con XML inválido → 400
- Upload duplicado → duplicados=1, recibidos=0
- GET /api/dtes-recibidos/ lista filas
- POST /api/dtes-recibidos/{id}/reclamar accion=aceptar
- POST reclamar accion=reclamar con motivo
- POST reclamar en estado final → 409
- POST reclamar con accion inválida → 422
- POST reclamar sin motivo para reclamar → 422
- GET /api/dtes-recibidos/{id}/acuse cuando no hay acuse → 404
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from crumbpos.api.app import app
from crumbpos.api.dependencies import get_tenant, TenantContext
from crumbpos.db.models import Base, DteRecibido, Empresa


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

FIXTURE_XML = (
    Path(__file__).parent / "fixtures" / "intercambio" / "ENVIO_DTE_4792140.xml"
)


def _xml_bytes() -> bytes:
    return FIXTURE_XML.read_bytes()


def _fake_tenant(empresa_rut: str = "77829149-5") -> tuple[TenantContext, Session]:
    """Crea tenant con DB in-memory y empresa sembrada."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    empresa_id = str(uuid.uuid4())
    empresa = Empresa(
        id=empresa_id,
        rut=empresa_rut,
        razon_social="Grupo Trestres SPA",
        giro="Comercio",
        direccion="Av. Principal 1",
        comuna="Santiago",
        ciudad="Santiago",
    )
    session.add(empresa)
    session.commit()

    tenant = MagicMock(spec=TenantContext)
    tenant.empresa_rut = empresa_rut
    tenant.empresa_id = empresa_id
    tenant.ambiente = "certificacion"
    tenant.db = session
    user = MagicMock()
    user.id = str(uuid.uuid4())
    user.rol = "master_client"
    user.empresa_rut = empresa_rut
    tenant.user = user
    tenant.close = MagicMock()
    return tenant, session


# ──────────────────────────────────────────────────────────────────────────────
# Autenticación
# ──────────────────────────────────────────────────────────────────────────────

def test_upload_sin_auth_retorna_401():
    """Upload sin token → 401."""
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/dtes-recibidos/upload",
        files={"archivo": ("envio.xml", io.BytesIO(b"<xml/>"), "text/xml")},
    )
    assert resp.status_code == 401


def test_listar_sin_auth_retorna_401():
    """GET sin token → 401."""
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dtes-recibidos/")
    assert resp.status_code == 401


# ──────────────────────────────────────────────────────────────────────────────
# POST /upload — validaciones
# ──────────────────────────────────────────────────────────────────────────────

def test_upload_archivo_vacio_retorna_400():
    """XML vacío → 400."""
    tenant, _ = _fake_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/dtes-recibidos/upload",
        files={"archivo": ("envio.xml", io.BytesIO(b""), "text/xml")},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 400


def test_upload_xml_invalido_retorna_400():
    """XML malformado → 400."""
    tenant, _ = _fake_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/dtes-recibidos/upload",
        files={"archivo": ("envio.xml", io.BytesIO(b"<esto_no_es_un_envio_dte/>"), "text/xml")},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 400


# ──────────────────────────────────────────────────────────────────────────────
# POST /upload — happy path con fixture real del SII
# ──────────────────────────────────────────────────────────────────────────────

def test_upload_fixture_real_persiste_dtes():
    """Upload con XML real del SII persiste los DTEs en DB."""
    tenant, session = _fake_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    xml_bytes = _xml_bytes()
    resp = client.post(
        "/api/dtes-recibidos/upload",
        files={"archivo": ("ENVIO_DTE_4792140.xml", io.BytesIO(xml_bytes), "text/xml")},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["recibidos"] >= 1  # al menos 1 DTE en el fixture
    assert data["duplicados"] == 0
    assert len(data["dtes"]) == data["recibidos"]

    # Verificar que se persistió en la DB
    count = session.query(DteRecibido).count()
    assert count == data["recibidos"]


def test_upload_fixture_real_dtes_tienen_tipo_y_folio():
    """Los DTEs persistidos tienen tipo_dte y folio correctos."""
    tenant, session = _fake_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/dtes-recibidos/upload",
        files={"archivo": ("ENVIO_DTE_4792140.xml", io.BytesIO(_xml_bytes()), "text/xml")},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 201
    dtes = resp.json()["dtes"]
    for dte in dtes:
        assert dte["tipo_dte"] > 0
        assert dte["folio"] > 0
        assert dte["rut_emisor"]
        assert dte["estado_recepcion"] in ("pendiente", "acuse_enviado")


def test_upload_duplicado_no_persiste_segunda_vez():
    """Subir el mismo XML dos veces → segunda vez duplicados=N, recibidos=0."""
    tenant, session = _fake_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    xml_bytes = _xml_bytes()

    # Primer upload
    resp1 = client.post(
        "/api/dtes-recibidos/upload",
        files={"archivo": ("envio.xml", io.BytesIO(xml_bytes), "text/xml")},
    )
    assert resp1.status_code == 201
    recibidos_1 = resp1.json()["recibidos"]

    # Segundo upload del mismo archivo
    resp2 = client.post(
        "/api/dtes-recibidos/upload",
        files={"archivo": ("envio.xml", io.BytesIO(xml_bytes), "text/xml")},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp2.status_code == 201
    data2 = resp2.json()
    assert data2["recibidos"] == 0
    assert data2["duplicados"] == recibidos_1


def test_upload_sin_cert_reporta_acuse_pendiente():
    """Sin certificado la empresa, acuse_ok=False y acuse_error indica la causa."""
    tenant, session = _fake_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/dtes-recibidos/upload",
        files={"archivo": ("envio.xml", io.BytesIO(_xml_bytes()), "text/xml")},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 201
    data = resp.json()
    # Sin cert_data en la empresa, el acuse no puede generarse
    assert data["acuse_ok"] is False
    assert data["acuse_error"] is not None


# ──────────────────────────────────────────────────────────────────────────────
# GET /
# ──────────────────────────────────────────────────────────────────────────────

def test_listar_dtes_recibidos_retorna_lista():
    """GET / retorna lista (vacía al inicio)."""
    tenant, session = _fake_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/dtes-recibidos/")
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_listar_dtes_recibidos_despues_de_upload():
    """Después de un upload, GET / retorna los DTEs persistidos."""
    tenant, session = _fake_tenant()
    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    client.post(
        "/api/dtes-recibidos/upload",
        files={"archivo": ("envio.xml", io.BytesIO(_xml_bytes()), "text/xml")},
    )
    resp = client.get("/api/dtes-recibidos/")
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 200
    dtes = resp.json()
    assert len(dtes) >= 1
    assert all("id" in d for d in dtes)
    assert all("tipo_dte" in d for d in dtes)


# ──────────────────────────────────────────────────────────────────────────────
# POST /{id}/reclamar
# ──────────────────────────────────────────────────────────────────────────────

def _setup_dte_recibido(tenant: TenantContext, session: Session) -> str:
    """Crea un DteRecibido directo en DB para tests de reclamar."""
    empresa = session.query(Empresa).filter(Empresa.rut == tenant.empresa_rut).first()
    dte = DteRecibido(
        empresa_id=empresa.id,
        tipo_dte=33,
        folio=99901,
        rut_emisor="88888888-8",
        monto_total=100000,
        estado_recepcion="pendiente",
        firma_valida=True,
        created_at=datetime.utcnow(),
    )
    session.add(dte)
    session.commit()
    return dte.id


def test_reclamar_accion_aceptar():
    """Acción 'aceptar' cambia estado a 'aceptado'."""
    tenant, session = _fake_tenant()
    dte_id = _setup_dte_recibido(tenant, session)

    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/dtes-recibidos/{dte_id}/reclamar",
        json={"accion": "aceptar"},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["nuevo_estado"] == "aceptado"

    # Verificar en DB
    dte = session.query(DteRecibido).filter(DteRecibido.id == dte_id).first()
    assert dte.estado_recepcion == "aceptado"
    assert dte.aceptado_at is not None


def test_reclamar_accion_reclamar_con_motivo():
    """Acción 'reclamar' con motivo cambia estado a 'reclamado'."""
    tenant, session = _fake_tenant()
    dte_id = _setup_dte_recibido(tenant, session)

    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/dtes-recibidos/{dte_id}/reclamar",
        json={"accion": "reclamar", "motivo": "Mercadería no recibida"},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["nuevo_estado"] == "reclamado"

    dte = session.query(DteRecibido).filter(DteRecibido.id == dte_id).first()
    assert dte.estado_recepcion == "reclamado"
    assert dte.motivo_reclamo == "Mercadería no recibida"


def test_reclamar_en_estado_final_retorna_409():
    """DTE ya en estado final → 409."""
    tenant, session = _fake_tenant()
    dte_id = _setup_dte_recibido(tenant, session)

    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)

    # Primera acción: aceptar
    client.post(f"/api/dtes-recibidos/{dte_id}/reclamar", json={"accion": "aceptar"})

    # Segunda acción: intentar reclamar
    resp = client.post(
        f"/api/dtes-recibidos/{dte_id}/reclamar",
        json={"accion": "reclamar", "motivo": "intentando cambiar estado final"},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 409


def test_reclamar_accion_invalida_retorna_422():
    """Acción desconocida → 422."""
    tenant, session = _fake_tenant()
    dte_id = _setup_dte_recibido(tenant, session)

    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/dtes-recibidos/{dte_id}/reclamar",
        json={"accion": "hackear"},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 422


def test_reclamar_sin_motivo_retorna_422():
    """Acción 'reclamar' sin motivo → 422."""
    tenant, session = _fake_tenant()
    dte_id = _setup_dte_recibido(tenant, session)

    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/dtes-recibidos/{dte_id}/reclamar",
        json={"accion": "reclamar"},  # falta motivo
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 422


def test_reclamar_dte_no_existente_retorna_404():
    """DTE inexistente → 404."""
    tenant, _ = _fake_tenant()

    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/dtes-recibidos/{uuid.uuid4()}/reclamar",
        json={"accion": "aceptar"},
    )
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# GET /{id}/acuse
# ──────────────────────────────────────────────────────────────────────────────

def test_descargar_acuse_sin_acuse_retorna_404():
    """DTE sin acuse generado → 404."""
    tenant, session = _fake_tenant()
    dte_id = _setup_dte_recibido(tenant, session)  # estado=pendiente, sin acuse

    app.dependency_overrides[get_tenant] = lambda: tenant
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"/api/dtes-recibidos/{dte_id}/acuse")
    app.dependency_overrides.pop(get_tenant, None)

    assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# Model: DteRecibido
# ──────────────────────────────────────────────────────────────────────────────

def test_dte_recibido_model_persiste_correctamente():
    """DteRecibido se puede persistir y recuperar con todos sus campos."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)

    with Session(engine) as s, s.begin():
        empresa = Empresa(
            id=str(uuid.uuid4()),
            rut="76354771-K",
            razon_social="Test SA",
            giro="Comercio",
            direccion="Calle 1",
            comuna="Santiago",
            ciudad="Santiago",
        )
        s.add(empresa)
        s.flush()

        dte = DteRecibido(
            empresa_id=empresa.id,
            tipo_dte=33,
            folio=12345,
            rut_emisor="88888888-8",
            razon_social_emisor="SII Simulado",
            monto_total=500000,
            estado_recepcion="pendiente",
            firma_valida=False,
            firma_error="certificado desconocido",
        )
        s.add(dte)
        s.flush()
        dte_id = dte.id

    with Session(engine) as s:
        dte2 = s.query(DteRecibido).filter(DteRecibido.id == dte_id).first()
        assert dte2 is not None
        assert dte2.tipo_dte == 33
        assert dte2.folio == 12345
        assert dte2.rut_emisor == "88888888-8"
        assert dte2.monto_total == 500000
        assert dte2.estado_recepcion == "pendiente"
        assert dte2.firma_valida is False
        assert dte2.firma_error == "certificado desconocido"
