"""Router de baja de empresas — super admin, destructivo.

Expone la Fase 7: exportar ZIP con el archivo tributario, mover a papelera,
restaurar desde papelera y (tras 30 días de gracia) eliminar definitivamente.

EXCEPCIÓN A R4
══════════════
Este router es un wrapper delgado sobre `crumbpos.admin.eliminacion_empresa`.
Toda la lógica destructiva vive en ese módulo — acá solo traducimos HTTP a
llamadas Python y de vuelta. El router en sí no toca `data/` directamente.

Las operaciones son todas super_admin-only vía `require_super_admin`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from crumbpos.admin import eliminacion_empresa
from crumbpos.api.dependencies import require_super_admin
from crumbpos.db.multi_tenant import UsuarioAuth

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/empresas",
    tags=["admin-empresas"],
)


# ══════════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════════

class ConfirmarBajaIn(BaseModel):
    """Body de POST /{rut}/confirmar-baja."""
    sha256: str
    # sha256 del ZIP que el super admin descargó, calculado del lado
    # del navegador. El servidor compara con el sha256 del último ZIP
    # exportado; si coinciden, marca zip_descargado_sha256 y procede
    # con la baja soft.


class EliminarDefinitivoIn(BaseModel):
    """Body de POST /{rut}/eliminar-definitivo."""
    rut_confirmacion: str
    # El super admin debe re-escribir el RUT exacto como doble confirmación.
    # Si no coincide con el rut del path, el endpoint aborta con 400.


class RutQuery(BaseModel):
    """Query schema de uso libre — placeholder."""
    pass


# ══════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════

@router.get("/papelera")
def listar_papelera(
    user: UsuarioAuth = Depends(require_super_admin),
):
    """Lista las empresas en papelera (soft-delete), con días restantes.

    Cada fila incluye `puede_eliminarse_ya` (bool) — la UI solo muestra
    el botón "Eliminar definitivamente" cuando está en true.
    """
    return eliminacion_empresa.listar_papelera_con_resumen()


@router.post("/{rut}/preparar-baja")
def preparar_baja(
    rut: str,
    user: UsuarioAuth = Depends(require_super_admin),
):
    """Resumen pre-baja — solo lectura. No modifica nada.

    Devuelve razón social, conteos de documentos en producción, estado
    actual, si hay un ZIP exportado previamente. Lo usa el modal de
    confirmación para mostrarle al admin qué está a punto de dar de baja.
    """
    try:
        return eliminacion_empresa.preparar_baja(rut)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        logger.exception("preparar_baja %s", rut)
        raise HTTPException(500, f"Error preparando baja: {exc}")


@router.post("/{rut}/exportar-zip")
def exportar_zip(
    rut: str,
    user: UsuarioAuth = Depends(require_super_admin),
):
    """Genera el ZIP con el archivo tributario de la empresa.

    No es destructivo — solo crea un archivo en `data/.exports/{rut}/`.
    La respuesta incluye `sha256`, `bytes` y `zip_nombre`; el front
    usa esa info para descargar el archivo vía GET /descargar-zip y
    luego confirmar con POST /confirmar-baja pasando el mismo sha256.

    Se puede llamar múltiples veces — cada llamada crea un nuevo ZIP
    con timestamp distinto. El último es el que toma `confirmar-baja`.
    """
    try:
        return eliminacion_empresa.exportar_zip(
            rut, user.id, user.email,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("exportar_zip %s", rut)
        raise HTTPException(500, f"Error exportando ZIP: {exc}")


@router.get("/{rut}/descargar-zip")
def descargar_zip(
    rut: str,
    user: UsuarioAuth = Depends(require_super_admin),
):
    """Descarga el ZIP más reciente generado para la empresa.

    Requiere que exista al menos un export previo (POST /exportar-zip).
    No modifica estado ni fija `zip_descargado_sha256` — eso pasa cuando
    el admin llama POST /confirmar-baja con el sha256.
    """
    zip_path = eliminacion_empresa.leer_ultimo_zip(rut)
    if zip_path is None or not zip_path.exists():
        raise HTTPException(
            404,
            f"Empresa {rut}: no hay ZIP exportado. Llamar "
            f"POST /api/admin/empresas/{rut}/exportar-zip primero.",
        )
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=zip_path.name,
    )


@router.post("/{rut}/confirmar-baja")
def confirmar_baja(
    rut: str,
    body: ConfirmarBajaIn,
    user: UsuarioAuth = Depends(require_super_admin),
):
    """OPERACIÓN DESTRUCTIVA — mueve la empresa a la papelera.

    Flujo:
      1. Verifica que `body.sha256` coincida con el sha256 del último
         ZIP exportado para esta empresa.
      2. Marca `empresa_registro.zip_descargado_sha256`.
      3. Mueve `data/{rut}/` → `data/.trash/{rut}_{timestamp}/`.
      4. Marca `empresa_registro.estado='eliminada_soft'` y deja un
         registro en `empresa_eliminacion_log`.

    La empresa desaparece del listado principal y aparece en la Papelera
    del super admin. Se puede restaurar dentro de 30 días.
    """
    try:
        eliminacion_empresa.marcar_zip_descargado(
            rut, body.sha256, user.id, user.email,
        )
        return eliminacion_empresa.confirmar_baja(rut, user.id, user.email)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("confirmar_baja %s", rut)
        raise HTTPException(500, f"Error confirmando baja: {exc}")


@router.post("/{rut}/restaurar")
def restaurar(
    rut: str,
    user: UsuarioAuth = Depends(require_super_admin),
):
    """Saca a la empresa de la papelera y la vuelve a 'activa'.

    No destructivo — mueve `data/.trash/{rut}_*/` de vuelta a
    `data/{rut}/`. Solo funciona si la empresa está en 'eliminada_soft';
    si ya fue eliminada definitivamente no hay vuelta atrás.
    """
    try:
        return eliminacion_empresa.restaurar(rut, user.id, user.email)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("restaurar %s", rut)
        raise HTTPException(500, f"Error restaurando: {exc}")


@router.post("/{rut}/eliminar-definitivo")
def eliminar_definitivo(
    rut: str,
    body: EliminarDefinitivoIn,
    user: UsuarioAuth = Depends(require_super_admin),
):
    """OPERACIÓN DESTRUCTIVA IRREVERSIBLE — borra archivos del disco.

    Requisitos:
      - `body.rut_confirmacion` debe ser exactamente igual al rut del path
        (doble confirmación manual, evita clicks accidentales).
      - Empresa debe estar en 'eliminada_soft'.
      - Debe haber pasado el período de gracia (30 días).
      - Debe existir el ZIP de respaldo registrado (lo verifica el guard
        del módulo eliminacion_empresa).

    El registro queda como tombstone en master.db con
    `estado='eliminada_hard'`, `fecha_eliminacion_hard` seteada.
    No hay vuelta atrás.
    """
    if body.rut_confirmacion.strip() != rut.strip():
        raise HTTPException(
            400,
            "El RUT de confirmación no coincide con el RUT a eliminar. "
            "Esto es una seguridad anti-click-accidental. Re-escribí el "
            "RUT exacto en el cuadro de confirmación.",
        )
    try:
        return eliminacion_empresa.eliminar_definitivo(
            rut, user.id, user.email,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("eliminar_definitivo %s", rut)
        raise HTTPException(500, f"Error eliminando definitivamente: {exc}")
