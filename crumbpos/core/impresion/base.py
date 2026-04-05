"""Clase base y modelo de datos compartido para impresión de DTEs."""
import os
import re
import tempfile
from dataclasses import dataclass, field

from fpdf import FPDF
from pdf417 import encode, render_image
from num2words import num2words

from crumbpos.config import settings


# Nombres de tipos de DTE para impresión
TIPO_NOMBRE = {
    33: ("FACTURA", "ELECTRONICA"),
    34: ("FACTURA NO AFECTA", "O EXENTA ELECTRONICA"),
    39: ("BOLETA", "ELECTRONICA"),
    41: ("BOLETA EXENTA", "ELECTRONICA"),
    46: ("FACTURA DE COMPRA", "ELECTRONICA"),
    52: ("GUIA DE DESPACHO", "ELECTRONICA"),
    56: ("NOTA DE DEBITO", "ELECTRONICA"),
    61: ("NOTA DE CREDITO", "ELECTRONICA"),
    801: ("ORDEN DE COMPRA", ""),
}

# Tipos que tienen copia cedible
TIPOS_CEDIBLES = {33, 34, 52}

# Sucursal SII (aparece bajo el recuadro rojo)
SUCURSAL_SII = "SANTIAGO ORIENTE"

# Logo por defecto
LOGO_PATH = str(settings.BASE_DIR / "crumbpos" / "config" / "logo.png")


@dataclass
class DTEPrintData:
    """Datos de un DTE para impresión.

    Se puede construir desde un DTE model o desde XML firmado.
    """
    tipo_dte: int = 0
    folio: int = 0
    fecha_emision: str = ""

    # Emisor
    emisor_rut: str = ""
    emisor_razon: str = ""
    emisor_giro: str = ""
    emisor_dir: str = ""
    emisor_comuna: str = ""
    emisor_ciudad: str = ""
    emisor_acteco: str = ""

    # Receptor
    receptor_rut: str = ""
    receptor_razon: str = ""
    receptor_giro: str = ""
    receptor_dir: str = ""
    receptor_comuna: str = ""
    receptor_ciudad: str = ""

    # Montos
    monto_neto: int | None = None
    monto_exento: int | None = None
    tasa_iva: int | None = None
    iva: int | None = None
    monto_total: int = 0

    # Detalle
    items: list = field(default_factory=list)
    referencias: list = field(default_factory=list)
    descuentos_globales: list = field(default_factory=list)

    # Timbre electrónico (TED XML raw para PDF417)
    ted_xml: str = ""

    # Condiciones de pago
    fma_pago: int | None = None  # 1=Contado, 2=Crédito
    fecha_vencimiento: str | None = None  # YYYY-MM-DD

    # Guía de despacho
    ind_traslado: str | None = None
    tipo_despacho: str | None = None
    ind_servicio: str | None = None

    # Logo personalizado (path)
    logo_path: str = ""

    # Resolución SII
    numero_resolucion: int = 0
    fecha_resolucion: str = ""


# ===================== UTILIDADES =====================

def format_rut(rut: str) -> str:
    """Formatea RUT: 77051056-2 -> 77.051.056-2"""
    parts = rut.split("-")
    if len(parts) != 2:
        return rut
    num, dv = parts
    formatted = ""
    for i, ch in enumerate(reversed(num)):
        if i > 0 and i % 3 == 0:
            formatted = "." + formatted
        formatted = ch + formatted
    return f"{formatted}-{dv}"


def format_number(amount) -> str:
    """Formatea número con separador de miles: 1000000 -> 1.000.000"""
    if amount is None:
        return "0"
    n = int(amount)
    return f"{n:,}".replace(",", ".")


def monto_en_palabras(monto: int) -> str:
    """Convierte monto a palabras: 1234 -> MIL DOSCIENTOS TREINTA Y CUATRO PESOS"""
    try:
        palabras = num2words(monto, lang='es')
        return palabras.upper() + " PESOS"
    except Exception:
        return str(monto)


def generar_imagen_timbre(ted_xml: str, columns: int = 10, scale: int = 3) -> str | None:
    """Genera imagen PNG del timbre PDF417 y retorna path temporal.

    El caller debe eliminar el archivo después de usarlo.

    Args:
        ted_xml: TED XML raw string.
        columns: Columnas del PDF417 (10 para carta, 6-8 para térmico).
        scale: Escala de renderizado.

    Returns:
        Path al PNG temporal, o None si falla.
    """
    if not ted_xml:
        return None
    try:
        ted_clean = ted_xml.strip()
        # CRÍTICO: Compactar el TED eliminando whitespace entre tags.
        # El SII verifica la firma FRMA del CAF contra el DA sin whitespace.
        ted_clean = re.sub(r'>\s+<', '><', ted_clean)
        ted_bytes = ted_clean.encode("ISO-8859-1")
        codes = encode(ted_bytes, columns=columns, security_level=5)
        img = render_image(codes, scale=scale, ratio=3, padding=2)

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        img.save(tmp.name)
        tmp.close()
        return tmp.name
    except Exception:
        return None


def fecha_display(fecha: str) -> str:
    """Convierte YYYY-MM-DD a DD-MM-YYYY para visualización."""
    if "-" in fecha:
        parts = fecha.split("-")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return fecha
