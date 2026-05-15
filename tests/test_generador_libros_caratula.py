"""Tests de la Caratula de los libros tributarios.

Verifica que cada generador emita los campos obligatorios de la carátula
según los XSDs oficiales del SII (``LibroCV_v10.xsd`` y
``LibroGuia_v10.xsd``):

- ``RutEmisorLibro``, ``RutEnvia``, ``PeriodoTributario``, ``FchResol``,
  ``NroResol`` → siempre, en los tres libros.
- ``TipoOperacion`` → ``VENTA`` / ``COMPRA`` **solo en LibroCV**. El XSD
  del LibroGuia NO define este elemento; incluirlo produce rechazo
  schema-level del SII (``cvc-complex-type.2.4.a``).
- ``TipoLibro`` → ``ESPECIAL`` si ``folio_notificacion > 0`` (certificación),
  ``MENSUAL`` si no.
- ``TipoEnvio`` → ``TOTAL`` en envíos originales, ``AJUSTE`` en re-envíos
  correctivos (libro ya recibido por el SII → evita error LNC).
- ``FolioNotificacion`` → solo presente cuando ``TipoLibro == ESPECIAL``.

Historia del bug (cert 77829149-5, 2026-04-23):
    Un fix previo (2026-04-22) agregó ``<TipoOperacion>GUIA</TipoOperacion>``
    a la carátula del LibroGuia asumiendo que era un invariante común a
    los tres libros. Los tests aquí validaban *presencia* del elemento
    pero no comparaban contra el XSD real del SII, así que pasaban GREEN
    mientras el SII rechazaba schema-level sin devolver trackid. El test
    de orden estricto vive ahora en ``tests/test_caratula_orden_xsd.py``.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from crumbpos.core.libros.generador_iecv import (
    generar_libro_compras,
    generar_libro_guias,
    generar_libro_ventas,
)


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════


def _empresa_fake():
    return SimpleNamespace(
        rut="77829149-5",
        razon_social="GRUPO TRESTRES SPA",
        giro="PUBLICIDAD",
        acteco=731001,
        direccion="LOS MILITARES 5620 OF 905",
        comuna="LAS CONDES",
        ciudad="SANTIAGO",
        fecha_resolucion="2026-04-21",
        numero_resolucion=0,
    )


def _dte_venta(folio: int, tipo: int = 33, monto_neto: int = 100_000):
    """DteEmitido mínimo para pasar por generar_libro_ventas.

    Usa los nombres reales del modelo ``DteEmitido``: ``iva`` (no
    ``monto_iva``), ``receptor_rut``/``receptor_razon``, ``xml_firmado``.
    """
    iva = round(monto_neto * 0.19)
    return SimpleNamespace(
        id=f"dte-{folio}",
        folio=folio,
        tipo_dte=tipo,
        fecha_emision="2026-04-10",
        receptor_rut="11111111-1",
        receptor_razon="CLIENTE",
        monto_neto=monto_neto,
        monto_exento=0,
        iva=iva,
        monto_total=monto_neto + iva,
        xml_firmado=None,
    )


def _dte_guia(folio: int):
    return SimpleNamespace(
        id=f"dte-guia-{folio}",
        folio=folio,
        tipo_dte=52,
        fecha_emision="2026-04-10",
        receptor_rut="11111111-1",
        receptor_razon="CLIENTE",
        monto_neto=100_000,
        monto_exento=0,
        iva=19_000,
        monto_total=119_000,
        xml_firmado=None,
    )


def _entrada_compra(folio: int = 1):
    return {
        "TpoDoc": 33,
        "NroDoc": folio,
        "TpoImp": 1,
        "TasaImp": 19,
        "FchDoc": "2026-04-10",
        "RUTDoc": "11111111-1",
        "RznSoc": "PROVEEDOR",
        "MntNeto": 100_000,
        "MntIVA": 19_000,
        "MntTotal": 119_000,
    }


def _extraer_caratula(xml: str) -> str:
    m = re.search(r"<Caratula.*?</Caratula>", xml, re.DOTALL)
    assert m, f"No se encontró <Caratula> en el XML generado: {xml[:300]}"
    return m.group(0)


# ══════════════════════════════════════════════════════════════════
# TipoOperacion (obligatorio en los tres libros)
# ══════════════════════════════════════════════════════════════════


class TestTipoOperacionEnCaratula:
    """``TipoOperacion`` es obligatorio en la carátula del LibroCV
    (ventas=``VENTA``, compras=``COMPRA``) según ``LibroCV_v10.xsd``.

    El XSD del LibroGuia (``LibroGuia_v10.xsd``) **NO define** este
    elemento, así que el generador de guías no debe emitirlo: incluirlo
    causa ``cvc-complex-type.2.4.a`` del SII sin devolver trackid.
    """

    def test_libro_ventas_emite_tipo_operacion_venta(self):
        xml, _ = generar_libro_ventas(
            dtes=[_dte_venta(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788484,
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoOperacion>VENTA</TipoOperacion>" in caratula

    def test_libro_compras_emite_tipo_operacion_compra(self):
        xml, _ = generar_libro_compras(
            dtes=[_entrada_compra(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788485,
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoOperacion>COMPRA</TipoOperacion>" in caratula

    def test_libro_guias_no_emite_tipo_operacion(self):
        """El XSD del LibroGuia no define TipoOperacion — emitirlo
        produce rechazo schema-level del SII sin devolver trackid.

        Historia: fix 2026-04-22 agregó ``<TipoOperacion>GUIA</TipoOperacion>``
        asumiendo que era un invariante común a los tres libros. El SII
        rechazó el envío con ``cvc-complex-type.2.4.a: Invalid content
        was found starting with element 'TipoOperacion'``.
        """
        xml, _ = generar_libro_guias(
            dtes=[_dte_guia(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788487,
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoOperacion>" not in caratula, (
            "La carátula del LibroGuia NO debe incluir <TipoOperacion>. "
            "El XSD oficial LibroGuia_v10.xsd no lo define; emitirlo "
            "causa rechazo schema-level del SII (cvc-complex-type.2.4.a)."
        )


# ══════════════════════════════════════════════════════════════════
# Invariantes compartidos de la carátula
# ══════════════════════════════════════════════════════════════════


class TestCaratulaCamposObligatoriosGuias:
    """Los demás campos de la carátula del libro de guías no deben
    perderse al agregar TipoOperacion."""

    @pytest.fixture
    def caratula(self):
        xml, _ = generar_libro_guias(
            dtes=[_dte_guia(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788487,
        )
        return _extraer_caratula(xml)

    def test_rut_emisor_libro(self, caratula):
        assert "<RutEmisorLibro>77829149-5</RutEmisorLibro>" in caratula

    def test_rut_envia(self, caratula):
        assert "<RutEnvia>17586255-2</RutEnvia>" in caratula

    def test_periodo_tributario(self, caratula):
        assert "<PeriodoTributario>2026-04</PeriodoTributario>" in caratula

    def test_fch_resol(self, caratula):
        assert "<FchResol>2026-04-21</FchResol>" in caratula

    def test_nro_resol(self, caratula):
        assert "<NroResol>0</NroResol>" in caratula

    def test_tipo_libro_especial_con_folio_notificacion(self, caratula):
        assert "<TipoLibro>ESPECIAL</TipoLibro>" in caratula

    def test_tipo_envio_total(self, caratula):
        assert "<TipoEnvio>TOTAL</TipoEnvio>" in caratula

    def test_folio_notificacion_presente_en_especial(self, caratula):
        assert "<FolioNotificacion>4788487</FolioNotificacion>" in caratula


class TestCaratulaModoMensualGuias:
    """LibroGuia solo acepta TipoLibro='ESPECIAL' — modo MENSUAL no existe."""

    def test_folio_notificacion_cero_lanza_value_error(self):
        """folio_notificacion=0 debe lanzar ValueError: LibroGuia_v10.xsd
        solo define TipoLibro='ESPECIAL' y FolioNotificacion es obligatorio.

        El modo MENSUAL (sin FolioNotificacion) es exclusivo de LibroCV
        (ventas/compras). LibroGuia siempre requiere número de atención del SII.
        """
        with pytest.raises(ValueError, match="folio_notificacion debe ser un entero > 0"):
            generar_libro_guias(
                dtes=[_dte_guia(1)],
                empresa=_empresa_fake(),
                periodo="2026-04",
                rut_envia="17586255-2",
                folio_notificacion=0,
            )


class TestCaratulaModoMensualVentas:
    """LibroCV ventas: folio_notificacion=0 → TipoLibro=MENSUAL, sin FolioNotificacion."""

    def test_tipo_libro_mensual_cuando_no_hay_folio_notificacion(self):
        """En LibroCV (ventas), folio_notificacion=0 debe producir TipoLibro=MENSUAL
        y omitir FolioNotificacion. Este es el modo normal de producción mensual."""
        xml, _ = generar_libro_ventas(
            dtes=[_dte_venta(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=0,
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoLibro>MENSUAL</TipoLibro>" in caratula
        assert "<FolioNotificacion>" not in caratula
        # TipoOperacion=VENTA debe estar presente
        assert "<TipoOperacion>VENTA</TipoOperacion>" in caratula


class TestCaratulaModoMensualCompras:
    """LibroCV compras: folio_notificacion=0 → TipoLibro=MENSUAL, sin FolioNotificacion."""

    def test_tipo_libro_mensual_cuando_no_hay_folio_notificacion(self):
        """En LibroCV (compras), folio_notificacion=0 debe producir TipoLibro=MENSUAL
        y omitir FolioNotificacion. Este es el modo normal de producción mensual."""
        xml, _ = generar_libro_compras(
            dtes=[_entrada_compra(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=0,
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoLibro>MENSUAL</TipoLibro>" in caratula
        assert "<FolioNotificacion>" not in caratula
        # TipoOperacion=COMPRA debe estar presente
        assert "<TipoOperacion>COMPRA</TipoOperacion>" in caratula


# ══════════════════════════════════════════════════════════════════
# TipoEnvio — TOTAL (default) vs AJUSTE (re-envío correctivo)
# ══════════════════════════════════════════════════════════════════
#
# Contexto SII (LibroCV_v10.xsd / LibroGuia_v10.xsd):
#   <TipoEnvio> permite TOTAL, PARCIAL, FINAL, AJUSTE.
#   - TOTAL  → "Único envío que compone el libro"
#   - AJUSTE → "Envío con información para corregir o complementar
#              un libro previamente enviado"
#
# Cuando el SII ya recibió un libro TOTAL para un combo
# ``N°Atención + Período + TipoLibro=ESPECIAL`` y se le envía otra
# vez TOTAL, responde con el error **LNC** ("Tipo de Envío de Libro
# No Corresponde"). El re-envío correctivo debe ir como AJUSTE.
#
# Historia real: certificación 77829149-5 (2026-04-23) mandó los 3
# libros con bugs → SII los recibió (LTC). Al regenerar con los fixes
# y re-enviar como TOTAL → SII devolvió LNC en los 3. Solución correcta:
# el core acepta ``tipo_envio`` y el servicio de envío lo fuerza a
# ``AJUSTE`` cuando detecta un trackid previo.


class TestTipoEnvioDefaultTotal:
    """Sin especificar tipo_envio, el default se mantiene en TOTAL
    (regresión: no debe romper primeras emisiones existentes)."""

    def test_libro_ventas_default_total(self):
        xml, _ = generar_libro_ventas(
            dtes=[_dte_venta(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788484,
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoEnvio>TOTAL</TipoEnvio>" in caratula

    def test_libro_compras_default_total(self):
        xml, _ = generar_libro_compras(
            dtes=[_entrada_compra(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788485,
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoEnvio>TOTAL</TipoEnvio>" in caratula

    def test_libro_guias_default_total(self):
        xml, _ = generar_libro_guias(
            dtes=[_dte_guia(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788487,
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoEnvio>TOTAL</TipoEnvio>" in caratula


class TestTipoEnvioAjuste:
    """Con tipo_envio='AJUSTE', la carátula emite AJUSTE en lugar de TOTAL.

    Este es el valor correcto para re-enviar un libro ya recibido por
    el SII (según LibroCV_v10.xsd: 'corregir o complementar un libro
    previamente enviado').
    """

    def test_libro_ventas_ajuste(self):
        xml, _ = generar_libro_ventas(
            dtes=[_dte_venta(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788484,
            tipo_envio="AJUSTE",
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoEnvio>AJUSTE</TipoEnvio>" in caratula
        assert "<TipoEnvio>TOTAL</TipoEnvio>" not in caratula

    def test_libro_compras_ajuste(self):
        xml, _ = generar_libro_compras(
            dtes=[_entrada_compra(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788485,
            tipo_envio="AJUSTE",
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoEnvio>AJUSTE</TipoEnvio>" in caratula
        assert "<TipoEnvio>TOTAL</TipoEnvio>" not in caratula

    def test_libro_guias_ajuste(self):
        xml, _ = generar_libro_guias(
            dtes=[_dte_guia(1)],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788487,
            tipo_envio="AJUSTE",
        )
        caratula = _extraer_caratula(xml)
        assert "<TipoEnvio>AJUSTE</TipoEnvio>" in caratula
        assert "<TipoEnvio>TOTAL</TipoEnvio>" not in caratula


class TestTipoEnvioValidacion:
    """Valores no permitidos por el XSD deben fallar antes de firmar.

    El XSD oficial del SII restringe TipoEnvio a TOTAL/PARCIAL/FINAL/AJUSTE.
    Cualquier otro string sería rechazado al validar XSD o por el SII,
    pero además queremos capturarlo temprano (fail fast).
    """

    @pytest.mark.parametrize("valor_invalido", ["RECTIFICA", "total", "", "FOO", "X"])
    def test_libro_ventas_rechaza_valor_invalido(self, valor_invalido):
        with pytest.raises(ValueError, match="TipoEnvio"):
            generar_libro_ventas(
                dtes=[_dte_venta(1)],
                empresa=_empresa_fake(),
                periodo="2026-04",
                rut_envia="17586255-2",
                folio_notificacion=4788484,
                tipo_envio=valor_invalido,
            )

    def test_libro_compras_rechaza_valor_invalido(self):
        with pytest.raises(ValueError, match="TipoEnvio"):
            generar_libro_compras(
                dtes=[_entrada_compra(1)],
                empresa=_empresa_fake(),
                periodo="2026-04",
                rut_envia="17586255-2",
                folio_notificacion=4788485,
                tipo_envio="RECTIFICA",
            )

    def test_libro_guias_rechaza_valor_invalido(self):
        with pytest.raises(ValueError, match="TipoEnvio"):
            generar_libro_guias(
                dtes=[_dte_guia(1)],
                empresa=_empresa_fake(),
                periodo="2026-04",
                rut_envia="17586255-2",
                folio_notificacion=4788487,
                tipo_envio="RECTIFICA",
            )
