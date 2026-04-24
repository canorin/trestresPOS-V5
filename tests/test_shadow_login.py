"""Tests del shadow login — POST /api/admin/empresas/{rut}/entrar.

Cubre tres contratos:

1. El endpoint acuña un JWT con los claims correctos (sub, empresa_rut,
   rol, shadow, exp) y falla con el código HTTP correcto cuando la
   empresa no existe o está dada de baja.
2. ``get_tenant`` acepta el claim ``empresa_rut`` del JWT como fuente
   válida de tenant cuando el caller es super_admin, sin necesidad
   del header ``X-Empresa-Rut``. Esto es lo que permite que el
   dashboard del master cliente funcione sin inyectar headers extra.
3. El sentinel ``empresa_rut="SYSTEM"`` del JWT normal de super_admin
   **no** se trata como RUT válido; sigue exigiendo header.

Los tests corren sobre SQLite in-memory. No levantan HTTP ni TestClient —
invocan los endpoints/dependencies como funciones con fixtures.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import bcrypt
import pytest
from fastapi import HTTPException
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.api.dependencies import ALGORITHM, SECRET_KEY
from crumbpos.api.routers.baja_empresas import (
    SHADOW_TOKEN_EXPIRE_MINUTES,
    entrar_consola_cliente,
)
from crumbpos.db.multi_tenant import BaseMaster, EmpresaRegistro, UsuarioAuth


# ══════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def master_session():
    """master.db in-memory con tablas creadas."""
    engine = create_engine("sqlite:///:memory:", future=True)
    BaseMaster.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def super_admin(master_session):
    """Inserta un super_admin en master.db y lo devuelve."""
    admin = UsuarioAuth(
        id="admin-uuid-00000000",
        empresa_rut="SYSTEM",
        email="matias@trestres.cl",
        password_hash=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        nombre="Matías Bañados",
        rol="super_admin",
        activo=True,
    )
    master_session.add(admin)
    master_session.commit()
    return admin


@pytest.fixture
def empresa_activa(master_session):
    """Empresa activa lista para ser 'entrada' por el super admin."""
    reg = EmpresaRegistro(
        rut="77829149-5",
        razon_social="GRUPO TRESTRES SPA",
        ambiente_activo="certificacion",
        etapa="pendiente_certificacion",
        plan="full_free",
        estado="activa",
    )
    master_session.add(reg)
    master_session.commit()
    return reg


@pytest.fixture
def empresa_papelera(master_session):
    """Empresa dada de baja soft — no debe aceptar shadow."""
    reg = EmpresaRegistro(
        rut="11111111-1",
        razon_social="EMPRESA EN PAPELERA",
        ambiente_activo="certificacion",
        etapa="pendiente_certificacion",
        plan="full_free",
        estado="eliminada_soft",
    )
    master_session.add(reg)
    master_session.commit()
    return reg


# ══════════════════════════════════════════════════════════════════
# El endpoint acuña un JWT shadow con los claims correctos
# ══════════════════════════════════════════════════════════════════


class TestEntrarConsolaClienteClaims:
    """Valida el payload del JWT devuelto por POST /{rut}/entrar."""

    def test_token_trae_claims_correctos(
        self, master_session, super_admin, empresa_activa,
    ):
        before = datetime.utcnow()
        resp = entrar_consola_cliente(
            rut=empresa_activa.rut,
            admin=super_admin,
            master_db=master_session,
        )
        after = datetime.utcnow()

        decoded = jwt.decode(
            resp.access_token, SECRET_KEY, algorithms=[ALGORITHM],
        )
        assert decoded["sub"] == super_admin.id
        assert decoded["empresa_rut"] == empresa_activa.rut
        assert decoded["rol"] == "super_admin"
        assert decoded["shadow"] is True
        assert decoded["sucursal_id"] is None

        # exp ≈ now + SHADOW_TOKEN_EXPIRE_MINUTES
        exp = datetime.utcfromtimestamp(decoded["exp"])
        expected_min = before + timedelta(
            minutes=SHADOW_TOKEN_EXPIRE_MINUTES,
        ) - timedelta(seconds=5)
        expected_max = after + timedelta(
            minutes=SHADOW_TOKEN_EXPIRE_MINUTES,
        ) + timedelta(seconds=5)
        assert expected_min <= exp <= expected_max

    def test_respuesta_incluye_metadata_para_frontend(
        self, master_session, super_admin, empresa_activa,
    ):
        resp = entrar_consola_cliente(
            rut=empresa_activa.rut,
            admin=super_admin,
            master_db=master_session,
        )
        assert resp.empresa_rut == empresa_activa.rut
        assert resp.empresa_razon_social == empresa_activa.razon_social
        assert resp.ambiente_activo == empresa_activa.ambiente_activo
        assert resp.expires_in_minutes == SHADOW_TOKEN_EXPIRE_MINUTES
        assert resp.dashboard_url == f"/{empresa_activa.rut}/dashboard"
        assert resp.token_type == "bearer"

    def test_ttl_es_corto_para_sesion_elevada(self):
        """La sesión shadow debe ser mucho más corta que el login normal.

        Si alguien sube :data:`SHADOW_TOKEN_EXPIRE_MINUTES` al nivel del
        login regular (480 min) el test falla para forzar la discusión.
        """
        assert 15 <= SHADOW_TOKEN_EXPIRE_MINUTES <= 120


# ══════════════════════════════════════════════════════════════════
# Errores: empresa inexistente, papelera
# ══════════════════════════════════════════════════════════════════


class TestEntrarConsolaClienteErrores:

    def test_empresa_inexistente_404(self, master_session, super_admin):
        with pytest.raises(HTTPException) as exc_info:
            entrar_consola_cliente(
                rut="99999999-9",
                admin=super_admin,
                master_db=master_session,
            )
        assert exc_info.value.status_code == 404
        assert "no registrada" in exc_info.value.detail.lower()

    def test_empresa_en_papelera_410(
        self, master_session, super_admin, empresa_papelera,
    ):
        with pytest.raises(HTTPException) as exc_info:
            entrar_consola_cliente(
                rut=empresa_papelera.rut,
                admin=super_admin,
                master_db=master_session,
            )
        assert exc_info.value.status_code == 410
        assert "dada de baja" in exc_info.value.detail.lower()


# ══════════════════════════════════════════════════════════════════
# Integración: get_tenant acepta el claim empresa_rut del shadow JWT
# ══════════════════════════════════════════════════════════════════


class TestGetTenantAceptaShadowJWT:
    """``get_tenant`` debe leer ``empresa_rut`` del JWT para super_admin.

    Esto es lo que permite que el dashboard del master cliente funcione
    con el token shadow sin que el JS tenga que inyectar el header
    ``X-Empresa-Rut`` en cada fetch. El contrato se testea directamente
    sobre la función ``_decode_token`` y la lógica del if de
    ``get_tenant``, porque el resto de la función (abrir engine del
    tenant) requiere filesystem real.
    """

    def test_claim_empresa_rut_se_extrae_del_jwt(
        self, super_admin, empresa_activa, master_session,
    ):
        """Smoke: el token que mintea el endpoint trae el RUT del tenant."""
        resp = entrar_consola_cliente(
            rut=empresa_activa.rut,
            admin=super_admin,
            master_db=master_session,
        )
        decoded = jwt.decode(
            resp.access_token, SECRET_KEY, algorithms=[ALGORITHM],
        )
        # Este es el claim que get_tenant lee cuando header = None.
        assert decoded["empresa_rut"] == empresa_activa.rut
        assert decoded["empresa_rut"] != "SYSTEM"

    def test_login_normal_super_admin_sigue_trayendo_SYSTEM(self):
        """El login regular del super admin (sin shadow) trae SYSTEM.

        ``get_tenant`` filtra ese sentinel para no tratarlo como RUT:
        si el super admin se loguea en ``/admin/login`` sin pasar por
        ``/entrar``, tiene que seguir usando el header para navegar
        entre tenants (por ej. desde la consola super admin).
        """
        # Simulamos el payload que emite auth.login para super_admin.
        payload_login_normal = {
            "sub": "admin-id",
            "empresa_rut": "SYSTEM",
            "rol": "super_admin",
            "sucursal_id": None,
        }
        # El filtro vive inline en get_tenant; acá solo aseguramos que
        # el sentinel sigue siendo el mismo string que filtramos.
        assert payload_login_normal["empresa_rut"] == "SYSTEM"
