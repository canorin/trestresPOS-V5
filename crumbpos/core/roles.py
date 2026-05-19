"""Taxonomía canónica de roles y matriz de permisos.

Este módulo es la fuente única de verdad para "quién puede qué" en la
plataforma. Los routers y el frontend consultan estas funciones en vez
de hardcodear listas de strings en cada chequeo.

Jerarquía (de más a menos privilegios)::

    super_admin          staff Crumb (cross-empresa)
    master_client        dueño / representante legal de la empresa
    administrador        admin general de la empresa
    administrador_tienda admin de una sucursal
    cajero               operador de caja

El super admin **nunca** ingresa por la consola del cliente
(``/{rut}/login``). Opera solo desde ``/admin/*`` con su JWT de
``empresa_rut='SYSTEM'`` y, cuando actúa sobre una empresa cliente,
lo hace enviando el header ``X-Empresa-Rut``.

Reglas en prosa (ejecutadas por las funciones de abajo):

* Toda persona puede cambiar su propia contraseña.
* Para cambiar la contraseña de otro, el actor tiene que estar por
  encima en la jerarquía — con una excepción: dos ``master_client`` de
  la misma empresa son pares, nadie pisa al otro (el super admin sí).
* Para crear usuarios, cada rol puede crear roles estrictamente
  inferiores; además ``master_client`` puede crear otros
  ``master_client`` (socios/co-dueños).
* Para ver usuarios, super admin ve todo; el resto está scopeado a su
  empresa. Dentro de la empresa, solo se ven roles de nivel menor o
  igual, excepto ``master_client`` que ve a todos los de su empresa.
"""
from __future__ import annotations

# ── Taxonomía ───────────────────────────────────────────────

#: Roles canónicos ordenados de mayor a menor privilegio. El índice
#: sirve para calcular ``nivel()``. NO cambiar el orden sin migrar la DB.
ROLES_JERARQUIA: tuple[str, ...] = (
    "super_admin",
    "master_client",
    "administrador",
    "administrador_tienda",
    "cajero",
)

#: Etiqueta legible (para UI). Claves: rol canónico.
ROLES_LABEL: dict[str, str] = {
    "super_admin": "Super admin",
    "master_client": "Master client",
    "administrador": "Administrador",
    "administrador_tienda": "Administrador de tienda",
    "cajero": "Cajero",
}

#: Descripción corta del rol (para UI / tooltips).
ROLES_DESC: dict[str, str] = {
    "super_admin": "Staff de Crumb — opera sobre todas las empresas.",
    "master_client": "Dueño o representante legal de la empresa.",
    "administrador": "Administra la empresa sin ser dueño.",
    "administrador_tienda": "Administra una sucursal.",
    "cajero": "Opera la caja de una sucursal.",
}

#: Alias de nombres viejos → canónicos. Sirve para migrar strings sueltas
#: en BDs que ya tengan valores antiguos (``admin_empresa`` era el único
#: rol "dueño-de-empresa" antes de la división master_client/administrador).
ROLES_ALIASES: dict[str, str] = {
    "admin_empresa": "master_client",
    "admin_sucursal": "administrador_tienda",
}

#: Qué roles puede CREAR cada rol (subconjuntos de ROLES_JERARQUIA).
CAN_CREATE: dict[str, tuple[str, ...]] = {
    "super_admin": (
        "master_client", "administrador", "administrador_tienda", "cajero",
    ),
    "master_client": (
        "master_client", "administrador", "administrador_tienda", "cajero",
    ),
    "administrador": ("administrador_tienda", "cajero"),
    "administrador_tienda": ("cajero",),
    "cajero": (),
}


# ── Helpers ─────────────────────────────────────────────────

def normalizar(rol: str | None) -> str | None:
    """Traduce aliases históricos al rol canónico.

    >>> normalizar("admin_empresa")
    'master_client'
    >>> normalizar("cajero")
    'cajero'
    >>> normalizar(None) is None
    True
    """
    if rol is None:
        return None
    return ROLES_ALIASES.get(rol, rol)


def es_valido(rol: str | None) -> bool:
    """True si el rol (ya normalizado) está en la taxonomía."""
    return normalizar(rol) in ROLES_JERARQUIA


def nivel(rol: str | None) -> int:
    """Nivel numérico del rol — mayor = más privilegios.

    super_admin=5, master_client=4, administrador=3,
    administrador_tienda=2, cajero=1. Rol inválido / None → 0.
    """
    rol_c = normalizar(rol)
    if rol_c not in ROLES_JERARQUIA:
        return 0
    return len(ROLES_JERARQUIA) - ROLES_JERARQUIA.index(rol_c)


def puede_crear(actor_rol: str, nuevo_rol: str) -> bool:
    """¿Puede ``actor_rol`` crear un usuario con rol ``nuevo_rol``?"""
    return normalizar(nuevo_rol) in CAN_CREATE.get(normalizar(actor_rol) or "", ())


def puede_cambiar_password(
    actor_rol: str,
    actor_id: str,
    target_rol: str,
    target_id: str,
) -> bool:
    """¿Puede ``actor`` cambiar la password de ``target``?

    Reglas combinadas:

    1. Un user siempre puede cambiar su propia password
       (``actor_id == target_id``).
    2. super_admin puede cambiar la de cualquiera — incluidos otros
       super_admin (staff interno que se autoregula).
    3. master_client NO puede cambiar la de otro master_client (son
       pares dentro de la empresa).
    4. En cualquier otro caso: permitido si
       ``nivel(actor) > nivel(target)``.

    Ojo: esta función no valida que actor y target pertenezcan a la
    misma empresa — eso lo hace el router con el context de tenant.
    """
    if actor_id == target_id:
        return True
    a, t = normalizar(actor_rol), normalizar(target_rol)
    if a == "super_admin":
        return True
    if a == "master_client" and t == "master_client":
        return False
    return nivel(a) > nivel(t)


def puede_ver_usuario(
    actor_rol: str,
    actor_empresa_rut: str,
    target_rol: str,
    target_empresa_rut: str,
) -> bool:
    """¿Puede ``actor`` listar/leer al user ``target``?

    - super_admin ve todo.
    - Resto: solo su propia empresa.
    - Dentro de la empresa:
        * master_client ve a todos (incluidos otros master_client).
        * administrador / administrador_tienda / cajero ven solo roles
          de nivel menor o igual al suyo.
    """
    a = normalizar(actor_rol)
    if a == "super_admin":
        return True
    if actor_empresa_rut != target_empresa_rut:
        return False
    if a == "master_client":
        return True
    t = normalizar(target_rol)
    return nivel(t) <= nivel(a)


def puede_gestionar_empresa(rol: str) -> bool:
    """Atajo para "este rol puede tocar config de la empresa".

    Equivale a super_admin, master_client o administrador. Sirve para
    endpoints que antes hacían ``rol in ("super_admin","admin_empresa")``.
    """
    return nivel(rol) >= nivel("administrador")


def puede_gestionar_sucursal(rol: str) -> bool:
    """Atajo para "este rol puede tocar sucursales/cajas".

    Incluye ``administrador_tienda`` para arriba (el admin de sucursal
    puede administrar SU sucursal).
    """
    return nivel(rol) >= nivel("administrador_tienda")
