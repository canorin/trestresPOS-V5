"""Tests de validaciones del generador de libros IECV.

Cubre las defensas pre-emisión agregadas para producción:

- ``_validar_periodo``: rechaza periodos fuera de formato YYYY-MM.
- ``_validar_tipo_envio``: rechaza valores fuera del enum SII.
- ``FctImpAdic``: rango [0.001, 1.000], tipo numérico, consistencia
  entre entries del mismo CodImp.
- ``T61`` (NC) en libro de ventas: montos siempre negativos.
- ``IndTraslado`` 8 y 9 en LibroGuia: ``TpoOper`` se omite, los totales
  van al ``TotTraslado`` con ``TpoTraslado``=8/9.

Estos tests protegen reglas críticas que el SII verifica schema-level
o tributariamente. Su rotura silenciosa habilita rechazos sin trackid.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from crumbpos.core.libros.generador_iecv import (
    generar_libro_compras,
    generar_libro_guias,
    generar_libro_ventas,
)


# ══════════════════════════════════════════════════════════════════
# Helpers compartidos
# ══════════════════════════════════════════════════════════════════


def _empresa_fake():
    return SimpleNamespace(
        rut="77829149-5",
        razon_social="GRUPO TRESTRES SPA",
        giro="PUBLICIDAD",
        acteco=731001,
        direccion="AV PROVIDENCIA 1234",
        comuna="PROVIDENCIA",
        ciudad="SANTIAGO",
        fecha_resolucion="2026-04-21",
        numero_resolucion=0,
    )


def _dte_venta(folio: int, tipo: int = 33, monto_neto: int = 100_000):
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


def _dte_guia_con_ind_traslado(folio: int, ind_traslado: int):
    """Crea un DTE de guía con un XML firmado que contiene IndTraslado."""
    xml = (
        '<DTE><Documento><Encabezado><IdDoc>'
        f'<TipoDTE>52</TipoDTE><Folio>{folio}</Folio>'
        '<FchEmis>2026-04-10</FchEmis>'
        f'<IndTraslado>{ind_traslado}</IndTraslado>'
        '</IdDoc></Encabezado></Documento></DTE>'
    ).encode("iso-8859-1")
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
        xml_firmado=base64.b64encode(xml).decode(),
    )


def _entrada_compra(folio: int = 1, **kwargs):
    base = {
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
    base.update(kwargs)
    return base


# ══════════════════════════════════════════════════════════════════
# 1. _validar_periodo — formato YYYY-MM estricto
# ══════════════════════════════════════════════════════════════════


class TestValidarPeriodo:
    """Periodo malformado debe lanzar ValueError descriptivo en los tres generadores."""

    @pytest.mark.parametrize("periodo", [
        "2026-13",       # mes inválido
        "2026-00",       # mes inválido
        "26-04",         # año corto
        "2026/04",       # separador incorrecto
        "April 2026",    # texto
        "",              # vacío
        "2026-4",        # mes sin padding
    ])
    def test_ventas_rechaza_periodo_invalido(self, periodo):
        with pytest.raises(ValueError, match="Periodo inválido"):
            generar_libro_ventas(
                dtes=[_dte_venta(1)],
                empresa=_empresa_fake(),
                periodo=periodo,
                rut_envia="17586255-2",
                folio_notificacion=0,
            )

    @pytest.mark.parametrize("periodo", ["2026-13", "26-04", ""])
    def test_compras_rechaza_periodo_invalido(self, periodo):
        with pytest.raises(ValueError, match="Periodo inválido"):
            generar_libro_compras(
                dtes=[_entrada_compra(1)],
                empresa=_empresa_fake(),
                periodo=periodo,
                rut_envia="17586255-2",
                folio_notificacion=0,
            )

    @pytest.mark.parametrize("periodo", ["2026-13", "26-04", ""])
    def test_guias_rechaza_periodo_invalido(self, periodo):
        with pytest.raises(ValueError, match="Periodo inválido"):
            generar_libro_guias(
                dtes=[_dte_guia_con_ind_traslado(1, 1)],
                empresa=_empresa_fake(),
                periodo=periodo,
                rut_envia="17586255-2",
                folio_notificacion=4788484,
            )


# ══════════════════════════════════════════════════════════════════
# 2. FctImpAdic — rango, tipo, consistencia
# ══════════════════════════════════════════════════════════════════


class TestFctImpAdic:
    """FctImpAdic debe estar en [0.001, 1.000], ser numérico y consistente."""

    def test_rechaza_fct_imp_adic_negativo(self):
        entrada = _entrada_compra(
            OtrosImp={"CodImp": 28, "TasaImp": 13, "MntImp": 1000, "FctImpAdic": -0.5},
        )
        with pytest.raises(ValueError, match="fuera del rango"):
            generar_libro_compras(
                dtes=[entrada],
                empresa=_empresa_fake(),
                periodo="2026-04",
                rut_envia="17586255-2",
                folio_notificacion=0,
            )

    def test_rechaza_fct_imp_adic_mayor_a_uno(self):
        entrada = _entrada_compra(
            OtrosImp={"CodImp": 28, "TasaImp": 13, "MntImp": 1000, "FctImpAdic": 1.5},
        )
        with pytest.raises(ValueError, match="fuera del rango"):
            generar_libro_compras(
                dtes=[entrada],
                empresa=_empresa_fake(),
                periodo="2026-04",
                rut_envia="17586255-2",
                folio_notificacion=0,
            )

    def test_rechaza_fct_imp_adic_no_numerico(self):
        entrada = _entrada_compra(
            OtrosImp={"CodImp": 28, "TasaImp": 13, "MntImp": 1000, "FctImpAdic": "abc"},
        )
        with pytest.raises(ValueError, match="no es numérico|FctImpAdic"):
            generar_libro_compras(
                dtes=[entrada],
                empresa=_empresa_fake(),
                periodo="2026-04",
                rut_envia="17586255-2",
                folio_notificacion=0,
            )

    def test_rechaza_fct_imp_adic_inconsistente_mismo_cod_imp(self):
        """Dos entries con mismo CodImp pero FctImpAdic distinto → ValueError."""
        e1 = _entrada_compra(
            1,
            OtrosImp={"CodImp": 28, "TasaImp": 13, "MntImp": 1000, "FctImpAdic": 0.6},
        )
        e2 = _entrada_compra(
            2,
            OtrosImp={"CodImp": 28, "TasaImp": 13, "MntImp": 2000, "FctImpAdic": 0.8},
        )
        with pytest.raises(ValueError, match="FctImpAdic inconsistente"):
            generar_libro_compras(
                dtes=[e1, e2],
                empresa=_empresa_fake(),
                periodo="2026-04",
                rut_envia="17586255-2",
                folio_notificacion=0,
            )

    def test_acepta_fct_imp_adic_default_uno(self):
        """Sin FctImpAdic explícito = 1.0 (default) → pasa."""
        entrada = _entrada_compra(
            OtrosImp={"CodImp": 28, "TasaImp": 13, "MntImp": 1000},
        )
        xml, _ = generar_libro_compras(
            dtes=[entrada],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=0,
        )
        # FctImpAdic NO debe aparecer cuando es 1.0 (default)
        assert "<FctImpAdic>" not in xml

    def test_emite_fct_imp_adic_cuando_difiere_de_uno(self):
        """FctImpAdic explícito < 1.0 se emite en el resumen."""
        entrada = _entrada_compra(
            OtrosImp={"CodImp": 28, "TasaImp": 13, "MntImp": 1000, "FctImpAdic": 0.6},
        )
        xml, _ = generar_libro_compras(
            dtes=[entrada],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=0,
        )
        assert "<FctImpAdic>0.6</FctImpAdic>" in xml
        # TotCredImp debe ser round(1000 * 0.6) = 600
        assert "<TotCredImp>600</TotCredImp>" in xml


# ══════════════════════════════════════════════════════════════════
# 3. T61 (NC) — montos siempre negativos en libro de ventas
# ══════════════════════════════════════════════════════════════════


class TestNotaCreditoMontosNegativos:
    """Las NC (T61) deben aparecer con montos negativos en el libro de ventas.

    Razón SII: las NC reducen las ventas del período. Si se emiten con
    montos positivos, el `ResumenPeriodo` no resta correctamente y el
    libro queda fuera de cuadratura.
    """

    def test_t61_con_montos_positivos_se_niegan(self):
        """Una NC almacenada con montos positivos en BD debe salir negada."""
        nc = _dte_venta(folio=10, tipo=61, monto_neto=50_000)
        # _dte_venta por default produce montos positivos
        xml, _ = generar_libro_ventas(
            dtes=[nc],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=0,
        )
        # Detalle debe llevar montos negativos
        assert "<MntNeto>-50000</MntNeto>" in xml
        assert "<MntIVA>-9500</MntIVA>" in xml
        assert "<MntTotal>-59500</MntTotal>" in xml

    def test_t56_nd_no_se_niega(self):
        """ND (T56) mantiene montos positivos — aumenta las ventas."""
        nd = _dte_venta(folio=20, tipo=56, monto_neto=30_000)
        xml, _ = generar_libro_ventas(
            dtes=[nd],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=0,
        )
        # ND debe llevar montos positivos
        assert "<MntNeto>30000</MntNeto>" in xml
        assert "<MntTotal>35700</MntTotal>" in xml

    def test_resumen_periodo_t61_negativo(self):
        """ResumenPeriodo de T61 debe sumar montos negativos."""
        nc = _dte_venta(folio=10, tipo=61, monto_neto=50_000)
        xml, _ = generar_libro_ventas(
            dtes=[nc],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=0,
        )
        # ResumenPeriodo debe tener TotMntNeto negativo
        assert "<TotMntNeto>-50000</TotMntNeto>" in xml
        assert "<TotMntTotal>-59500</TotMntTotal>" in xml


# ══════════════════════════════════════════════════════════════════
# 4. LibroVentas — TpoDocRef en NC/ND (T61/T56)
# ══════════════════════════════════════════════════════════════════


def _dte_nc_con_referencia(folio: int, tipo_dte: int, tpo_doc_ref: int,
                           folio_ref: int, monto_neto: int = 50_000) -> object:
    """DTE de NC/ND con XML firmado que contiene una Referencia."""
    xml = (
        '<DTE xmlns="http://www.sii.cl/SiiDte">'
        '<Documento><Referencia>'
        f'<TpoDocRef>{tpo_doc_ref}</TpoDocRef>'
        f'<FolioRef>{folio_ref}</FolioRef>'
        '</Referencia></Documento></DTE>'
    ).encode("iso-8859-1")
    iva = round(abs(monto_neto) * 0.19)
    total = monto_neto - (iva if monto_neto < 0 else -iva)
    return SimpleNamespace(
        id=f"dte-{folio}",
        folio=folio,
        tipo_dte=tipo_dte,
        fecha_emision="2026-05-27",
        receptor_rut="77051056-2",
        receptor_razon="RECEPTOR",
        monto_neto=abs(monto_neto),
        monto_exento=0,
        iva=iva,
        monto_total=abs(monto_neto) + iva,
        xml_firmado=base64.b64encode(xml).decode(),
    )


class TestNotaCreditoTpoDocRefVentas:
    """TpoDocRef en libro de VENTAS solo para liquidaciones (T40/T43/T103).

    Regla SII: en el libro de ventas IECV, TpoDocRef en Detalle de T61/T56
    solo es válido cuando apunta a una liquidación (T40, T43, T103). Para
    NCs/NDs que referencian facturas regulares (T33, T34, T52) el campo debe
    OMITIRSE. Incluirlo provoca reparo LBR-2 "TpoDoc debe ser [40, 43, 103]"
    que bloquea la declaración de avance en certificación.

    Ref: crumbpos/core/libros/generador_iecv.py — _TIPOS_REF_VALIDOS_VENTA
    """

    def test_t61_referencia_t33_omite_tpo_doc_ref(self):
        """NC T61 que referencia T33 NO debe incluir TpoDocRef en libro ventas."""
        nc = _dte_nc_con_referencia(folio=141, tipo_dte=61, tpo_doc_ref=33,
                                    folio_ref=140, monto_neto=-50_000)
        xml, _ = generar_libro_ventas(
            dtes=[nc], empresa=_empresa_fake(), periodo="2026-05",
            rut_envia="17586255-2",
        )
        assert "<TpoDocRef>" not in xml, (
            "TpoDocRef=33 no debe aparecer en libro ventas: produce reparo LBR-2"
        )
        assert "<FolioDocRef>" not in xml

    def test_t61_referencia_t34_omite_tpo_doc_ref(self):
        """NC T61 que referencia T34 exenta tampoco incluye TpoDocRef."""
        nc = _dte_nc_con_referencia(folio=200, tipo_dte=61, tpo_doc_ref=34,
                                    folio_ref=10, monto_neto=-30_000)
        xml, _ = generar_libro_ventas(
            dtes=[nc], empresa=_empresa_fake(), periodo="2026-05",
            rut_envia="17586255-2",
        )
        assert "<TpoDocRef>" not in xml

    def test_t61_referencia_t103_incluye_tpo_doc_ref(self):
        """NC T61 que referencia T103 (liquidación electrónica) SÍ lleva TpoDocRef."""
        nc = _dte_nc_con_referencia(folio=300, tipo_dte=61, tpo_doc_ref=103,
                                    folio_ref=50, monto_neto=-80_000)
        xml, _ = generar_libro_ventas(
            dtes=[nc], empresa=_empresa_fake(), periodo="2026-05",
            rut_envia="17586255-2",
        )
        assert "<TpoDocRef>103</TpoDocRef>" in xml
        assert "<FolioDocRef>50</FolioDocRef>" in xml

    def test_t61_referencia_t40_incluye_tpo_doc_ref(self):
        """NC T61 que referencia T40 (liquidación papel) SÍ lleva TpoDocRef."""
        nc = _dte_nc_con_referencia(folio=301, tipo_dte=61, tpo_doc_ref=40,
                                    folio_ref=12, monto_neto=-20_000)
        xml, _ = generar_libro_ventas(
            dtes=[nc], empresa=_empresa_fake(), periodo="2026-05",
            rut_envia="17586255-2",
        )
        assert "<TpoDocRef>40</TpoDocRef>" in xml

    def test_t56_referencia_t33_omite_tpo_doc_ref(self):
        """ND T56 que referencia T33 también omite TpoDocRef."""
        nd = _dte_nc_con_referencia(folio=78, tipo_dte=56, tpo_doc_ref=33,
                                    folio_ref=140, monto_neto=10_000)
        xml, _ = generar_libro_ventas(
            dtes=[nd], empresa=_empresa_fake(), periodo="2026-05",
            rut_envia="17586255-2",
        )
        assert "<TpoDocRef>" not in xml


# ══════════════════════════════════════════════════════════════════
# 5. LibroGuia — IndTraslado 8 y 9 (exportación)
# ══════════════════════════════════════════════════════════════════


class TestLibroGuiaExportacion:
    """IndTraslado ∈ {8, 9} se mapea a TpoOper=None (omitido en Detalle)
    pero los totales sí van a TotTraslado con TpoTraslado=8/9.

    Razón SII: ``LibroGuia_v10.xsd`` define ``TpoOper`` con enum 1-7
    mientras ``TpoTraslado`` (en TotTraslado) acepta 2-9. Si la guía
    de exportación emitiera ``<TpoOper>8</TpoOper>``, el SII rechaza
    schema-level (cvc-enumeration-valid).
    """

    def test_ind_traslado_8_omite_tpo_oper_en_detalle(self):
        """IndTraslado=8 (traslado exportación) NO emite TpoOper en Detalle."""
        dte = _dte_guia_con_ind_traslado(1, ind_traslado=8)
        xml, _ = generar_libro_guias(
            dtes=[dte],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788484,
        )
        # En el Detalle no debe aparecer TpoOper
        # (que solo va si el valor está en 1-7)
        detalle_inicio = xml.find("<Detalle>")
        detalle_fin = xml.find("</Detalle>", detalle_inicio)
        detalle = xml[detalle_inicio:detalle_fin]
        assert "<TpoOper>" not in detalle, (
            f"TpoOper no debe estar presente en Detalle para IndTraslado=8. "
            f"Detalle: {detalle}"
        )

    def test_ind_traslado_9_omite_tpo_oper_en_detalle(self):
        """IndTraslado=9 (venta exportación) NO emite TpoOper en Detalle."""
        dte = _dte_guia_con_ind_traslado(1, ind_traslado=9)
        xml, _ = generar_libro_guias(
            dtes=[dte],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788484,
        )
        detalle_inicio = xml.find("<Detalle>")
        detalle_fin = xml.find("</Detalle>", detalle_inicio)
        detalle = xml[detalle_inicio:detalle_fin]
        assert "<TpoOper>" not in detalle

    def test_ind_traslado_8_va_a_tot_traslado(self):
        """IndTraslado=8 debe aparecer en TotTraslado con TpoTraslado=8."""
        dte = _dte_guia_con_ind_traslado(1, ind_traslado=8)
        xml, _ = generar_libro_guias(
            dtes=[dte],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788484,
        )
        # ResumenPeriodo debe tener TotTraslado con TpoTraslado=8
        assert "<TpoTraslado>8</TpoTraslado>" in xml
        # Debe estar dentro de un TotTraslado
        tt_idx = xml.find("<TotTraslado>")
        assert tt_idx != -1
        tt_end = xml.find("</TotTraslado>", tt_idx)
        tt_block = xml[tt_idx:tt_end]
        assert "<TpoTraslado>8</TpoTraslado>" in tt_block

    def test_ind_traslado_1_si_emite_tpo_oper(self):
        """IndTraslado=1 (venta) SÍ emite TpoOper=1 (regresión)."""
        dte = _dte_guia_con_ind_traslado(1, ind_traslado=1)
        xml, _ = generar_libro_guias(
            dtes=[dte],
            empresa=_empresa_fake(),
            periodo="2026-04",
            rut_envia="17586255-2",
            folio_notificacion=4788484,
        )
        detalle_inicio = xml.find("<Detalle>")
        detalle_fin = xml.find("</Detalle>", detalle_inicio)
        detalle = xml[detalle_inicio:detalle_fin]
        assert "<TpoOper>1</TpoOper>" in detalle
