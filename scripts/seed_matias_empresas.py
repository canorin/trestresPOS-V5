"""Seed idempotente: Matías Bañados como master cliente en las 2 empresas.

Contexto:
    Las empresas ``77051056-2`` y ``77829149-5`` pertenecen a Matías
    (representante legal). Hasta ahora solo existía ``matias@trestres.cl``
    como super_admin (``empresa_rut='SYSTEM'``). Con el namespacing por RUT
    (``UNIQUE(empresa_rut, email)``) ahora podemos tener el mismo correo
    como master cliente de ambas empresas sin colisión.

Qué hace (todo es idempotente — re-ejecutable sin romper nada):
    1. Actualiza el super_admin con ``rut_personal=17586255-2``.
    2. Setea ``representante_legal_*`` en las 2 empresas (solo si está
       vacío; no sobrescribe datos ya capturados).
    3. Si falta, crea ``master_client`` en ``usuario_auth`` +
       ``Usuario`` (tenant.db) con una password auto-generada que se
       imprime al final para que el super admin la guarde.
    4. Si el usuario ya existe, deja la password intacta (no la rota) —
       solo rellena campos faltantes como ``rut_personal``.

Cómo correr::

    cd /path/to/trestresPOS
    python3.13 scripts/seed_matias_empresas.py

La password solo se imprime una vez al crear — después el master cliente
la rota desde su propia consola.
"""
from __future__ import annotations

import secrets
import uuid

import bcrypt

from crumbpos.db import multi_tenant as mt
from crumbpos.db.multi_tenant import (
    EmpresaRegistro,
    UsuarioAuth,
    _ensure_master,
    get_empresa_db_session,
)


RUT_MATIAS = "17586255-2"
NOMBRE_MATIAS = "Matías Bañados"
EMAIL_MATIAS = "matias@trestres.cl"
EMPRESAS = ("77051056-2", "77829149-5")


def _gen_password() -> str:
    """Password URL-safe de ~16 caracteres (96 bits de entropía)."""
    return secrets.token_urlsafe(12)


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _actualizar_super_admin(master) -> None:
    sa = master.query(UsuarioAuth).filter(
        UsuarioAuth.empresa_rut == "SYSTEM",
        UsuarioAuth.email == EMAIL_MATIAS,
    ).first()
    if not sa:
        print(f"  ⚠️  super_admin {EMAIL_MATIAS} no existe (se crea al arrancar app)")
        return
    cambios = []
    if not sa.rut_personal:
        sa.rut_personal = RUT_MATIAS
        cambios.append("rut_personal")
    if sa.nombre != NOMBRE_MATIAS:
        sa.nombre = NOMBRE_MATIAS
        cambios.append("nombre")
    if cambios:
        master.commit()
        print(f"  ✓ super_admin actualizado: {', '.join(cambios)}")
    else:
        print("  = super_admin ya tiene rut_personal y nombre correctos")


def _set_representante_legal(master, reg: EmpresaRegistro) -> None:
    cambios = []
    if not reg.representante_legal_nombre:
        reg.representante_legal_nombre = NOMBRE_MATIAS
        cambios.append("nombre")
    if not reg.representante_legal_rut:
        reg.representante_legal_rut = RUT_MATIAS
        cambios.append("rut")
    if not reg.representante_legal_email:
        reg.representante_legal_email = EMAIL_MATIAS
        cambios.append("email")
    if cambios:
        master.commit()
        print(f"    ✓ representante_legal fijado ({', '.join(cambios)})")
    else:
        print("    = representante_legal ya estaba fijado")


def _upsert_master_client(
    master, reg: EmpresaRegistro,
) -> tuple[str | None, str]:
    """Devuelve (password_nueva_o_None, mensaje).

    Si la password es None significa que el usuario ya existía y no se
    rotó (solo se rellenaron campos faltantes como rut_personal).
    """
    from crumbpos.db.models import Empresa, Usuario as UsuarioTenant

    existente = master.query(UsuarioAuth).filter(
        UsuarioAuth.empresa_rut == reg.rut,
        UsuarioAuth.email == EMAIL_MATIAS,
    ).first()

    if existente:
        # Rellenar campos faltantes sin tocar password.
        touched = []
        if not existente.rut_personal:
            existente.rut_personal = RUT_MATIAS
            touched.append("rut_personal")
        if existente.nombre != NOMBRE_MATIAS:
            existente.nombre = NOMBRE_MATIAS
            touched.append("nombre")
        if existente.rol != "master_client":
            existente.rol = "master_client"
            touched.append("rol")
        if not existente.activo:
            existente.activo = True
            touched.append("activo")
        if touched:
            master.commit()
            return None, f"existente — campos actualizados: {', '.join(touched)}"
        return None, "existente sin cambios"

    # Crear nuevo master_client con password auto-generada.
    password = _gen_password()
    password_hash = _hash(password)
    user_id = str(uuid.uuid4())

    master.add(UsuarioAuth(
        id=user_id,
        empresa_rut=reg.rut,
        email=EMAIL_MATIAS,
        nombre=NOMBRE_MATIAS,
        rut_personal=RUT_MATIAS,
        password_hash=password_hash,
        rol="master_client",
    ))
    master.commit()

    # Replicar en tenant.db (Usuario local de la empresa) para que el
    # mismo user_id resuelva en el lado operativo. Solo en el ambiente
    # activo — el otro se llena cuando la empresa cambia.
    tenant = get_empresa_db_session(reg.rut, reg.ambiente_activo)
    try:
        empresa = tenant.query(Empresa).filter_by(rut=reg.rut).first()
        if empresa:
            existente_tenant = tenant.query(UsuarioTenant).filter(
                UsuarioTenant.email == EMAIL_MATIAS,
                UsuarioTenant.empresa_id == empresa.id,
            ).first()
            if not existente_tenant:
                tenant.add(UsuarioTenant(
                    id=user_id,
                    empresa_id=empresa.id,
                    email=EMAIL_MATIAS,
                    nombre=NOMBRE_MATIAS,
                    password_hash=password_hash,
                    rol="master_client",
                ))
                tenant.commit()
    finally:
        tenant.close()

    return password, f"creado (user_id={user_id})"


def main() -> None:
    _ensure_master()
    # Leer la factory a través del módulo (no del import directo) porque
    # se asigna dinámicamente dentro de _ensure_master y el import
    # original capturaría la referencia None.
    master = mt._MasterSessionFactory()
    try:
        print(f"\n== Seed Matías como master cliente en {len(EMPRESAS)} empresas ==\n")

        print("[super_admin]")
        _actualizar_super_admin(master)
        print()

        passwords_nuevas: dict[str, str] = {}
        for rut in EMPRESAS:
            print(f"[{rut}]")
            reg = master.query(EmpresaRegistro).filter_by(rut=rut).first()
            if not reg:
                print(f"  ⚠️  empresa no registrada — skip")
                continue
            print(f"  → {reg.razon_social}")

            _set_representante_legal(master, reg)
            password, msg = _upsert_master_client(master, reg)
            print(f"    master_client: {msg}")
            if password:
                passwords_nuevas[rut] = password
            print()

        if passwords_nuevas:
            print("=" * 60)
            print("PASSWORDS INICIALES — guárdalas ahora, no se vuelven a mostrar")
            print("=" * 60)
            for rut, pw in passwords_nuevas.items():
                print(f"  {rut} · {EMAIL_MATIAS}")
                print(f"    login:    http://localhost:8000/{rut}/login")
                print(f"    password: {pw}")
                print()
            print("Próximo paso: iniciar sesión y rotar la password desde")
            print("la consola del cliente (módulo Usuarios).")
        else:
            print("Sin passwords nuevas — todos los admins ya existían.")
    finally:
        master.close()


if __name__ == "__main__":
    main()
