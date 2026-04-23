"""Tests de orden estricto de elementos en la carátula (contra XSD oficial).

Estos tests protegen la secuencia exacta que exige cada XSD del SII:

- ``LibroCV_v10.xsd`` (ventas/compras) define en la ``<Caratula>``:
  RutEmisorLibro → RutEnvia → PeriodoTributario → FchResol → NroResol
  → **TipoOperacion** → TipoLibro → TipoEnvio (opcional) → FolioNotificacion
  (+ otros campos mensuales opcionales).

- ``LibroGuia_v10.xsd`` define en la ``<Caratula>``:
  RutEmisorLibro → RutEnvia → PeriodoTributario → FchResol → NroResol
  → TipoLibro → TipoEnvio (opcional) → FolioNotificacion.
  **NO incluye TipoOperacion** — no está en el XSD del libro de guías.

Historia del bug (cert 77829149-5, 2026-04-23):
    Un fix previo agregó ``<TipoOperacion>GUIA</TipoOperacion>`` a la
    carátula del LibroGuia asumiendo que era un invariante común a los
    tres libros. El SII rechazó schema-level con::

        cvc-complex-type.2.4.a: Invalid content was found starting with
        element 'TipoOperacion'. One of '{...:TipoLibro}' is expected.

    Los tests existentes validaban *presencia* del elemento, pero no el
    *orden* ni la *correspondencia con el XSD real*, así que el fix
    pasaba GREEN mientras el SII rechazaba.

Estos tests son la red de protección para que no vuelva a pasar:
  1. Orden por índice de cada elemento de la carátula.
  2. Inclusión/exclusión explícita de ``TipoOperacion`` por tipo de libro.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from crumbpos.core.libros.generador_iecv import (
    generar_libro_compras,
    generar_libro_guias,
    generar_libro_ventas,
)


# ══════════════════════════════════════════════════════════════════
# Secuencias oficiales (copiadas directamente del XSD)
# ══════════════════════════════════════════════════════════════════
#
# Estas listas son el contrato con el SII. Si alguien reordena la
# carátula en el generador, el test falla antes de llegar a enviar.

ORDEN_CARATULA_LIBRO_CV = [
    "RutEmisorLibro",
    "RutEnvia",
    "PeriodoTributario",
    "FchResol",
    "NroResol",
    "TipoOperacion",    # sí va en LibroCV (valores: COMPRA | VENTA)
    "TipoLibro",
    "TipoEnvio",
    "FolioNotificacion",
]

ORDEN_CARATULA_LIBRO_GUIA = [
    "RutEmisorLibro",
    "RutEnvia",
    "PeriodoTributario",
    "FchResol",
    "NroResol",
    # NO va TipoOperacion en LibroGuia: el XSD no lo define
    "TipoLibro",
    "TipoEnvio",
    "FolioNotificacion",
]


def _empresa_fake():
    return SimpleNamespace(
        rut="77829149-5",
        razon_social="GRUPO TRESTRES SPA",
        giro="PUBLICIDAD",
        acteco=731001,
        direccion="LOS MILITARES 5620",
        comuna="LAS CONDES",
        ciudad="SANTIAGO",
        fecha_resolucion="2026-04-21",
        numero_resolucion=0,
    )


def _dte_venta(folio: int = 1):
    return SimpleNamespace(
        id=f"dte-{folio}", folio=folio, tipo_dte=33,
        fecha_emision="2026-04-10",
        receptor_rut="11111111-1", receptor_razon="CLIENTE",
        monto_neto=100_000, monto_exento=0,
        iva=19_000, monto_total=119_000, xml_firmado=None,
    )


def _dte_guia(folio: int = 1):
    return SimpleNamespace(
        id=f"dte-guia-{folio}", folio=folio, tipo_dte=52,
        fecha_emision="2026-04-10",
        receptor_rut="11111111-1", receptor_razon="CLIENTE",
        monto_neto=100_000, monto_exento=0,
        iva=19_000, monto_total=119_000, xml_firmado=None,
    )


def _entrada_compra(folio: int = 1):
    return {
        "TpoDoc": 33, "NroDoc": folio, "TpoImp": 1, "TasaImp": 19,
        "FchDoc": "2026-04-10", "RUTDoc": "11111111-1",
        "RznSoc": "PROVEEDOR", "MntNeto": 100_000, "MntIVA": 19_000,
        "MntTotal": 119_000,
    }


def _extraer_nombres_elementos_caratula(xml: str) -> list[str]:
    """Extrae los nombres de los elementos directos dentro de <Caratula>,
    en orden de aparición. Ignora namespaces.

    Por ejemplo: ``['RutEmisorLibro', 'RutEnvia', ..., 'FolioNotificacion']``
    """
    m = re.search(r"<Caratula>(.*?)</Caratula>", xml, re.DOTALL)
    assert m, f"No hay <Caratula> en el XML: {xml[:200]}"
    interior = m.group(1)
    # <Nombre>...</Nombre> — matchea solo tags de apertura de primer nivel
    return re.findall(r"<(\w+)>", interior)


# ══════════════════════════════════════════════════════════════════
# Orden de elementos — LibroGuia (sin TipoOperacion)
# ══════════════════════════════════════════════════════════════════


class TestOrdenCaratulaLibroGuia:
    """La carátula del libro de guías debe seguir EXACTAMENTE la sequence
    del XSD ``LibroGuia_v10.xsd``. Violarlo produce rechazo schema-level
    del SII (``cvc-complex-type.2.4.a``) sin devolver trackid.
    """

    def test_orden_exacto_con_folio_notificacion(self):
        xml, _ = generar_libro_guias(
            dtes=[_dte_guia(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4791437,
        )
        nombres = _extraer_nombres_elementos_caratula(xml)
        assert nombres == ORDEN_CARATULA_LIBRO_GUIA, (
            f"Orden de carátula LibroGuia no corresponde al XSD.\n"
            f"Esperado: {ORDEN_CARATULA_LIBRO_GUIA}\n"
            f"Actual:   {nombres}"
        )

    def test_orden_exacto_sin_folio_notificacion(self):
        """Sin FolioNotificacion (MENSUAL), los 7 elementos previos deben
        seguir apareciendo en el mismo orden."""
        xml, _ = generar_libro_guias(
            dtes=[_dte_guia(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=0,
        )
        nombres = _extraer_nombres_elementos_caratula(xml)
        # Sin FolioNotificacion al final
        esperado = [
            n for n in ORDEN_CARATULA_LIBRO_GUIA if n != "FolioNotificacion"
        ]
        assert nombres == esperado, (
            f"Orden de carátula LibroGuia MENSUAL incorrecto.\n"
            f"Esperado: {esperado}\nActual: {nombres}"
        )

    def test_no_incluye_tipo_operacion(self):
        """El XSD del LibroGuia NO define ``TipoOperacion`` en la carátula.
        Incluirlo causa ``cvc-complex-type.2.4.a`` del SII al validar
        esquema, sin devolver trackid."""
        xml, _ = generar_libro_guias(
            dtes=[_dte_guia(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4791437,
        )
        nombres = _extraer_nombres_elementos_caratula(xml)
        assert "TipoOperacion" not in nombres, (
            "La carátula del LibroGuia NO debe incluir <TipoOperacion>. "
            "El XSD oficial (LibroGuia_v10.xsd) no define ese elemento; "
            "emitirlo produce rechazo schema-level del SII."
        )


# ══════════════════════════════════════════════════════════════════
# Orden de elementos — LibroCV (CON TipoOperacion entre NroResol y TipoLibro)
# ══════════════════════════════════════════════════════════════════


class TestOrdenCaratulaLibroVentas:
    """La carátula del libro de ventas sigue el orden del XSD
    ``LibroCV_v10.xsd`` — ``TipoOperacion=VENTA`` va entre ``NroResol`` y
    ``TipoLibro``.
    """

    def test_orden_exacto_con_folio_notificacion(self):
        xml, _ = generar_libro_ventas(
            dtes=[_dte_venta(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788484,
        )
        nombres = _extraer_nombres_elementos_caratula(xml)
        assert nombres == ORDEN_CARATULA_LIBRO_CV, (
            f"Orden de carátula Libro Ventas no corresponde al XSD.\n"
            f"Esperado: {ORDEN_CARATULA_LIBRO_CV}\n"
            f"Actual:   {nombres}"
        )

    def test_incluye_tipo_operacion_venta(self):
        xml, _ = generar_libro_ventas(
            dtes=[_dte_venta(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788484,
        )
        assert "<TipoOperacion>VENTA</TipoOperacion>" in xml


class TestOrdenCaratulaLibroCompras:
    """La carátula del libro de compras — mismo XSD que ventas, el único
    campo que cambia es ``TipoOperacion=COMPRA``.
    """

    def test_orden_exacto_con_folio_notificacion(self):
        xml, _ = generar_libro_compras(
            dtes=[_entrada_compra(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788485,
        )
        nombres = _extraer_nombres_elementos_caratula(xml)
        assert nombres == ORDEN_CARATULA_LIBRO_CV, (
            f"Orden de carátula Libro Compras no corresponde al XSD.\n"
            f"Esperado: {ORDEN_CARATULA_LIBRO_CV}\n"
            f"Actual:   {nombres}"
        )

    def test_incluye_tipo_operacion_compra(self):
        xml, _ = generar_libro_compras(
            dtes=[_entrada_compra(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788485,
        )
        assert "<TipoOperacion>COMPRA</TipoOperacion>" in xml
