"""Intercambio de Información SII (Ley 19.983).

El SII envía a los contribuyentes en certificación un `ENVIO_DTE.xml`
simulando un proveedor. El contribuyente debe responder con 3 archivos:

1. `1_RecepcionDTE.xml` — acuse de formato del sobre.
2. `2_EnvioRecibos.xml`  — acuse comercial Ley 19.983.
3. `3_ResultadoDTE.xml`  — resultado comercial (aceptado/rechazado).

Este módulo:
- `parser.py`    → lee el XML del SII y devuelve un `SobreIntercambio`.
- `generador.py` → arma y firma los 3 XMLs de respuesta.

Referencia: etapa OPCIONAL en la certificación. El SII no la pide
en todos los casos (ej: TRESTRES PUBLICIDAD no la tuvo).
"""
