"""Migra los CAFs de archivos locales a la base de datos.

Uso:
    python -m crumbpos.scripts.migrar_cafs_a_db <RUT_EMPRESA>

Ejemplo:
    python -m crumbpos.scripts.migrar_cafs_a_db 77829149-5

La empresa debe existir en la base de datos antes de ejecutar este script.
Créala primero via API: POST /api/empresas
Lee los XMLs de nuevapostulacion/CAF/ y los inserta en la tabla caf_folio.
Respeta los folios consumidos del JSON existente.
"""
import json
import sys
from pathlib import Path

# Agregar root al path
root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(root))

from crumbpos.db.database import SessionLocal, init_db
from crumbpos.db.models import Empresa, CafFolio
from crumbpos.core.caf.caf_manager import CAF
from crumbpos.config import settings


def migrar(rut_empresa: str):
    init_db()
    db = SessionLocal()

    try:
        # 1. Buscar la empresa en DB (debe existir previamente)
        empresa = db.query(Empresa).filter(Empresa.rut == rut_empresa).first()
        if not empresa:
            print(f"ERROR: Empresa {rut_empresa} no existe en la base de datos.")
            print("Créala primero via API: POST /api/empresas")
            sys.exit(1)
        print(f"Empresa: {empresa.rut} - {empresa.razon_social} (ID: {empresa.id})")

        # 2. Cargar folios consumidos
        caf_dir = settings.CAF_DIR
        folios_file = caf_dir / "folios_consumidos.json"
        folios_consumidos = {}
        if folios_file.exists():
            folios_consumidos = json.loads(folios_file.read_text())
            print(f"Folios consumidos: {folios_consumidos}")

        # 3. Recorrer XMLs
        xml_files = sorted(caf_dir.rglob("*.xml"))
        print(f"\nEncontrados {len(xml_files)} archivos XML en {caf_dir}")

        migrados = 0
        saltados = 0
        for xml_file in xml_files:
            try:
                caf = CAF(xml_file)
            except Exception as e:
                print(f"  SKIP {xml_file.name}: {e}")
                saltados += 1
                continue

            # Verificar si ya existe
            existing = db.query(CafFolio).filter(
                CafFolio.empresa_id == empresa.id,
                CafFolio.tipo_dte == caf.tipo_dte,
                CafFolio.rango_desde == caf.folio_desde,
                CafFolio.rango_hasta == caf.folio_hasta,
            ).first()

            if existing:
                print(f"  EXISTE T{caf.tipo_dte} {caf.folio_desde}-{caf.folio_hasta} (skip)")
                saltados += 1
                continue

            # Determinar folio_actual
            consumido = folios_consumidos.get(str(caf.tipo_dte), caf.folio_desde)
            if consumido > caf.folio_hasta:
                folio_actual = caf.folio_hasta + 1
                estado = "agotado"
            elif consumido >= caf.folio_desde:
                folio_actual = consumido
                estado = "activo"
            else:
                folio_actual = caf.folio_desde
                estado = "activo"

            # Leer XML raw
            xml_raw = xml_file.read_bytes()

            row = CafFolio(
                empresa_id=empresa.id,
                tipo_dte=caf.tipo_dte,
                rango_desde=caf.folio_desde,
                rango_hasta=caf.folio_hasta,
                folio_actual=folio_actual,
                caf_xml_raw=xml_raw.decode("ISO-8859-1"),
                rut_emisor=caf.rut_emisor,
                fecha_autorizacion=caf.fecha_autorizacion,
                estado=estado,
            )
            db.add(row)
            migrados += 1
            print(f"  OK  T{caf.tipo_dte} {caf.folio_desde:>3}-{caf.folio_hasta:>3} folio_actual={folio_actual} ({estado}) [{xml_file.name}]")

        db.commit()
        print(f"\nMigración completada: {migrados} CAFs migrados, {saltados} saltados")

        # 4. Verificar estado final
        print("\n── Estado final en DB ──")
        from crumbpos.core.caf.caf_manager_db import CAFManagerDB
        mgr = CAFManagerDB(db, empresa.id)
        for f in mgr.estado_folios():
            print(f"  T{f['tipo_dte']:>2} | {f['nombre']:40} | Prox: {f['folio_actual']:>3} | "
                  f"Rango: {f['folio_min']}-{f['folio_max']} | Disp: {f['disponibles']:>3} | {f['alerta'].upper()}")

    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python -m crumbpos.scripts.migrar_cafs_a_db <RUT_EMPRESA>")
        print("Ejemplo: python -m crumbpos.scripts.migrar_cafs_a_db 77829149-5")
        sys.exit(1)
    migrar(sys.argv[1])
