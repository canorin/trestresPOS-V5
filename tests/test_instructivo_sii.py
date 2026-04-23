"""Tests de contrato del módulo ``crumbpos.core.libros.instructivo_sii``.

Este módulo es la fuente de verdad de las reglas que el SII impone sobre
los libros electrónicos. Los tests se agrupan en tres bloques:

1. **Snapshot** — las citas literales (constantes ``INSTRUCCION_*_SII``)
   no pueden ser alteradas sin dejar un RED explícito. Cualquier cambio
   debe venir acompañado de la actualización del snapshot en este archivo
   y de una justificación (cambio del SII).

2. **Unit** — las funciones canónicas (``elegir_set_libro_ventas``,
   ``filtrar_dte_ids_para_libro_ventas``, ``parsear_casos_guias_*``,
   ``detectar_observaciones_compra``) producen el resultado esperado
   sobre inputs controlados.

3. **Contrato consumidor** — el código que consume libros importa las
   reglas desde ``instructivo_sii`` y no reimplementa la lógica
   inline. Esto evita que un fix local silencioso vuelva a ignorar el
   instructivo (ya pasó en cert 77829149-5).

Si uno de estos tests falla, detente antes de "arreglarlo": o el SII
cambió de verdad (entonces actualiza la cita literal y la implementación
de la función) o el consumidor regresó a la lógica ad-hoc (entonces
revierte al import canónico).
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass

import pytest

from crumbpos.core.libros import instructivo_sii
from crumbpos.core.libros.instructivo_sii import (
    INSTRUCCION_LIBRO_GUIAS_SII_EJEMPLO,
    INSTRUCCION_LIBRO_VENTAS_SII,
    OBS_ENTREGA_GRATUITA,
    OBS_FACTOR_PROPORCIONALIDAD,
    OBS_IVA_RETENIDO_TOTAL,
    OBS_RETENCION_TOTAL,
    OBS_USO_COMUN,
    PRIORIDAD_SETS_LIBRO_VENTAS,
    RE_GUIA_ANULADA_INSTRUCCION,
    RE_GUIA_FACTURADA_INSTRUCCION,
    detectar_observaciones_compra,
    elegir_set_libro_ventas,
    filtrar_dte_ids_para_libro_guias,
    filtrar_dte_ids_para_libro_ventas,
    parsear_casos_guias_anuladas,
    parsear_casos_guias_facturadas,
)


# ══════════════════════════════════════════════════════════════════
# Bloque 1 — Snapshots de citas literales
# ══════════════════════════════════════════════════════════════════


class TestSnapshotInstruccionesLiterales:
    """Las citas literales del instructivo SII son inmutables.

    Si uno de estos tests falla, es porque alguien cambió el texto que
    el SII publica oficialmente. Requiere justificación expresa.
    """

    def test_instruccion_libro_ventas_es_exactamente_la_del_set(self):
        esperado = (
            "CONSTRUYA EL LIBRO DE VENTAS CON LOS DOCUMENTOS CON QUE GENERO\n"
            "EL SET BASICO O EL SET DE FACTURA EXENTA, SEGUN CORRESPONDA.\n"
            "SI OBTUVO AMBOS SET, UTILICE LOS DOCUMENTOS DEL SET BASICO PARA\n"
            "CONSTRUIR EL LIBRO DE VENTAS."
        )
        assert INSTRUCCION_LIBRO_VENTAS_SII == esperado

    def test_instruccion_libro_guias_es_exactamente_la_del_set(self):
        esperado = (
            "CONSTRUYA EL LIBRO CON LAS GUIAS CON QUE GENERO EL SET GUIA DE DESPACHO,\n"
            "TENIENDO EN CUENTA LAS SIGUIENTES CONSIDERACIONES\n"
            "\n"
            "- EL CASO N CORRESPONDE A UNA GUIA QUE SE FACTURO EN EL PERIODO\n"
            "- EL CASO N CORRESPONDE A UNA GUIA ANULADA"
        )
        assert INSTRUCCION_LIBRO_GUIAS_SII_EJEMPLO == esperado

    def test_prioridad_sets_libro_ventas_basico_antes_que_exenta(self):
        # El instructivo manda priorizar BASICO. No invertir el orden.
        assert PRIORIDAD_SETS_LIBRO_VENTAS == ("BASICO", "EXENTA")

    def test_observaciones_compra_literales(self):
        # Los textos que el set imprime en ``observaciones`` deben
        # coincidir exactamente con los que busca el enriquecedor.
        assert OBS_ENTREGA_GRATUITA == "ENTREGA GRATUITA"
        assert OBS_FACTOR_PROPORCIONALIDAD == "FACTOR PROPORCIONALIDAD"
        assert OBS_USO_COMUN == "USO COMUN"
        assert OBS_IVA_RETENIDO_TOTAL == "IVA RETENIDO TOTAL"
        assert OBS_RETENCION_TOTAL == "RETENCION TOTAL"

    def test_regex_guia_anulada_captura_numero_de_caso(self):
        # Patrón canónico sobre el texto literal del set.
        m = RE_GUIA_ANULADA_INSTRUCCION.search(
            "- EL CASO 3 CORRESPONDE A UNA GUIA ANULADA"
        )
        assert m is not None
        assert m.group(1) == "3"

    def test_regex_guia_facturada_captura_numero_de_caso(self):
        m = RE_GUIA_FACTURADA_INSTRUCCION.search(
            "- EL CASO 1 CORRESPONDE A UNA GUIA QUE SE FACTURO EN EL PERIODO"
        )
        assert m is not None
        assert m.group(1) == "1"


# ══════════════════════════════════════════════════════════════════
# Bloque 2 — Unit tests de las funciones canónicas
# ══════════════════════════════════════════════════════════════════


@dataclass
class _CasoFake:
    """Caso mínimo que implementa el Protocol ``_CasoVenta``."""
    set_nombre: str
    dte_emitido_id: str | None


class TestElegirSetLibroVentas:
    """Regla: si hay ambos sets, gana BASICO. Solo se consideran
    casos con DTE efectivamente emitido."""

    def test_ambos_sets_gana_basico(self):
        casos = [
            _CasoFake(set_nombre="BASICO", dte_emitido_id="dte-1"),
            _CasoFake(set_nombre="EXENTA", dte_emitido_id="dte-2"),
        ]
        assert elegir_set_libro_ventas(casos) == "BASICO"

    def test_solo_exenta_gana_exenta(self):
        casos = [_CasoFake(set_nombre="EXENTA", dte_emitido_id="dte-2")]
        assert elegir_set_libro_ventas(casos) == "EXENTA"

    def test_solo_basico_gana_basico(self):
        casos = [_CasoFake(set_nombre="BASICO", dte_emitido_id="dte-1")]
        assert elegir_set_libro_ventas(casos) == "BASICO"

    def test_caso_sin_dte_emitido_no_cuenta(self):
        # Un caso del set BASICO que NO tiene dte_emitido_id no puede
        # hacer ganar a BASICO — no hay nada que emitir.
        casos = [
            _CasoFake(set_nombre="BASICO", dte_emitido_id=None),
            _CasoFake(set_nombre="EXENTA", dte_emitido_id="dte-2"),
        ]
        assert elegir_set_libro_ventas(casos) == "EXENTA"

    def test_sets_desconocidos_ignorados(self):
        # GUIAS no participa del libro de ventas, aun si tuviera DTE.
        casos = [_CasoFake(set_nombre="GUIAS", dte_emitido_id="dte-g")]
        assert elegir_set_libro_ventas(casos) is None

    def test_sin_casos_devuelve_none(self):
        # None = fallback al modo producción en el consumidor.
        assert elegir_set_libro_ventas([]) is None


class TestFiltrarDteIdsParaLibroVentas:
    """Regla: devolver solo los DTE IDs del set elegido."""

    def test_ambos_sets_devuelve_solo_ids_de_basico(self):
        casos = [
            _CasoFake(set_nombre="BASICO", dte_emitido_id="dte-b1"),
            _CasoFake(set_nombre="BASICO", dte_emitido_id="dte-b2"),
            _CasoFake(set_nombre="EXENTA", dte_emitido_id="dte-e1"),
        ]
        assert filtrar_dte_ids_para_libro_ventas(casos) == ["dte-b1", "dte-b2"]

    def test_solo_exenta_devuelve_ids_de_exenta(self):
        casos = [
            _CasoFake(set_nombre="EXENTA", dte_emitido_id="dte-e1"),
            _CasoFake(set_nombre="EXENTA", dte_emitido_id="dte-e2"),
        ]
        assert filtrar_dte_ids_para_libro_ventas(casos) == ["dte-e1", "dte-e2"]

    def test_casos_sin_dte_no_se_incluyen(self):
        casos = [
            _CasoFake(set_nombre="BASICO", dte_emitido_id="dte-b1"),
            _CasoFake(set_nombre="BASICO", dte_emitido_id=None),
        ]
        assert filtrar_dte_ids_para_libro_ventas(casos) == ["dte-b1"]

    def test_sin_casos_devuelve_lista_vacia(self):
        # Lista vacía = señal al consumidor para caer a modo producción.
        assert filtrar_dte_ids_para_libro_ventas([]) == []

    def test_consumible_multiples_veces(self):
        """La función no debe depender de iterables de una sola pasada."""
        casos_gen = (
            _CasoFake(set_nombre="BASICO", dte_emitido_id=f"dte-{i}")
            for i in range(3)
        )
        # El Protocol permite cualquier iterable; el helper debe
        # materializar internamente si lo necesita.
        result = filtrar_dte_ids_para_libro_ventas(casos_gen)
        assert result == ["dte-0", "dte-1", "dte-2"]


@dataclass
class _CasoGuiaFake:
    """Caso mínimo que implementa el Protocol ``_CasoGuia``.

    Expone ``tipo_dte``, ``dte_emitido_id`` y opcionalmente ``estado``
    tal como los expone el modelo real ``CertificacionCaso``.
    ``estado`` default ``"aprobado"`` porque es la regla nueva (solo
    casos aprobados entran al libro); los tests que quieran probar
    el rechazo por estado setean otro valor.
    """
    tipo_dte: int
    dte_emitido_id: str | None
    estado: str = "aprobado"


@dataclass
class _CasoVentaConEstado:
    """``_CasoFake`` + ``estado`` — para tests de la regla de aprobados."""
    set_nombre: str
    dte_emitido_id: str | None
    estado: str = "aprobado"


@dataclass
class _CasoConTimestamps:
    """Caso con los timestamps reales del flujo de certificación SII.

    Regla del usuario: el libro solo puede tomar documentos que
    pasaron ``avance_declarado_at`` y ``aprobado_at``. Este fake
    permite probar esa regla explícitamente.
    """
    tipo_dte: int
    dte_emitido_id: str | None
    estado: str = "aprobado"
    avance_declarado_at: object = object()  # sentinel non-None por default
    aprobado_at: object = object()


class TestFiltrarDteIdsParaLibroGuias:
    """Regla: solo los casos del run que sean guía (tipo_dte=52) y
    estén efectivamente emitidos entran al libro de guías.

    El bug que motiva estos tests (cert 77829149-5, 2026-04-23):
        El libro de guías mezcló DTEs de la certificación previa (ya
        huérfanos en la BD) con los del set vigente. El SII aceptó
        schema pero rechazó SRH: "El Numero de Guias Venta/Traslado
        No Cuadra" — esperaba 3, recibió 6.
    """

    def test_tres_casos_guia_emitidos_devuelve_tres_ids(self):
        casos = [
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g1"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g2"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g3"),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == [
            "dte-g1", "dte-g2", "dte-g3",
        ]

    def test_casos_no_guia_se_ignoran(self):
        """Solo tipo_dte=52 entra al libro de guías.

        Previene que un caso de factura (33) o boleta (39) termine
        contaminando el libro de guías si hay un bug de clasificación.
        """
        casos = [
            _CasoGuiaFake(tipo_dte=33, dte_emitido_id="dte-f1"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g1"),
            _CasoGuiaFake(tipo_dte=39, dte_emitido_id="dte-b1"),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == ["dte-g1"]

    def test_casos_guia_sin_dte_se_ignoran(self):
        """Caso pendiente (``dte_emitido_id=None``) no entra al libro."""
        casos = [
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g1"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id=None),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g3"),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == [
            "dte-g1", "dte-g3",
        ]

    def test_sin_casos_devuelve_lista_vacia(self):
        # Lista vacía = señal al consumidor para caer a modo producción.
        assert filtrar_dte_ids_para_libro_guias([]) == []

    def test_solo_casos_no_emitidos_devuelve_lista_vacia(self):
        casos = [
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id=None),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id=None),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == []

    def test_preserva_orden_de_entrada(self):
        """El orden de salida respeta el orden de la secuencia de casos.

        Importante porque el libro de guías ordena por folio después, y
        el llamador necesita determinismo para los tests de integración.
        """
        casos = [
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-86"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-84"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-85"),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == [
            "dte-g-86", "dte-g-84", "dte-g-85",
        ]


class TestFiltrarDteIdsGuiasSoloAprobados:
    """Regla del usuario (cert 77829149-5, 2026-04-23):
        'los libros se hidratan SOLO con documentos aprobados del
        run actual. Un caso emitido pero no aprobado aún no puede
        aparecer en el libro.'
    """

    def test_caso_emitido_pero_no_aprobado_no_entra(self):
        casos = [
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-ok",  estado="aprobado"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-ng",  estado="emitido"),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == ["dte-g-ok"]

    def test_caso_rechazado_no_entra(self):
        casos = [
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-ok", estado="aprobado"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-rj", estado="rechazado"),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == ["dte-g-ok"]

    def test_caso_pendiente_no_entra(self):
        casos = [
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-ok", estado="aprobado"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-pn", estado="pendiente"),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == ["dte-g-ok"]

    def test_todos_no_aprobados_devuelve_vacio(self):
        """Sin casos aprobados, el libro no se puede hidratar."""
        casos = [
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-1", estado="emitido"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-2", estado="enviado"),
            _CasoGuiaFake(tipo_dte=52, dte_emitido_id="dte-g-3", estado="en_revision"),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == []

    def test_sin_atributo_estado_sigue_funcionando_legacy(self):
        """Retrocompat: objetos sin ``estado`` (tests viejos, modo
        producción) entran con solo tener ``dte_emitido_id``."""
        @dataclass
        class _SinEstado:
            tipo_dte: int
            dte_emitido_id: str | None
        casos = [_SinEstado(tipo_dte=52, dte_emitido_id="dte-g-1")]
        assert filtrar_dte_ids_para_libro_guias(casos) == ["dte-g-1"]


class TestFiltrarDteIdsGuiasExigeDeclaracionDeAvance:
    """Regla literal del usuario: 'debe tomar los datos de esos
    documentos que pasaron la aprobacion en declaracion de avance'.

    El triple-check es estado=aprobado + avance_declarado_at no-None +
    aprobado_at no-None. Los tres deben estar alineados.
    """

    def test_sin_avance_declarado_no_entra(self):
        """El operador aún no clickeó 'Declarar avance' en el wizard.

        Aunque el caso esté en estado='aprobado' (caso edge: estado
        desalineado con los timestamps), si no pasó por declaración
        de avance real, no puede entrar al libro.
        """
        casos = [
            _CasoConTimestamps(
                tipo_dte=52, dte_emitido_id="dte-g-ok",
                estado="aprobado",
                avance_declarado_at=None,  # ← no declaró avance
                aprobado_at=object(),
            ),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == []

    def test_sin_aprobado_at_no_entra(self):
        """Declaró avance pero aún no confirmó 'Marcar aprobado'."""
        casos = [
            _CasoConTimestamps(
                tipo_dte=52, dte_emitido_id="dte-g-ok",
                estado="aprobado",
                avance_declarado_at=object(),
                aprobado_at=None,  # ← no confirmó aprobación
            ),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == []

    def test_con_ambos_timestamps_y_estado_entra(self):
        casos = [
            _CasoConTimestamps(
                tipo_dte=52, dte_emitido_id="dte-g-ok",
                estado="aprobado",
                avance_declarado_at=object(),
                aprobado_at=object(),
            ),
        ]
        assert filtrar_dte_ids_para_libro_guias(casos) == ["dte-g-ok"]


class TestFiltrarDteIdsVentasSoloAprobados:
    """Misma regla para ventas: solo casos aprobados del set elegido."""

    def test_caso_basico_no_aprobado_no_entra(self):
        casos = [
            _CasoVentaConEstado(set_nombre="BASICO", dte_emitido_id="dte-b1", estado="aprobado"),
            _CasoVentaConEstado(set_nombre="BASICO", dte_emitido_id="dte-b2", estado="emitido"),
        ]
        assert filtrar_dte_ids_para_libro_ventas(casos) == ["dte-b1"]

    def test_si_todo_basico_sin_aprobar_y_exenta_aprobada_no_cae_a_exenta(self):
        """El set elegido NO cambia por el estado — si BASICO tiene
        casos (aunque ninguno aprobado), sigue siendo el set del libro.

        Esto previene que un error transitorio en BASICO "promocione"
        EXENTA al libro de ventas. El wizard debe bloquear la
        generación si el set elegido aún no tiene todos los casos
        aprobados (eso vive en el service, no en el filtro).
        """
        casos = [
            _CasoVentaConEstado(set_nombre="BASICO", dte_emitido_id="dte-b1", estado="emitido"),
            _CasoVentaConEstado(set_nombre="EXENTA", dte_emitido_id="dte-e1", estado="aprobado"),
        ]
        # BASICO sigue siendo el set elegido; como no hay aprobados de
        # BASICO, se devuelve lista vacía (el consumidor debe ver
        # vacío → no puede generar libro).
        assert filtrar_dte_ids_para_libro_ventas(casos) == []


class TestParsearCasosGuiasAnuladas:
    """Regla: extraer los N de 'EL CASO N CORRESPONDE A UNA GUIA ANULADA'."""

    def test_una_guia_anulada(self):
        instr = "- EL CASO 3 CORRESPONDE A UNA GUIA ANULADA"
        assert parsear_casos_guias_anuladas(instr) == {3}

    def test_multiples_guias_anuladas(self):
        instr = (
            "- EL CASO 2 CORRESPONDE A UNA GUIA ANULADA\n"
            "- EL CASO 5 CORRESPONDE A UNA GUIA ANULADA"
        )
        assert parsear_casos_guias_anuladas(instr) == {2, 5}

    def test_sin_anuladas_devuelve_set_vacio(self):
        instr = "- EL CASO 1 CORRESPONDE A UNA GUIA QUE SE FACTURO EN EL PERIODO"
        assert parsear_casos_guias_anuladas(instr) == set()

    def test_instrucciones_none_devuelve_set_vacio(self):
        assert parsear_casos_guias_anuladas(None) == set()

    def test_instrucciones_vacio_devuelve_set_vacio(self):
        assert parsear_casos_guias_anuladas("") == set()

    def test_case_insensitive(self):
        instr = "- el caso 7 corresponde a una guia anulada"
        assert parsear_casos_guias_anuladas(instr) == {7}

    def test_texto_mixto_solo_extrae_anuladas(self):
        instr = (
            "CONSTRUYA EL LIBRO CON LAS GUIAS...\n"
            "- EL CASO 1 CORRESPONDE A UNA GUIA QUE SE FACTURO EN EL PERIODO\n"
            "- EL CASO 3 CORRESPONDE A UNA GUIA ANULADA"
        )
        assert parsear_casos_guias_anuladas(instr) == {3}


class TestParsearCasosGuiasFacturadas:
    """Regla: extraer los N de 'EL CASO N CORRESPONDE A UNA GUIA QUE SE FACTURO'."""

    def test_una_guia_facturada(self):
        instr = "- EL CASO 1 CORRESPONDE A UNA GUIA QUE SE FACTURO EN EL PERIODO"
        assert parsear_casos_guias_facturadas(instr) == {1}

    def test_multiples_guias_facturadas(self):
        instr = (
            "- EL CASO 1 CORRESPONDE A UNA GUIA QUE SE FACTURO EN EL PERIODO\n"
            "- EL CASO 4 CORRESPONDE A UNA GUIA QUE SE FACTURO"
        )
        assert parsear_casos_guias_facturadas(instr) == {1, 4}

    def test_solo_anuladas_no_matchean(self):
        instr = "- EL CASO 3 CORRESPONDE A UNA GUIA ANULADA"
        assert parsear_casos_guias_facturadas(instr) == set()

    def test_none_vacio(self):
        assert parsear_casos_guias_facturadas(None) == set()
        assert parsear_casos_guias_facturadas("") == set()


class TestDetectarObservacionesCompra:
    """Regla: detectar triggers SII en el campo ``observaciones``."""

    def test_entrega_gratuita(self):
        flags = detectar_observaciones_compra("ENTREGA GRATUITA")
        assert flags["entrega_gratuita"] is True
        assert flags["iva_uso_comun"] is False
        assert flags["iva_retenido_total"] is False

    def test_factor_proporcionalidad_activa_uso_comun(self):
        flags = detectar_observaciones_compra("FACTOR PROPORCIONALIDAD")
        assert flags["iva_uso_comun"] is True

    def test_uso_comun_activa_uso_comun(self):
        flags = detectar_observaciones_compra("USO COMUN")
        assert flags["iva_uso_comun"] is True

    def test_iva_retenido_total(self):
        flags = detectar_observaciones_compra("IVA RETENIDO TOTAL")
        assert flags["iva_retenido_total"] is True

    def test_retencion_total_alias(self):
        flags = detectar_observaciones_compra("RETENCION TOTAL")
        assert flags["iva_retenido_total"] is True

    def test_case_insensitive(self):
        flags = detectar_observaciones_compra("entrega gratuita")
        assert flags["entrega_gratuita"] is True

    def test_observacion_combinada(self):
        # Un detalle real puede traer dos flags a la vez.
        flags = detectar_observaciones_compra("USO COMUN - ENTREGA GRATUITA")
        assert flags["entrega_gratuita"] is True
        assert flags["iva_uso_comun"] is True

    def test_none_todos_false(self):
        flags = detectar_observaciones_compra(None)
        assert flags == {
            "entrega_gratuita": False,
            "iva_uso_comun": False,
            "iva_retenido_total": False,
        }

    def test_vacio_todos_false(self):
        flags = detectar_observaciones_compra("")
        assert all(v is False for v in flags.values())


# ══════════════════════════════════════════════════════════════════
# Bloque 3 — Contrato con el código consumidor
# ══════════════════════════════════════════════════════════════════


class TestContratoConsumidores:
    """Los consumidores DEBEN importar desde ``instructivo_sii``.

    Esto previene que un fix local silencioso vuelva a reimplementar
    la regla ad-hoc e ignore el instructivo del SII. Ocurrió en cert
    77829149-5 (libro ventas incluyó EXENTA, libro guías no marcó
    anulada) — este módulo existe para que no vuelva a pasar.
    """

    def test_envio_libro_cert_importa_filtrar_dte_ids(self):
        from crumbpos.api.services import envio_libro_cert
        src = inspect.getsource(envio_libro_cert)
        assert "filtrar_dte_ids_para_libro_ventas" in src, (
            "envio_libro_cert.py debe importar "
            "filtrar_dte_ids_para_libro_ventas de instructivo_sii."
        )

    def test_envio_libro_cert_importa_filtrar_dte_ids_guias(self):
        """El libro de guías debe filtrar por casos del run.

        Si el consumidor reimplementa la selección de DTEs de guías
        (ej. ``query(DteEmitido).filter(tipo_dte=52)`` sin cruzar con
        ``CertificacionCaso.run_id``), vuelve a pasar el bug de la
        cert 77829149-5: se mezclan folios huérfanos de certificaciones
        anteriores con los del set vigente → SRH "No Cuadra".
        """
        from crumbpos.api.services import envio_libro_cert
        src = inspect.getsource(envio_libro_cert)
        assert "filtrar_dte_ids_para_libro_guias" in src, (
            "envio_libro_cert.py debe importar "
            "filtrar_dte_ids_para_libro_guias de instructivo_sii."
        )

    def test_envio_libro_cert_importa_parsear_anuladas(self):
        from crumbpos.api.services import envio_libro_cert
        src = inspect.getsource(envio_libro_cert)
        assert "parsear_casos_guias_anuladas" in src, (
            "envio_libro_cert.py debe importar "
            "parsear_casos_guias_anuladas de instructivo_sii."
        )

    def test_envio_libro_cert_importa_detectar_observaciones(self):
        from crumbpos.api.services import envio_libro_cert
        src = inspect.getsource(envio_libro_cert)
        assert "detectar_observaciones_compra" in src, (
            "envio_libro_cert.py debe importar "
            "detectar_observaciones_compra de instructivo_sii."
        )

    def test_envio_libro_cert_no_reimplementa_regex_anulada(self):
        """El consumidor no debe tener su propio regex de guía anulada.

        La fuente de verdad es ``RE_GUIA_ANULADA_INSTRUCCION`` en
        ``instructivo_sii``. Cualquier re.compile adicional sobre ese
        patrón es una reimplementación silenciosa.
        """
        from crumbpos.api.services import envio_libro_cert
        src = inspect.getsource(envio_libro_cert)
        # Heurística: buscar re.compile con el patrón característico
        # de "EL CASO ... CORRESPONDE A UNA GUIA ANULADA" (en cualquier
        # forma). Si aparece, el consumidor está reimplementando.
        patron_prohibido = re.compile(
            r"re\.compile\([^)]*GUIA\s*\\?s?\+?\s*ANULADA",
            re.IGNORECASE,
        )
        assert not patron_prohibido.search(src), (
            "envio_libro_cert.py no debe compilar su propio regex de "
            "guía anulada. Usa instructivo_sii.parsear_casos_guias_anuladas."
        )

    def test_envio_libro_cert_no_reimplementa_prioridad_sets(self):
        """El consumidor no debe hardcodear el orden BASICO→EXENTA.

        La prioridad vive en ``PRIORIDAD_SETS_LIBRO_VENTAS``; cualquier
        branch local ``if set_nombre == 'BASICO'`` para decidir
        prioridad es una regresión al modo ad-hoc.
        """
        from crumbpos.api.services import envio_libro_cert
        src = inspect.getsource(envio_libro_cert)
        # Prohibimos la combinación exacta de chequeos que implementaba
        # la prioridad localmente (``casos_basico = [... BASICO ...]``
        # seguido de un ``if casos_basico else casos``). Permite menciones
        # a "BASICO" en docstrings/comentarios.
        patron_prohibido = re.compile(
            r"casos_basico\s*=\s*\[[^]]*BASICO",
            re.IGNORECASE,
        )
        assert not patron_prohibido.search(src), (
            "envio_libro_cert.py no debe elegir el set prioritario "
            "localmente. Usa instructivo_sii.filtrar_dte_ids_para_libro_ventas."
        )

    def test_modulo_expone_api_publica_esperada(self):
        """La API pública no debe reducirse por error en un refactor."""
        publico_minimo = {
            "INSTRUCCION_LIBRO_VENTAS_SII",
            "INSTRUCCION_LIBRO_GUIAS_SII_EJEMPLO",
            "PRIORIDAD_SETS_LIBRO_VENTAS",
            "RE_GUIA_ANULADA_INSTRUCCION",
            "RE_GUIA_FACTURADA_INSTRUCCION",
            "OBS_ENTREGA_GRATUITA",
            "OBS_FACTOR_PROPORCIONALIDAD",
            "OBS_USO_COMUN",
            "OBS_IVA_RETENIDO_TOTAL",
            "OBS_RETENCION_TOTAL",
            "elegir_set_libro_ventas",
            "filtrar_dte_ids_para_libro_ventas",
            "filtrar_dte_ids_para_libro_guias",
            "parsear_casos_guias_anuladas",
            "parsear_casos_guias_facturadas",
            "detectar_observaciones_compra",
        }
        presentes = set(dir(instructivo_sii))
        faltantes = publico_minimo - presentes
        assert not faltantes, (
            f"instructivo_sii perdió símbolos públicos: {faltantes}"
        )
