"""Tests para B3 — endpoints ARCO Ley 19.628.

Cubre:
- GET /api/datos-personales/me devuelve campos personales del usuario
- GET /api/datos-personales/me sin auth → 401
- POST /api/datos-personales/solicitud-cancelacion crea solicitud
- POST con tipo inválido → 422
- Solicitud queda en master.db con estado 'pendiente'
"""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from crumbpos.api.app import app
from crumbpos.api.dependencies import get_current_user
from crumbpos.db.multi_tenant import UsuarioAuth


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _mock_user(
    nombre: str = "Juan Pérez",
    email: str = "juan@empresa.cl",
    rut_personal: str | None = "12345678-9",
    empresa_rut: str = "76354771-K",
    rol: str = "cajero",
) -> UsuarioAuth:
    user = MagicMock(spec=UsuarioAuth)
    user.id = str(uuid.uuid4())
    user.nombre = nombre
    user.email = email
    user.rut_personal = rut_personal
    user.empresa_rut = empresa_rut
    user.rol = rol
    user.activo = True
    user.created_at = datetime(2025, 1, 15, 10, 0, 0)
    return user


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/datos-personales/me
# ──────────────────────────────────────────────────────────────────────────────

def test_mis_datos_sin_auth_retorna_401():
    """Sin token debe retornar 401."""
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/datos-personales/me")
    assert resp.status_code == 401


def test_mis_datos_retorna_campos_personales():
    """Con usuario autenticado devuelve nombre, email, rut_personal, etc."""
    user = _mock_user()

    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/datos-personales/me")
    app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["nombre"] == "Juan Pérez"
    assert data["email"] == "juan@empresa.cl"
    assert data["rut_personal"] == "12345678-9"
    assert data["empresa_rut"] == "76354771-K"
    assert data["rol"] == "cajero"
    assert data["activo"] is True
    assert "campos_almacenados" in data
    assert len(data["campos_almacenados"]) > 0


def test_mis_datos_sin_rut_personal():
    """Si el usuario no tiene rut_personal, el campo sale null."""
    user = _mock_user(rut_personal=None)

    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/datos-personales/me")
    app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    assert resp.json()["rut_personal"] is None


def test_mis_datos_lista_campos_almacenados():
    """La respuesta incluye una descripción de cada campo almacenado."""
    user = _mock_user()

    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/datos-personales/me")
    app.dependency_overrides.pop(get_current_user, None)

    campos = resp.json()["campos_almacenados"]
    campo_nombres = [c["campo"] for c in campos]
    # Debe listar al menos: nombre, email, rut_personal
    assert "nombre" in campo_nombres
    assert "email" in campo_nombres
    assert "rut_personal" in campo_nombres


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/datos-personales/solicitud-cancelacion
# ──────────────────────────────────────────────────────────────────────────────

def test_solicitud_sin_auth_retorna_401():
    """Sin token debe retornar 401."""
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/datos-personales/solicitud-cancelacion",
        json={"tipo": "cancelacion"},
    )
    assert resp.status_code == 401


def test_solicitud_tipo_invalido_retorna_422():
    """Tipo desconocido → 422."""
    user = _mock_user()

    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/datos-personales/solicitud-cancelacion",
        json={"tipo": "hackeo"},
    )
    app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 422


def test_solicitud_cancelacion_registra_en_db():
    """POST solicitud-cancelacion persiste en master.db y devuelve solicitud_id."""
    user = _mock_user()
    solicitudes_guardadas = []

    def fake_session():
        session = MagicMock()
        session.add.side_effect = lambda obj: solicitudes_guardadas.append(obj)
        session.commit.return_value = None
        session.close.return_value = None
        return session

    app.dependency_overrides[get_current_user] = lambda: user
    with patch(
        "crumbpos.api.routers.datos_personales.get_master_session",
        side_effect=fake_session,
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/datos-personales/solicitud-cancelacion",
            json={"tipo": "cancelacion", "motivo": "Ya no uso el servicio"},
        )
    app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201
    data = resp.json()
    assert "solicitud_id" in data
    assert data["tipo"] == "cancelacion"
    assert data["estado"] == "pendiente"
    assert "5 días hábiles" in data["mensaje"]
    assert data["solicitud_id"] in data["mensaje"]

    # Verificar que se persistió la solicitud
    assert len(solicitudes_guardadas) == 1
    sol = solicitudes_guardadas[0]
    assert sol.tipo == "cancelacion"
    assert sol.motivo == "Ya no uso el servicio"
    assert sol.usuario_id == user.id
    assert sol.empresa_rut == user.empresa_rut
    assert sol.estado == "pendiente"


def test_solicitud_acceso_tambien_valida():
    """Tipo 'acceso' también es válido."""
    user = _mock_user()

    def fake_session():
        session = MagicMock()
        session.commit.return_value = None
        session.close.return_value = None
        return session

    app.dependency_overrides[get_current_user] = lambda: user
    with patch(
        "crumbpos.api.routers.datos_personales.get_master_session",
        side_effect=fake_session,
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/datos-personales/solicitud-cancelacion",
            json={"tipo": "acceso"},
        )
    app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201
    assert resp.json()["tipo"] == "acceso"


def test_solicitud_rectificacion_valida():
    """Tipo 'rectificacion' es válido."""
    user = _mock_user()

    def fake_session():
        session = MagicMock()
        session.commit.return_value = None
        session.close.return_value = None
        return session

    app.dependency_overrides[get_current_user] = lambda: user
    with patch(
        "crumbpos.api.routers.datos_personales.get_master_session",
        side_effect=fake_session,
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/datos-personales/solicitud-cancelacion",
            json={"tipo": "rectificacion", "motivo": "Mi nombre tiene un error"},
        )
    app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201
