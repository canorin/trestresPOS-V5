"""Tests del módulo canónico de roles + matriz de permisos.

La tabla que vive en docstrings de ``crumbpos.core.roles`` se ejecuta
acá como código. Si alguna celda cambia, estos tests fallan y obligan
a actualizar doc + call sites en conjunto.
"""
from crumbpos.core import roles


# ── Taxonomía básica ─────────────────────────────────────────

def test_niveles_orden_estricto():
    """El orden de la jerarquía es super_admin > ... > cajero."""
    assert roles.nivel("super_admin") > roles.nivel("master_client")
    assert roles.nivel("master_client") > roles.nivel("administrador")
    assert roles.nivel("administrador") > roles.nivel("administrador_tienda")
    assert roles.nivel("administrador_tienda") > roles.nivel("cajero")
    assert roles.nivel("cajero") > 0


def test_nivel_rol_invalido_es_cero():
    assert roles.nivel("foo") == 0
    assert roles.nivel("") == 0
    assert roles.nivel(None) == 0


def test_aliases_se_traducen():
    """Los nombres viejos siguen siendo legibles durante la migración."""
    assert roles.normalizar("admin_empresa") == "master_client"
    assert roles.normalizar("admin_sucursal") == "administrador_tienda"
    # El canónico pasa intacto.
    assert roles.normalizar("cajero") == "cajero"


def test_es_valido():
    for rol in roles.ROLES_JERARQUIA:
        assert roles.es_valido(rol)
    assert roles.es_valido("admin_empresa")  # via alias
    assert not roles.es_valido("gerente")


# ── puede_crear ──────────────────────────────────────────────

def test_super_admin_crea_cualquier_rol_menos_super_admin():
    """super_admin no se clona a sí mismo — son staff trestresPOS
    creados manualmente, no por la UI."""
    for rol in ("master_client", "administrador", "administrador_tienda", "cajero"):
        assert roles.puede_crear("super_admin", rol)
    assert not roles.puede_crear("super_admin", "super_admin")


def test_master_client_puede_crear_otros_master_client():
    """Caso de co-dueños / socios."""
    assert roles.puede_crear("master_client", "master_client")
    assert roles.puede_crear("master_client", "administrador")
    assert roles.puede_crear("master_client", "cajero")
    assert not roles.puede_crear("master_client", "super_admin")


def test_administrador_solo_admin_tienda_y_cajero():
    assert roles.puede_crear("administrador", "administrador_tienda")
    assert roles.puede_crear("administrador", "cajero")
    assert not roles.puede_crear("administrador", "master_client")
    assert not roles.puede_crear("administrador", "administrador")


def test_admin_tienda_solo_crea_cajeros():
    assert roles.puede_crear("administrador_tienda", "cajero")
    assert not roles.puede_crear("administrador_tienda", "administrador_tienda")
    assert not roles.puede_crear("administrador_tienda", "administrador")


def test_cajero_no_crea_nada():
    for rol in roles.ROLES_JERARQUIA:
        assert not roles.puede_crear("cajero", rol)


# ── puede_cambiar_password ──────────────────────────────────

def test_todos_cambian_su_propia_password():
    for rol in roles.ROLES_JERARQUIA:
        assert roles.puede_cambiar_password(rol, "u1", rol, "u1")


def test_super_admin_cambia_password_de_cualquiera():
    for rol in roles.ROLES_JERARQUIA:
        assert roles.puede_cambiar_password("super_admin", "sa", rol, "u2")


def test_master_client_no_pisa_a_otro_master_client():
    """Peer rule: un dueño no rotea el password de otro dueño."""
    assert not roles.puede_cambiar_password(
        "master_client", "m1", "master_client", "m2",
    )


def test_master_client_cambia_password_de_subs():
    for rol in ("administrador", "administrador_tienda", "cajero"):
        assert roles.puede_cambiar_password("master_client", "m1", rol, "u2")


def test_administrador_cambia_passwords_estrictamente_abajo():
    assert roles.puede_cambiar_password(
        "administrador", "a1", "administrador_tienda", "u2",
    )
    assert roles.puede_cambiar_password("administrador", "a1", "cajero", "u2")
    # No sube a master_client.
    assert not roles.puede_cambiar_password(
        "administrador", "a1", "master_client", "m1",
    )
    # Tampoco a un peer administrador.
    assert not roles.puede_cambiar_password(
        "administrador", "a1", "administrador", "a2",
    )


def test_admin_tienda_cambia_solo_cajeros():
    assert roles.puede_cambiar_password(
        "administrador_tienda", "at1", "cajero", "c1",
    )
    assert not roles.puede_cambiar_password(
        "administrador_tienda", "at1", "administrador_tienda", "at2",
    )
    assert not roles.puede_cambiar_password(
        "administrador_tienda", "at1", "administrador", "a1",
    )


def test_cajero_solo_cambia_su_propia_password():
    assert roles.puede_cambiar_password("cajero", "c1", "cajero", "c1")
    assert not roles.puede_cambiar_password("cajero", "c1", "cajero", "c2")


# ── puede_ver_usuario ───────────────────────────────────────

def test_super_admin_ve_usuarios_de_cualquier_empresa():
    assert roles.puede_ver_usuario(
        "super_admin", "SYSTEM",
        "cajero", "77829149-5",
    )


def test_no_super_admin_no_ve_otra_empresa():
    assert not roles.puede_ver_usuario(
        "master_client", "77829149-5",
        "cajero", "77051056-2",
    )


def test_master_client_ve_todos_los_de_su_empresa():
    rut = "77829149-5"
    for rol in ("master_client", "administrador", "cajero"):
        assert roles.puede_ver_usuario("master_client", rut, rol, rut)


def test_administrador_no_ve_master_client():
    rut = "77829149-5"
    assert not roles.puede_ver_usuario(
        "administrador", rut, "master_client", rut,
    )
    assert roles.puede_ver_usuario(
        "administrador", rut, "administrador_tienda", rut,
    )
    # Se ve a sí mismo (peer).
    assert roles.puede_ver_usuario("administrador", rut, "administrador", rut)


def test_cajero_solo_ve_cajeros():
    rut = "77829149-5"
    assert roles.puede_ver_usuario("cajero", rut, "cajero", rut)
    for rol in ("administrador_tienda", "administrador", "master_client"):
        assert not roles.puede_ver_usuario("cajero", rut, rol, rut)


# ── Atajos ──────────────────────────────────────────────────

def test_atajos_gestionar():
    assert roles.puede_gestionar_empresa("super_admin")
    assert roles.puede_gestionar_empresa("master_client")
    assert roles.puede_gestionar_empresa("administrador")
    assert not roles.puede_gestionar_empresa("administrador_tienda")
    assert not roles.puede_gestionar_empresa("cajero")

    assert roles.puede_gestionar_sucursal("administrador_tienda")
    assert not roles.puede_gestionar_sucursal("cajero")


def test_atajos_aceptan_alias_viejos():
    # Las rutas de migración: si un JWT viene con rol="admin_empresa"
    # (antes de rehash), los atajos igual deben funcionar.
    assert roles.puede_gestionar_empresa("admin_empresa")
    assert roles.puede_gestionar_sucursal("admin_sucursal")
