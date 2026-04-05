"""Seed: carga datos iniciales para desarrollo.

Ejecutar: python -m crumbpos.api.seed
"""
from crumbpos.db.database import SessionLocal, init_db
from crumbpos.db.models import (
    Empresa, Sucursal, Caja, Usuario, UsuarioSucursal,
    Familia, FamiliaSucursal, Articulo, ArticuloSucursal, Bodega,
)
from crumbpos.api.auth.jwt import hash_password


def seed():
    init_db()
    db = SessionLocal()

    # Verificar si ya existe
    if db.query(Empresa).first():
        print("Base de datos ya tiene datos. Saltando seed.")
        db.close()
        return

    # ─── EMPRESA ───
    empresa = Empresa(
        rut="77051056-2",
        razon_social="TRESTRES PUBLICIDAD SPA",
        nombre_fantasia="Trestres",
        giro="PASTELERIA Y PANADERIA",
        acteco=472105,
        direccion="CAMINO DEL ALBA 11969 LT",
        comuna="LAS CONDES",
        ciudad="SANTIAGO",
        ambiente_sii="certificacion",
        fecha_resolucion="2026-03-30",
        numero_resolucion=0,
        cert_rut_firmante="17586255-2",
    )
    db.add(empresa)
    db.flush()

    # ─── SUCURSALES ───
    suc_lc = Sucursal(
        empresa_id=empresa.id,
        nombre="Las Condes",
        codigo="LC",
        direccion="CAMINO DEL ALBA 11969 LT",
        comuna="LAS CONDES",
        ciudad="SANTIAGO",
    )
    suc_prov = Sucursal(
        empresa_id=empresa.id,
        nombre="Providencia",
        codigo="PV",
        direccion="AV PROVIDENCIA 1234",
        comuna="PROVIDENCIA",
        ciudad="SANTIAGO",
    )
    db.add_all([suc_lc, suc_prov])
    db.flush()

    # ─── CAJAS ───
    caja_lc1 = Caja(sucursal_id=suc_lc.id, nombre="Caja 1")
    caja_lc2 = Caja(sucursal_id=suc_lc.id, nombre="Caja 2")
    caja_prov1 = Caja(sucursal_id=suc_prov.id, nombre="Caja 1")
    db.add_all([caja_lc1, caja_lc2, caja_prov1])

    # ─── BODEGAS (preparadas para fase 2) ───
    bod_lc = Bodega(sucursal_id=suc_lc.id, nombre="Bodega Venta", tipo="venta", es_default=True)
    bod_prov = Bodega(sucursal_id=suc_prov.id, nombre="Bodega Venta", tipo="venta", es_default=True)
    db.add_all([bod_lc, bod_prov])

    # ─── USUARIOS ───
    admin = Usuario(
        empresa_id=empresa.id,
        email="admin@trestres.cl",
        nombre="Matías Bañados",
        password_hash=hash_password("admin123"),
        rol="admin_empresa",
    )
    cajero1 = Usuario(
        empresa_id=empresa.id,
        email="cajero1@trestres.cl",
        nombre="María González",
        password_hash=hash_password("cajero123"),
        rol="cajero",
    )
    db.add_all([admin, cajero1])
    db.flush()

    # Acceso a sucursales
    db.add(UsuarioSucursal(usuario_id=admin.id, sucursal_id=suc_lc.id))
    db.add(UsuarioSucursal(usuario_id=admin.id, sucursal_id=suc_prov.id))
    db.add(UsuarioSucursal(usuario_id=cajero1.id, sucursal_id=suc_lc.id))

    # ─── FAMILIAS ───
    fam_pan = Familia(empresa_id=empresa.id, nombre="Panadería", codigo="PAN", color="#D4A574", orden=1)
    fam_past = Familia(empresa_id=empresa.id, nombre="Pastelería", codigo="PAST", color="#E8A0BF", orden=2)
    fam_cafe = Familia(empresa_id=empresa.id, nombre="Cafetería", codigo="CAF", color="#8B6914", orden=3)
    fam_beb = Familia(empresa_id=empresa.id, nombre="Bebidas", codigo="BEB", color="#4A90D9", orden=4)
    db.add_all([fam_pan, fam_past, fam_cafe, fam_beb])
    db.flush()

    # Familias activas por sucursal
    for fam in [fam_pan, fam_past, fam_cafe, fam_beb]:
        db.add(FamiliaSucursal(familia_id=fam.id, sucursal_id=suc_lc.id, activa=True, orden=fam.orden))
    for fam in [fam_pan, fam_past, fam_beb]:  # Providencia sin cafetería
        db.add(FamiliaSucursal(familia_id=fam.id, sucursal_id=suc_prov.id, activa=True, orden=fam.orden))
    # Cafetería desactivada en Providencia
    db.add(FamiliaSucursal(familia_id=fam_cafe.id, sucursal_id=suc_prov.id, activa=False))

    # ─── ARTÍCULOS ───
    articulos_data = [
        # (nombre, nombre_corto, familia, sku, precio, exento)
        ("Marraqueta bolsa 6 unidades", "Marraqueta 6un", fam_pan, "P001", 1800, False),
        ("Pan de molde integral 500gr", "Pan molde int.", fam_pan, "P002", 3200, False),
        ("Hallulla bolsa 10 unidades", "Hallulla 10un", fam_pan, "P003", 2500, False),
        ("Croissant mantequilla", "Croissant", fam_pan, "P004", 950, False),
        ("Torta tres leches porción", "Torta 3 leches", fam_past, "T001", 3500, False),
        ("Torta chocolate porción", "Torta chocolate", fam_past, "T002", 3500, False),
        ("Galleta avena chocolate", "Galleta avena", fam_past, "T003", 1050, False),
        ("Café latte grande", "Café latte gde", fam_cafe, "C001", 2500, False),
        ("Café americano", "Café americano", fam_cafe, "C002", 2000, False),
        ("Cappuccino", "Cappuccino", fam_cafe, "C003", 2800, False),
        ("Agua mineral 500ml", "Agua 500ml", fam_beb, "B001", 1500, False),
        ("Jugo natural 350ml", "Jugo nat 350ml", fam_beb, "B002", 2200, False),
    ]

    for nombre, nombre_corto, familia, sku, precio, exento in articulos_data:
        art = Articulo(
            empresa_id=empresa.id,
            familia_id=familia.id,
            sku=sku,
            nombre=nombre,
            nombre_corto=nombre_corto,
            precio_default=precio,
            es_exento=exento,
        )
        db.add(art)
        db.flush()

        # Activar en ambas sucursales
        db.add(ArticuloSucursal(
            articulo_id=art.id, sucursal_id=suc_lc.id, activo=True
        ))
        # En Providencia: mismos precios excepto cafetería (familia desactivada)
        db.add(ArticuloSucursal(
            articulo_id=art.id, sucursal_id=suc_prov.id, activo=True
        ))

    db.commit()
    db.close()

    print("Seed completado:")
    print("  Empresa: TRESTRES PUBLICIDAD SPA")
    print("  Sucursales: Las Condes, Providencia")
    print("  Usuarios: admin@trestres.cl (admin123), cajero1@trestres.cl (cajero123)")
    print("  Familias: 4 (Panadería, Pastelería, Cafetería, Bebidas)")
    print(f"  Artículos: {len(articulos_data)}")


if __name__ == "__main__":
    seed()
