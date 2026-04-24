"""Servicio de gestión del logo de empresa.

El logo se usa en la representación impresa de los DTEs (factura, boleta,
guía, etc.). El cliente lo sube desde la consola master cliente —
``/{rut}/negocio`` — y queda disponible para el renderer sin intervención
manual.

**Almacenamiento.** El archivo vive en el filesystem bajo
``DATA_DIR/{rut}/logo.png`` — siempre PNG, siempre ese nombre. La columna
``empresa.logo_url`` guarda el path relativo (``{rut}/logo.png``) o
``None`` si la empresa aún no subió logo. El path absoluto se resuelve
en tiempo de render con :func:`resolver_logo_path`.

**Por qué filesystem y no blob en DB.** El cert digital sí va en DB
(``cert_data`` base64) porque cualquier replica debe poder firmar. El
logo es cosmético: perderlo temporalmente no rompe la emisión (el
renderer cae al logo default ``crumbpos/config/logo.png``). Mantenerlo
fuera de la DB evita que cada ``SELECT * FROM empresa`` arrastre KBs
de binario innecesariamente.

**Por qué siempre PNG.** Unificar formato simplifica el consumer: el
renderer hace ``PIL.open(path)`` y punto, sin ramificación por MIME.
Si el cliente sube JPG, :func:`guardar_logo` lo re-codifica a PNG; si
sube PNG con canal alpha, se preserva.

**Dimensión máxima 800 px.** El recuadro del logo en la hoja carta mide
~53 × 26 mm (≈ 630 × 310 px a 300 dpi). 800 px de lado mayor da margen
para pantalla y mantiene el archivo bajo ~200 KB típicamente. Si el
cliente sube una imagen más grande, se redimensiona preservando aspect
ratio; si es más chica, se respeta tal cual (no upscale).
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image

from crumbpos.config.settings import DATA_DIR

logger = logging.getLogger(__name__)

# ── Límites y formatos aceptados ────────────────────────────────────

#: Tamaño máximo del upload en bytes (2 MB). PIL después baja esto a
#: ~100–200 KB post-compresión, pero rechazamos en la puerta para no
#: cargar basura en memoria.
MAX_LOGO_BYTES = 2 * 1024 * 1024

#: Lado mayor máximo al que redimensionamos el logo. Preserva aspect
#: ratio. Ver docstring del módulo para la justificación del número.
MAX_LOGO_DIM = 800

#: Content-Types aceptados por el endpoint de upload.
MIME_ALLOWED = frozenset({"image/png", "image/jpeg", "image/jpg"})

#: Nombre canónico del archivo bajo ``DATA_DIR/{rut}/``.
LOGO_FILENAME = "logo.png"


# ── API pública ─────────────────────────────────────────────────────


class LogoValidationError(ValueError):
    """El binario subido no cumple las reglas del logo.

    El router lo traduce a HTTP 400 con el mensaje literal.
    """


def guardar_logo(rut: str, contenido: bytes) -> str:
    """Guarda el logo de la empresa ``rut`` en disco.

    Valida tamaño y formato, redimensiona si excede ``MAX_LOGO_DIM``,
    normaliza siempre a PNG y escribe en ``DATA_DIR/{rut}/logo.png``.
    Si ya existía un logo, lo sobreescribe.

    Parameters
    ----------
    rut:
        RUT de la empresa (con guión y DV), por ejemplo ``77829149-5``.
        Determina el subdirectorio bajo ``DATA_DIR``.
    contenido:
        Bytes crudos del upload — el router ya hizo ``await file.read()``.

    Returns
    -------
    str
        Path relativo a ``DATA_DIR`` en formato posix — listo para
        persistir en ``empresa.logo_url``. Por ejemplo:
        ``"77829149-5/logo.png"``.

    Raises
    ------
    LogoValidationError
        Si el archivo excede ``MAX_LOGO_BYTES``, viene vacío, o PIL no
        puede abrirlo como imagen. El caller traduce a HTTP 400.
    """
    if not contenido:
        raise LogoValidationError("Archivo vacío")
    if len(contenido) > MAX_LOGO_BYTES:
        limite_mb = MAX_LOGO_BYTES // (1024 * 1024)
        raise LogoValidationError(
            f"El logo pesa {len(contenido) // 1024} KB; el máximo son "
            f"{limite_mb} MB. Reducí la imagen antes de subirla.",
        )

    # PIL es permisivo con el formato; rechaza lo que no es imagen.
    try:
        img = Image.open(io.BytesIO(contenido))
        img.load()  # forzar decode acá para capturar errores de archivo corrupto
    except Exception as e:
        raise LogoValidationError(
            f"No se pudo abrir el archivo como imagen: {e}",
        ) from e

    # Redimensión: solo si excede. No upscale.
    if max(img.size) > MAX_LOGO_DIM:
        img.thumbnail((MAX_LOGO_DIM, MAX_LOGO_DIM), Image.LANCZOS)

    # Normalizar a modo que PNG soporta sin rarezas. RGBA preserva
    # transparencia; LA la convierte a RGBA por consistencia.
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.mode else "RGB")

    # Crear directorio del tenant si no existe.
    tenant_dir = DATA_DIR / rut
    tenant_dir.mkdir(parents=True, exist_ok=True)

    destino = tenant_dir / LOGO_FILENAME
    img.save(destino, format="PNG", optimize=True)
    logger.info(
        "logo empresa=%s guardado en %s (%d bytes entrada → %d bytes salida)",
        rut, destino, len(contenido), destino.stat().st_size,
    )

    # Path relativo posix para persistir en DB. Usamos forward slashes
    # incluso en Windows para tener un valor estable cross-platform.
    return f"{rut}/{LOGO_FILENAME}"


def eliminar_logo(rut: str) -> bool:
    """Borra el archivo de logo de la empresa ``rut``.

    Returns
    -------
    bool
        ``True`` si el archivo existía y fue borrado, ``False`` si no
        existía (idempotente — no falla si ya estaba limpio).
    """
    destino = DATA_DIR / rut / LOGO_FILENAME
    if not destino.exists():
        return False
    try:
        destino.unlink()
        logger.info("logo empresa=%s eliminado (%s)", rut, destino)
        return True
    except OSError as e:
        logger.error("No se pudo eliminar logo %s: %s", destino, e)
        raise


def resolver_logo_path(logo_url: str | None) -> str:
    """Traduce ``empresa.logo_url`` a un path absoluto consumible.

    El renderer de DTEs (``crumbpos/core/impresion/formato_carta.py``)
    espera un path string que puede pasar a ``PIL.Image.open``. Esta
    función resuelve el path relativo almacenado en DB contra
    ``DATA_DIR`` y valida que el archivo exista en disco.

    Parameters
    ----------
    logo_url:
        Valor de ``empresa.logo_url``. ``None`` o string vacío significa
        que la empresa no subió logo.

    Returns
    -------
    str
        Path absoluto si el archivo existe, o ``""`` en caso contrario.
        El renderer interpreta ``""`` como "usar el logo default".
    """
    if not logo_url:
        return ""
    # Defensa contra paths absolutos o con traversal que pudieran haber
    # quedado en DB de una versión anterior. Solo aceptamos paths
    # relativos que resuelvan dentro de DATA_DIR.
    candidato = (DATA_DIR / logo_url).resolve()
    try:
        candidato.relative_to(DATA_DIR.resolve())
    except ValueError:
        logger.warning(
            "logo_url=%r intenta salir de DATA_DIR; se ignora",
            logo_url,
        )
        return ""
    if not candidato.exists():
        return ""
    return str(candidato)


def path_absoluto_logo(rut: str) -> Path:
    """Path absoluto canónico del logo de una empresa (exista o no).

    Útil para el endpoint ``GET /mi-empresa/logo`` que construye el
    :class:`FileResponse` antes de validar existencia.
    """
    return DATA_DIR / rut / LOGO_FILENAME
