"""Módulo del Set de Simulación SII (paso 2 de 6 de la certificación).

A diferencia del Set de Pruebas (paso 1), que lee los casos de un .txt que
envía el SII, el Set de Simulación lo genera el contribuyente: 10 a 100
DTEs representativos de su operación real, firmados y enviados en un solo
EnvioDTE al SII.

Este módulo contiene:

- ``validador``: valida la configuración del set (tipos, cantidades, pool
  de items, receptor) y enforce las reglas de dependencia T33 → T61 → T56
  y la obligatoriedad de notas cuando se emiten facturas.
- ``generador`` (Stage 2): a partir de una config válida, crea los
  ``CertificacionCaso`` con ``set_nombre="SIMULACION"`` y arma la cadena
  de referencias automáticamente.

Las reglas y la cadena de dependencias son las mismas que la operación
real en Chile:

  Factura (T33/T34) → se corrige con → Nota de Crédito (T61)
                                       Nota de Crédito → se corrige con →
                                       Nota de Débito (T56)
"""
from __future__ import annotations

SET_SIMULACION = "SIMULACION"

__all__ = ["SET_SIMULACION"]
