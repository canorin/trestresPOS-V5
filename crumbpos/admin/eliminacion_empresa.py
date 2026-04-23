"""Baja de empresas — exportación a ZIP + papelera + eliminación definitiva.

EXCEPCIÓN NARROW-SCOPED a R4
════════════════════════════
Este archivo es el ÚNICO lugar del repo autorizado a mover o borrar
directorios dentro de `data/`. Todo el resto del core accede a los
archivos de empresa exclusivamente vía sesiones SQLAlchemy del modelo.

La excepción está regulada por tres cosas:

1.  **Guard obligatorio**: toda función destructiva (confirmar_baja,
    eliminar_definitivo) llama `_verificar_zip_descargado_o_error(rut)`
    como **primera línea ejecutable** del cuerpo. Sin esa llamada la
    operación aborta. El test `test_R4_narrow_eliminacion_tiene_guard`
    verifica esto vía AST — no muevas el guard sin actualizar el test.

2.  **Listado de operaciones permitidas**: solo `shutil.move` y
    `shutil.rmtree`, solo sobre directorios cuya ruta absoluta
    está contenida en `DATA_DIR`. Nunca se abren ni mutan archivos
    `.db` individuales — siempre se mueve el directorio entero
    `data/{rut}/` que contiene cert + prod juntos. Esto preserva la
    R1 (no bifurcar por ambiente): la unidad atómica es "la empresa",
    no "un ambiente de la empresa".

3.  **Garantía de ZIP previo**: antes de cualquier operación
    destructiva el campo `empresa_registro.zip_descargado_sha256` tiene
    que estar seteado con el sha256 del ZIP que el super admin ya
    descargó. Esto da una traza auditable: nunca destruimos datos sin
    haber puesto antes una copia en manos del cliente.

Flujo de baja típico
────────────────────
1. Super admin → `preparar_baja(rut)` → resumen (cuántos DTEs, libros, etc.)
2. Super admin → `exportar_zip(rut, user)` → genera `.exports/{rut}/...zip`
3. Super admin descarga el ZIP vía el endpoint de streaming.
4. Super admin → `confirmar_baja(rut, user, sha256)` → soft-delete:
   empresa_registro.estado='eliminada_soft', data/{rut}/ movido a .trash/.
5. Dentro de 30 días: puede llamar `restaurar(rut, user)` para deshacer.
6. Pasados 30 días: el super admin puede (no está obligado) llamar
   `eliminar_definitivo(rut, user)` → hard-delete: `.trash/{rut}_*`
   borrado del disco, registro queda como tombstone inmutable.

Qué entra al ZIP y qué no
─────────────────────────
Entra (solo producción real, nada de certificación ficticia):
  - Datos legales de la empresa (RUT, razón social, giro, direcciones)
  - Sucursales
  - DTEs emitidos (xml_firmado + pdf si existe en disco)
  - Libros generados (ventas, compras, guías)
  - RCOFs diarios
  - CAFs usados (como histórico)

NO entra:
  - certificado digital .p12 + password (es credencial, no archivo tributario)
  - Credenciales SII
  - Inventario, productos, stock, bodegas
  - Clientes, familias, catálogo
  - Ventas no facturadas, sesiones de caja, arqueos
  - certificacion.db entera (son documentos ficticios)
  - Usuarios, contraseñas
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from crumbpos.db.multi_tenant import (
    DATA_DIR,
    ELIMINACION_GRACIA_DIAS,
    EmpresaEliminacionLog,
    EmpresaRegistro,
    get_empresa_db_session,
    get_master_session,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# UBICACIONES EN DISCO
# ══════════════════════════════════════════════════════════════════

# Raíz de los ZIPs exportados por empresa. Los archivos acá son
# efímeros — se conservan hasta que el super admin confirma la baja
# o hasta que pide un nuevo export.
ZIP_EXPORT_ROOT = DATA_DIR / ".exports"

# Papelera — data/.trash/{rut}_{YYYYMMDD_HHMMSS}/. Cada baja soft
# crea un subdirectorio nuevo con timestamp, así nunca se pisa un
# snapshot anterior por error.
TRASH_ROOT = DATA_DIR / ".trash"


def _empresa_data_dir(rut: str) -> Path:
    """Directorio en disco que contiene ambas BDs de la empresa."""
    return DATA_DIR / rut.strip()


def _trash_dir_for(rut: str, timestamp: datetime) -> Path:
    """Nombre del subdirectorio en la papelera para esta baja."""
    stamp = timestamp.strftime("%Y%m%d_%H%M%S")
    return TRASH_ROOT / f"{rut.strip()}_{stamp}"


def _zip_dir_for(rut: str) -> Path:
    """Directorio que almacena los ZIPs exportados de esta empresa."""
    return ZIP_EXPORT_ROOT / rut.strip()


# ══════════════════════════════════════════════════════════════════
# GUARD DE R4 — obligatorio antes de toda operación destructiva
# ══════════════════════════════════════════════════════════════════

def _verificar_zip_descargado_o_error(rut: str) -> None:
    """Aborta si no hay ZIP de respaldo registrado para la empresa.

    **NO MOVER ESTA FUNCIÓN ni renombrarla sin actualizar el test
    `test_R4_narrow_eliminacion_tiene_guard` en
    tests/test_invariantes_produccion.py.** El test hace un chequeo
    AST: cada función destructiva de este módulo debe tener como
    **primera línea ejecutable** una llamada a esta función.

    El guard fuerza la invariante central de R4 narrow-scoped:
    nunca borramos datos sin que antes exista un ZIP en manos del
    cliente, verificado por sha256.
    """
    master = get_master_session()
    try:
        reg = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if reg is None:
            raise RuntimeError(
                f"Empresa {rut} no está registrada en master.db. "
                f"No se pueden ejecutar operaciones de baja sobre "
                f"una empresa que no existe."
            )
        if not reg.zip_descargado_sha256:
            raise RuntimeError(
                f"Empresa {rut}: no hay ZIP de respaldo descargado. "
                f"Antes de confirmar una baja hay que llamar "
                f"exportar_zip() y confirmar el sha256 del archivo "
                f"que descargó el super admin."
            )
    finally:
        master.close()


# ══════════════════════════════════════════════════════════════════
# HELPERS PRIVADOS
# ══════════════════════════════════════════════════════════════════

def _calcular_sha256(path: Path, chunk: int = 65536) -> str:
    """Calcula sha256 de un archivo. Usa chunks para no cargar todo en RAM."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _log_evento(
    rut: str,
    evento: str,
    user_id: str,
    user_email: str | None,
    detalle: dict[str, Any] | None = None,
) -> None:
    """Agrega una fila a empresa_eliminacion_log. Nunca actualiza; siempre append."""
    master = get_master_session()
    try:
        master.add(EmpresaEliminacionLog(
            id=str(uuid.uuid4()),
            empresa_rut=rut,
            evento=evento,
            user_id=user_id,
            user_email=user_email,
            timestamp=datetime.utcnow(),
            detalle_json=json.dumps(detalle or {}, default=str),
        ))
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()


def _leer_registro_empresa(rut: str) -> EmpresaRegistro:
    """Lee el registro de la empresa desde master.db. Error si no existe."""
    master = get_master_session()
    try:
        reg = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if reg is None:
            raise ValueError(f"Empresa {rut} no existe en master.db")
        # Desacoplar del session para que sea utilizable fuera del with.
        master.expunge(reg)
        return reg
    finally:
        master.close()


def _contar_documentos_produccion(rut: str) -> dict[str, int]:
    """Conteos para mostrar en el resumen de baja.

    Cuenta desde produccion.db. Si la empresa nunca llegó a producción
    (produccion.db vacía), los conteos son 0 — y el ZIP resultante va
    a ser casi vacío, lo cual es correcto: no hay archivo tributario
    que respaldar.
    """
    from crumbpos.db.models import DteEmitido, LibroGenerado, CafFolio, RcofDiario

    out = {"dtes": 0, "libros": 0, "cafs": 0, "rcofs": 0}
    try:
        db = get_empresa_db_session(rut, "produccion")
        try:
            out["dtes"] = db.query(DteEmitido).count()
            out["libros"] = db.query(LibroGenerado).count()
            out["cafs"] = db.query(CafFolio).count()
            out["rcofs"] = db.query(RcofDiario).count()
        finally:
            db.close()
    except Exception as exc:
        logger.warning(
            "No se pudo contar documentos de producción de %s: %s", rut, exc,
        )
    return out


# ══════════════════════════════════════════════════════════════════
# CONSTRUCCIÓN DEL ZIP
# ══════════════════════════════════════════════════════════════════

_README_CONTENT = """# Respaldo de empresa — trestresPOS

Este archivo contiene la información tributaria de la empresa exportada
desde trestresPOS antes de su baja del sistema.

## Qué contiene

- **empresa/**: datos legales e información de sucursales.
- **facturacion/dtes/**: todos los documentos tributarios electrónicos
  emitidos, organizados por tipo (T33 = factura electrónica,
  T39 = boleta electrónica, etc.). Cada DTE tiene su XML firmado y,
  si existe, su PDF impreso.
- **facturacion/libros/**: libros de compra, venta y guías generados.
- **facturacion/rcof/**: reportes de consumo de folios diarios.
- **facturacion/cafs/**: archivos CAF autorizados por el SII que se
  usaron durante la operación. Son histórico — los folios ya están
  consumidos y no se pueden reutilizar.
- **auditoria/**: metadata del evento de baja y manifest sha256
  para verificación de integridad.

## Qué NO contiene

Ver el archivo `NO_INCLUIDO.txt` para el detalle. En resumen: no se
respalda el certificado digital .p12, las credenciales del SII, ni
información operativa (inventario, ventas no facturadas, usuarios,
catálogo). Esa información se considera del software, no del cliente.

## Conservación legal

La ley chilena obliga a conservar los DTEs emitidos y los libros
por 6 años. Este ZIP cubre esa obligación — guárdalo en un lugar
seguro. El sha256 de cada archivo está en `auditoria/manifest.sha256`
por si necesitas verificar la integridad.
"""

_NO_INCLUIDO_CONTENT = """Qué NO se incluye en este respaldo
═══════════════════════════════════

Las siguientes categorías de datos NO se exportan al ZIP de baja:

• Certificado digital .p12 y su contraseña
  Razón: es una credencial, no un archivo tributario. El cliente ya
  tiene copia de su certificado (lo subió desde su propia fuente).

• Credenciales del SII (usuario, clave, clave tributaria)
  Razón: son del cliente y él las conoce por separado.

• Inventario, stock, bodegas, movimientos de stock
  Razón: es información operativa del software, no parte del
  archivo tributario que obliga el SII.

• Clientes maestros, artículos, familias, precios
  Razón: ídem. El catálogo se rearma en el nuevo sistema del cliente.

• Ventas no facturadas, sesiones de caja, arqueos
  Razón: son datos de proceso interno, no tributarios.

• Usuarios y contraseñas del POS
  Razón: seguridad. Las contraseñas son hashes que no sirven para
  recuperarse en otro sistema.

• Base de datos de certificación (certificacion.db)
  Razón: contiene documentos ficticios emitidos contra el ambiente
  de prueba del SII, sin validez tributaria. No son del cliente.

• Logs del servidor, crash dumps, métricas
  Razón: operativos del servidor, no del cliente.
"""


def _escribir_empresa_info(
    zf: zipfile.ZipFile,
    rut: str,
    manifest: dict[str, str],
) -> None:
    """Escribe datos legales y sucursales al ZIP desde producción."""
    from crumbpos.db.models import Empresa, Sucursal

    empresa_data: dict[str, Any] = {"rut": rut}
    sucursales_data: list[dict[str, Any]] = []
    try:
        db = get_empresa_db_session(rut, "produccion")
        try:
            e = db.query(Empresa).filter(Empresa.rut == rut).first()
            if e is not None:
                empresa_data = {
                    "rut": e.rut,
                    "razon_social": e.razon_social,
                    "nombre_fantasia": e.nombre_fantasia,
                    "giro": e.giro,
                    "acteco": e.acteco,
                    "direccion": e.direccion,
                    "comuna": e.comuna,
                    "ciudad": e.ciudad,
                    "fecha_resolucion": e.fecha_resolucion,
                    "numero_resolucion": e.numero_resolucion,
                    "cert_rut_firmante": e.cert_rut_firmante,
                    "tasa_iva": e.tasa_iva,
                    "created_at": (
                        e.created_at.isoformat() if e.created_at else None
                    ),
                }
                sucursales = db.query(Sucursal).filter(
                    Sucursal.empresa_id == e.id,
                ).all()
                sucursales_data = [
                    {
                        "nombre": s.nombre,
                        "codigo": s.codigo,
                        "direccion": s.direccion,
                        "comuna": s.comuna,
                        "ciudad": s.ciudad,
                        "sii_sucursal": s.sii_sucursal,
                        "activa": s.activa,
                    }
                    for s in sucursales
                ]
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Exportando info empresa %s: %s", rut, exc)

    _add_json_to_zip(zf, "empresa/datos_legales.json", empresa_data, manifest)
    _add_json_to_zip(zf, "empresa/sucursales.json", sucursales_data, manifest)


def _escribir_dtes(
    zf: zipfile.ZipFile,
    rut: str,
    manifest: dict[str, str],
) -> int:
    """Escribe todos los DTEs emitidos al ZIP. Retorna cantidad."""
    from crumbpos.db.models import DteEmitido

    index: list[dict[str, Any]] = []
    count = 0
    try:
        db = get_empresa_db_session(rut, "produccion")
        try:
            dtes = db.query(DteEmitido).order_by(
                DteEmitido.tipo_dte, DteEmitido.folio,
            ).all()
            for d in dtes:
                tipo_label = f"T{d.tipo_dte:02d}"
                folio_label = f"F{d.folio:06d}"
                xml_arcname = f"facturacion/dtes/{tipo_label}/{tipo_label}_{folio_label}.xml"
                if d.xml_firmado:
                    _add_text_to_zip(zf, xml_arcname, d.xml_firmado, manifest)
                pdf_written = None
                if d.pdf_path:
                    pdf_src = Path(d.pdf_path)
                    if pdf_src.exists() and pdf_src.is_file():
                        pdf_arcname = (
                            f"facturacion/dtes/{tipo_label}/"
                            f"{tipo_label}_{folio_label}.pdf"
                        )
                        _add_file_to_zip(zf, pdf_arcname, pdf_src, manifest)
                        pdf_written = pdf_arcname
                index.append({
                    "tipo_dte": d.tipo_dte,
                    "folio": d.folio,
                    "fecha_emision": (
                        d.fecha_emision.isoformat() if d.fecha_emision else None
                    ),
                    "receptor_rut": d.receptor_rut,
                    "receptor_razon": d.receptor_razon,
                    "monto_neto": d.monto_neto,
                    "monto_exento": d.monto_exento,
                    "iva": d.iva,
                    "monto_total": d.monto_total,
                    "track_id": d.track_id,
                    "estado_sii": d.estado_sii,
                    "glosa_sii": d.glosa_sii,
                    "xml_path": xml_arcname if d.xml_firmado else None,
                    "pdf_path": pdf_written,
                })
                count += 1
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Exportando DTEs de %s: %s", rut, exc)

    _add_json_to_zip(zf, "facturacion/dtes/index.json", index, manifest)
    return count


def _escribir_libros(
    zf: zipfile.ZipFile,
    rut: str,
    manifest: dict[str, str],
) -> int:
    """Escribe libros generados al ZIP. Retorna cantidad."""
    from crumbpos.db.models import LibroGenerado

    index: list[dict[str, Any]] = []
    count = 0
    try:
        db = get_empresa_db_session(rut, "produccion")
        try:
            libros = db.query(LibroGenerado).order_by(
                LibroGenerado.periodo, LibroGenerado.tipo_libro,
            ).all()
            for l in libros:
                arcname = f"facturacion/libros/{l.tipo_libro}_{l.periodo}.xml"
                if l.xml_firmado:
                    _add_text_to_zip(zf, arcname, l.xml_firmado, manifest)
                index.append({
                    "tipo_libro": l.tipo_libro,
                    "periodo": l.periodo,
                    "track_id": l.track_id,
                    "estado_sii": l.estado_sii,
                    "xml_path": arcname if l.xml_firmado else None,
                    "resumen": json.loads(l.resumen_json) if l.resumen_json else None,
                })
                count += 1
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Exportando libros de %s: %s", rut, exc)

    _add_json_to_zip(zf, "facturacion/libros/index.json", index, manifest)
    return count


def _escribir_rcof(
    zf: zipfile.ZipFile,
    rut: str,
    manifest: dict[str, str],
) -> int:
    """Escribe RCOFs diarios al ZIP. Retorna cantidad."""
    from crumbpos.db.models import RcofDiario

    index: list[dict[str, Any]] = []
    count = 0
    try:
        db = get_empresa_db_session(rut, "produccion")
        try:
            rcofs = db.query(RcofDiario).order_by(RcofDiario.fecha).all()
            for r in rcofs:
                fecha_str = r.fecha.isoformat() if r.fecha else "sinfecha"
                arcname = f"facturacion/rcof/{r.sucursal_id}_{fecha_str}.xml"
                if r.xml_firmado:
                    _add_text_to_zip(zf, arcname, r.xml_firmado, manifest)
                index.append({
                    "sucursal_id": r.sucursal_id,
                    "fecha": fecha_str,
                    "track_id": r.track_id,
                    "estado_sii": r.estado_sii,
                    "resumen": r.resumen,
                    "xml_path": arcname if r.xml_firmado else None,
                })
                count += 1
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Exportando RCOFs de %s: %s", rut, exc)

    _add_json_to_zip(zf, "facturacion/rcof/index.json", index, manifest)
    return count


def _escribir_cafs(
    zf: zipfile.ZipFile,
    rut: str,
    manifest: dict[str, str],
) -> int:
    """Escribe CAFs al ZIP. Retorna cantidad."""
    from crumbpos.db.models import CafFolio

    index: list[dict[str, Any]] = []
    count = 0
    try:
        db = get_empresa_db_session(rut, "produccion")
        try:
            cafs = db.query(CafFolio).order_by(
                CafFolio.tipo_dte, CafFolio.rango_desde,
            ).all()
            for c in cafs:
                tipo_label = f"T{c.tipo_dte:02d}"
                arcname = (
                    f"facturacion/cafs/{tipo_label}_"
                    f"{c.rango_desde:06d}-{c.rango_hasta:06d}.xml"
                )
                if c.caf_xml_raw:
                    # caf_xml_raw puede venir como str o bytes dependiendo del driver.
                    if isinstance(c.caf_xml_raw, bytes):
                        _add_bytes_to_zip(zf, arcname, c.caf_xml_raw, manifest)
                    else:
                        _add_text_to_zip(zf, arcname, c.caf_xml_raw, manifest)
                index.append({
                    "tipo_dte": c.tipo_dte,
                    "rango_desde": c.rango_desde,
                    "rango_hasta": c.rango_hasta,
                    "folio_actual": c.folio_actual,
                    "estado": c.estado,
                    "fecha_autorizacion": c.fecha_autorizacion,
                    "rut_emisor": c.rut_emisor,
                    "xml_path": arcname if c.caf_xml_raw else None,
                })
                count += 1
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Exportando CAFs de %s: %s", rut, exc)

    _add_json_to_zip(zf, "facturacion/cafs/index.json", index, manifest)
    return count


def _add_text_to_zip(
    zf: zipfile.ZipFile,
    arcname: str,
    content: str,
    manifest: dict[str, str],
) -> None:
    data = content.encode("utf-8")
    zf.writestr(arcname, data)
    manifest[arcname] = hashlib.sha256(data).hexdigest()


def _add_bytes_to_zip(
    zf: zipfile.ZipFile,
    arcname: str,
    data: bytes,
    manifest: dict[str, str],
) -> None:
    zf.writestr(arcname, data)
    manifest[arcname] = hashlib.sha256(data).hexdigest()


def _add_json_to_zip(
    zf: zipfile.ZipFile,
    arcname: str,
    obj: Any,
    manifest: dict[str, str],
) -> None:
    data = json.dumps(obj, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    zf.writestr(arcname, data)
    manifest[arcname] = hashlib.sha256(data).hexdigest()


def _add_file_to_zip(
    zf: zipfile.ZipFile,
    arcname: str,
    src: Path,
    manifest: dict[str, str],
) -> None:
    data = src.read_bytes()
    zf.writestr(arcname, data)
    manifest[arcname] = hashlib.sha256(data).hexdigest()


def _construir_zip(
    rut: str,
    destino: Path,
    admin_user_id: str,
    admin_email: str | None,
) -> dict[str, Any]:
    """Arma el ZIP completo. Retorna resumen de conteos."""
    manifest: dict[str, str] = {}
    resumen = {"dtes": 0, "libros": 0, "rcofs": 0, "cafs": 0}
    destino.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as zf:
        _add_text_to_zip(zf, "README.md", _README_CONTENT, manifest)
        _add_text_to_zip(zf, "NO_INCLUIDO.txt", _NO_INCLUIDO_CONTENT, manifest)
        _escribir_empresa_info(zf, rut, manifest)
        resumen["dtes"] = _escribir_dtes(zf, rut, manifest)
        resumen["libros"] = _escribir_libros(zf, rut, manifest)
        resumen["rcofs"] = _escribir_rcof(zf, rut, manifest)
        resumen["cafs"] = _escribir_cafs(zf, rut, manifest)
        # Metadata del evento
        eliminacion_meta = {
            "rut": rut,
            "exportado_at": datetime.utcnow().isoformat() + "Z",
            "exportado_por": {
                "user_id": admin_user_id,
                "email": admin_email,
            },
            "software": "trestresPOS",
            "conteos": resumen,
        }
        _add_json_to_zip(
            zf, "auditoria/eliminacion.json", eliminacion_meta, manifest,
        )
        # Manifest de integridad — se agrega al final sin incluirse a sí mismo.
        manifest_text = "\n".join(
            f"{sha}  {name}" for name, sha in sorted(manifest.items())
        ) + "\n"
        # NO incluir el propio manifest en el manifest (auto-referencial).
        zf.writestr("auditoria/manifest.sha256", manifest_text.encode("utf-8"))

    return resumen


# ══════════════════════════════════════════════════════════════════
# OPERACIONES PÚBLICAS
# ══════════════════════════════════════════════════════════════════

def preparar_baja(rut: str) -> dict[str, Any]:
    """Resumen pre-baja — solo lectura, no modifica nada.

    Devuelve la info que el modal de confirmación necesita mostrar:
    razón social, conteos de documentos, estado actual, si ya hay un
    ZIP exportado previamente. No toca disco.
    """
    reg = _leer_registro_empresa(rut)
    conteos = _contar_documentos_produccion(rut)
    zip_dir = _zip_dir_for(rut)
    ultimo_zip = None
    if zip_dir.exists():
        zips = sorted(zip_dir.glob("*.zip"))
        if zips:
            last = zips[-1]
            ultimo_zip = {
                "nombre": last.name,
                "bytes": last.stat().st_size,
                "creado_at": datetime.fromtimestamp(
                    last.stat().st_mtime,
                ).isoformat(),
            }
    return {
        "rut": reg.rut,
        "razon_social": reg.razon_social,
        "estado": reg.estado,
        "etapa": reg.etapa,
        "ambiente_activo": reg.ambiente_activo,
        "conteos": conteos,
        "ultimo_zip_exportado": ultimo_zip,
        "zip_descargado_sha256": reg.zip_descargado_sha256,
        "fecha_eliminacion_soft": (
            reg.fecha_eliminacion_soft.isoformat()
            if reg.fecha_eliminacion_soft else None
        ),
        "puede_eliminarse_desde": (
            reg.puede_eliminarse_desde.isoformat()
            if reg.puede_eliminarse_desde else None
        ),
    }


def exportar_zip(
    rut: str,
    admin_user_id: str,
    admin_email: str | None = None,
) -> dict[str, Any]:
    """Genera el ZIP con todo el archivo tributario de la empresa.

    No es una operación destructiva — no borra nada ni cambia el estado
    de la empresa. Solo escribe un archivo nuevo en
    `data/.exports/{rut}/{rut}_{timestamp}.zip`. Se puede llamar varias
    veces sin problema; cada llamada genera un nuevo ZIP con timestamp
    distinto. El último ZIP generado es el que toma `confirmar_baja`
    como referencia para el sha256.
    """
    # Verifica que la empresa exista antes de empezar a construir nada.
    reg = _leer_registro_empresa(rut)
    if reg.estado == "eliminada_hard":
        raise RuntimeError(
            f"Empresa {rut} ya fue eliminada definitivamente. "
            f"No queda data en disco para exportar."
        )

    timestamp = datetime.utcnow()
    stamp = timestamp.strftime("%Y%m%d_%H%M%S")
    zip_dir = _zip_dir_for(rut)
    zip_dir.mkdir(parents=True, exist_ok=True)
    zip_path = zip_dir / f"{rut}_{stamp}.zip"

    resumen = _construir_zip(rut, zip_path, admin_user_id, admin_email)
    sha256 = _calcular_sha256(zip_path)
    bytes_ = zip_path.stat().st_size

    _log_evento(
        rut, "zip_exportado", admin_user_id, admin_email,
        detalle={
            "zip_path": str(zip_path),
            "sha256": sha256,
            "bytes": bytes_,
            "conteos": resumen,
        },
    )
    logger.info(
        "ZIP de baja exportado para %s: %s (%d bytes, sha256=%s)",
        rut, zip_path.name, bytes_, sha256[:12],
    )
    return {
        "rut": rut,
        "zip_path": str(zip_path),
        "zip_nombre": zip_path.name,
        "sha256": sha256,
        "bytes": bytes_,
        "conteos": resumen,
        "creado_at": timestamp.isoformat() + "Z",
    }


def leer_ultimo_zip(rut: str) -> Path | None:
    """Retorna el path del ZIP más reciente en `.exports/{rut}/`, o None."""
    zip_dir = _zip_dir_for(rut)
    if not zip_dir.exists():
        return None
    zips = sorted(zip_dir.glob("*.zip"))
    return zips[-1] if zips else None


def marcar_zip_descargado(
    rut: str,
    sha256: str,
    admin_user_id: str,
    admin_email: str | None = None,
) -> dict[str, Any]:
    """Confirma que el super admin descargó el ZIP con el sha256 esperado.

    Esto setea `empresa_registro.zip_descargado_sha256`, que es la
    condición que verifica `_verificar_zip_descargado_o_error` para
    permitir operaciones destructivas.

    Verifica que exista un ZIP en `.exports/{rut}/` con ese sha256.
    Si el sha256 no coincide con ningún ZIP exportado, aborta.
    """
    zip_dir = _zip_dir_for(rut)
    if not zip_dir.exists():
        raise RuntimeError(
            f"Empresa {rut}: no hay ZIPs exportados todavía. "
            f"Llamar exportar_zip() primero."
        )
    match = None
    for zf in zip_dir.glob("*.zip"):
        if _calcular_sha256(zf) == sha256:
            match = zf
            break
    if match is None:
        raise RuntimeError(
            f"Empresa {rut}: el sha256 recibido no coincide con "
            f"ningún ZIP exportado en {zip_dir}. Re-descargar y "
            f"verificar antes de confirmar la baja."
        )

    master = get_master_session()
    try:
        reg = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if reg is None:
            raise ValueError(f"Empresa {rut} no existe")
        reg.zip_descargado_sha256 = sha256
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()

    _log_evento(
        rut, "zip_exportado", admin_user_id, admin_email,
        detalle={
            "accion": "marcado_como_descargado",
            "sha256": sha256,
            "zip_path": str(match),
        },
    )
    return {"rut": rut, "sha256": sha256, "zip_path": str(match)}


def confirmar_baja(
    rut: str,
    admin_user_id: str,
    admin_email: str | None = None,
) -> dict[str, Any]:
    """OPERACIÓN DESTRUCTIVA — mueve data/{rut}/ a papelera.

    **Primera línea ejecutable**: `_verificar_zip_descargado_o_error`.
    Ver docstring del guard y del test AST en tests/.

    Flujo:
      1. Verifica que exista un ZIP descargado (guard).
      2. Mueve `data/{rut}/` → `data/.trash/{rut}_{timestamp}/`.
         Esto incluye cert + prod + cualquier otro archivo. Es un
         movimiento atómico a nivel de directorio.
      3. Marca `empresa_registro.estado='eliminada_soft'`, registra
         timestamps y `puede_eliminarse_desde = now + 30 días`.
      4. Loguea el evento en `empresa_eliminacion_log`.

    Es idempotente parcial: si una empresa ya está en estado
    'eliminada_soft' y hay un directorio `{rut}_*` en la papelera,
    este método aborta con error en vez de crear un segundo snapshot.
    """
    _verificar_zip_descargado_o_error(rut)

    reg = _leer_registro_empresa(rut)
    if reg.estado != "activa":
        raise RuntimeError(
            f"Empresa {rut} está en estado '{reg.estado}', no 'activa'. "
            f"Solo se puede dar de baja a empresas activas."
        )

    src = _empresa_data_dir(rut)
    if not src.exists():
        raise RuntimeError(
            f"Empresa {rut}: el directorio {src} no existe. "
            f"No hay archivos que mover a la papelera — algo está "
            f"inconsistente entre master.db y el filesystem."
        )

    timestamp = datetime.utcnow()
    trash_dir = _trash_dir_for(rut, timestamp)
    if trash_dir.exists():
        raise RuntimeError(
            f"Ya existe un snapshot en la papelera: {trash_dir}. "
            f"Esto no debería pasar — el timestamp debería ser único."
        )
    TRASH_ROOT.mkdir(parents=True, exist_ok=True)

    # MOVIMIENTO ATÓMICO — toda la empresa, no un ambiente específico.
    shutil.move(str(src), str(trash_dir))

    # Ahora marcar estado en master.db.
    master = get_master_session()
    try:
        row = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if row is None:
            raise ValueError(f"Empresa {rut} desapareció de master.db")
        row.estado = "eliminada_soft"
        row.fecha_eliminacion_soft = timestamp
        row.puede_eliminarse_desde = timestamp + timedelta(
            days=ELIMINACION_GRACIA_DIAS,
        )
        row.eliminado_por_user_id = admin_user_id
        master.commit()
    except Exception:
        master.rollback()
        # Si no pudimos marcar en master, intentamos volver el directorio
        # para no quedar inconsistentes.
        try:
            shutil.move(str(trash_dir), str(src))
        except Exception as undo_err:
            logger.error(
                "FALLÓ rollback del move a trash de %s: %s", rut, undo_err,
            )
        raise
    finally:
        master.close()

    _log_evento(
        rut, "baja_soft", admin_user_id, admin_email,
        detalle={
            "trash_dir": str(trash_dir),
            "puede_eliminarse_desde": (
                timestamp + timedelta(days=ELIMINACION_GRACIA_DIAS)
            ).isoformat(),
        },
    )
    logger.info(
        "Empresa %s dada de baja (soft) → %s, puede eliminarse desde %s",
        rut, trash_dir, (timestamp + timedelta(days=ELIMINACION_GRACIA_DIAS)),
    )
    return {
        "rut": rut,
        "estado": "eliminada_soft",
        "trash_dir": str(trash_dir),
        "fecha_eliminacion_soft": timestamp.isoformat() + "Z",
        "puede_eliminarse_desde": (
            timestamp + timedelta(days=ELIMINACION_GRACIA_DIAS)
        ).isoformat() + "Z",
    }


def restaurar(
    rut: str,
    admin_user_id: str,
    admin_email: str | None = None,
) -> dict[str, Any]:
    """Revierte una baja soft: saca de la papelera y vuelve a activa.

    NO es destructiva — mueve `.trash/{rut}_*/` de vuelta a `data/{rut}/`.
    Por eso NO llama al guard `_verificar_zip_descargado_o_error`.
    """
    reg = _leer_registro_empresa(rut)
    if reg.estado != "eliminada_soft":
        raise RuntimeError(
            f"Empresa {rut} está en estado '{reg.estado}', no "
            f"'eliminada_soft'. Solo se pueden restaurar empresas "
            f"que están en la papelera."
        )

    # Buscar el snapshot en la papelera — debería haber uno.
    if not TRASH_ROOT.exists():
        raise RuntimeError(
            f"No existe {TRASH_ROOT}. El filesystem está en un estado "
            f"inesperado."
        )
    candidatos = sorted(TRASH_ROOT.glob(f"{rut}_*"))
    if not candidatos:
        raise RuntimeError(
            f"No se encontró snapshot de {rut} en la papelera. "
            f"Esto indica inconsistencia — la empresa está marcada "
            f"como eliminada_soft pero sus archivos no están en "
            f"{TRASH_ROOT}."
        )
    src = candidatos[-1]  # el más reciente
    dst = _empresa_data_dir(rut)
    if dst.exists():
        raise RuntimeError(
            f"El directorio destino {dst} ya existe. Alguien debe "
            f"haber recreado la empresa mientras estaba en la "
            f"papelera — resolver a mano antes de restaurar."
        )

    shutil.move(str(src), str(dst))

    master = get_master_session()
    try:
        row = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if row is None:
            raise ValueError(f"Empresa {rut} desapareció de master.db")
        row.estado = "activa"
        row.fecha_eliminacion_soft = None
        row.puede_eliminarse_desde = None
        row.eliminado_por_user_id = None
        # Dejamos `zip_descargado_sha256` seteado a propósito — si el
        # admin vuelve a dar de baja, puede verificar que aún tiene el
        # ZIP sin pedir uno nuevo. Si prefiere hacer uno nuevo,
        # `exportar_zip` sobrescribe.
        master.commit()
    except Exception:
        master.rollback()
        try:
            shutil.move(str(dst), str(src))
        except Exception as undo_err:
            logger.error(
                "FALLÓ rollback del restaurar de %s: %s", rut, undo_err,
            )
        raise
    finally:
        master.close()

    _log_evento(
        rut, "restaurada", admin_user_id, admin_email,
        detalle={"restaurado_desde": str(src), "restaurado_a": str(dst)},
    )
    logger.info("Empresa %s restaurada desde la papelera", rut)
    return {"rut": rut, "estado": "activa", "restaurado_desde": str(src)}


def eliminar_definitivo(
    rut: str,
    admin_user_id: str,
    admin_email: str | None = None,
) -> dict[str, Any]:
    """OPERACIÓN DESTRUCTIVA IRREVERSIBLE — borra datos del disco.

    **Primera línea ejecutable**: `_verificar_zip_descargado_o_error`.

    Requisitos:
      - Empresa debe estar en estado 'eliminada_soft'.
      - `puede_eliminarse_desde` debe haber pasado (≥30 días de gracia).
      - Debe existir un ZIP de respaldo verificado (lo valida el guard).

    Efecto:
      - `shutil.rmtree(data/.trash/{rut}_*)` — borra del disco.
      - `empresa_registro.estado='eliminada_hard'`,
        `fecha_eliminacion_hard=now`.
      - El registro queda como tombstone en master.db, visible solo
        para auditoría. NO se borra la fila porque sirve para
        demostrar "sí, esta empresa existió y fue dada de baja por
        X persona en tal fecha".
    """
    _verificar_zip_descargado_o_error(rut)

    reg = _leer_registro_empresa(rut)
    if reg.estado != "eliminada_soft":
        raise RuntimeError(
            f"Empresa {rut} está en estado '{reg.estado}', no "
            f"'eliminada_soft'. Solo se pueden eliminar definitivamente "
            f"empresas que están en la papelera."
        )
    if reg.puede_eliminarse_desde is None:
        raise RuntimeError(
            f"Empresa {rut}: el campo puede_eliminarse_desde está vacío. "
            f"No se puede calcular si pasó el período de gracia."
        )
    now = datetime.utcnow()
    if now < reg.puede_eliminarse_desde:
        falta = reg.puede_eliminarse_desde - now
        raise RuntimeError(
            f"Empresa {rut}: aún está dentro del período de gracia. "
            f"Falta(n) {falta.days} día(s) para poder eliminar "
            f"definitivamente. Fecha habilitada: "
            f"{reg.puede_eliminarse_desde.isoformat()}Z."
        )

    # Buscar y borrar los snapshots de la papelera.
    candidatos: list[Path] = []
    if TRASH_ROOT.exists():
        candidatos = sorted(TRASH_ROOT.glob(f"{rut}_*"))
    borrados: list[str] = []
    for dir_papelera in candidatos:
        if dir_papelera.is_dir():
            shutil.rmtree(dir_papelera)
            borrados.append(str(dir_papelera))
        elif dir_papelera.exists():
            # Archivo suelto por algún motivo — lo quitamos también.
            dir_papelera.unlink()
            borrados.append(str(dir_papelera))

    # Marcar tombstone en master.
    master = get_master_session()
    try:
        row = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if row is None:
            raise ValueError(f"Empresa {rut} desapareció de master.db")
        row.estado = "eliminada_hard"
        row.fecha_eliminacion_hard = now
        row.eliminado_por_user_id = admin_user_id
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()

    _log_evento(
        rut, "baja_hard", admin_user_id, admin_email,
        detalle={
            "borrados": borrados,
            "tombstone_at": now.isoformat() + "Z",
        },
    )
    logger.warning(
        "Empresa %s eliminada DEFINITIVAMENTE por %s — borrados: %s",
        rut, admin_email or admin_user_id, borrados,
    )
    return {
        "rut": rut,
        "estado": "eliminada_hard",
        "borrados": borrados,
        "fecha_eliminacion_hard": now.isoformat() + "Z",
    }


def listar_papelera_con_resumen() -> list[dict[str, Any]]:
    """Listado de la papelera para la UI, con días restantes de gracia."""
    from crumbpos.db.multi_tenant import listar_papelera as _listar_papelera

    out: list[dict[str, Any]] = []
    now = datetime.utcnow()
    for reg in _listar_papelera():
        dias_restantes: int | None = None
        puede_eliminarse_ya = False
        if reg.puede_eliminarse_desde is not None:
            delta = reg.puede_eliminarse_desde - now
            dias_restantes = max(0, delta.days + (1 if delta.seconds > 0 else 0))
            puede_eliminarse_ya = now >= reg.puede_eliminarse_desde
        out.append({
            "rut": reg.rut,
            "razon_social": reg.razon_social,
            "estado": reg.estado,
            "fecha_eliminacion_soft": (
                reg.fecha_eliminacion_soft.isoformat() + "Z"
                if reg.fecha_eliminacion_soft else None
            ),
            "puede_eliminarse_desde": (
                reg.puede_eliminarse_desde.isoformat() + "Z"
                if reg.puede_eliminarse_desde else None
            ),
            "dias_restantes": dias_restantes,
            "puede_eliminarse_ya": puede_eliminarse_ya,
            "eliminado_por_user_id": reg.eliminado_por_user_id,
        })
    return out
