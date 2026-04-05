"""Módulo de impresión de DTEs — CrumbPOS.

Soporta dos formatos:
  - "carta": Tamaño carta (215.9 x 279.4 mm) para impresoras láser/inkjet
  - "termico": Rollo térmico 80mm para impresoras POS

Uso:
    from crumbpos.core.impresion import generar_pdf

    pdf_bytes = generar_pdf(dte_data, formato="carta", cedible=False)
    pdf_bytes = generar_pdf(dte_data, formato="termico")
"""
from .base import DTEPrintData
from .formato_carta import PDFCarta
from .formato_termico import PDFTermico


def generar_pdf(data: DTEPrintData, formato: str = "carta", cedible: bool = False) -> bytes:
    """Genera PDF de un DTE en el formato especificado.

    Args:
        data: Datos del DTE a imprimir.
        formato: "carta" (letter) o "termico" (80mm thermal).
        cedible: Si True, genera copia cedible (solo carta, tipos 33/34/52).

    Returns:
        bytes del PDF generado.
    """
    if formato == "carta":
        pdf = PDFCarta(data, cedible=cedible)
    elif formato == "termico":
        pdf = PDFTermico(data)
    else:
        raise ValueError(f"Formato no soportado: {formato!r}. Use 'carta' o 'termico'.")

    return pdf.generar()
