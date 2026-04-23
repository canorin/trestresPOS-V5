"""Reglas CANÓNICAS del instructivo SII para libros electrónicos.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  CONTRATO DEL SOFTWARE CON EL SII — NO PARCHEAR NI IGNORAR  ⚠️
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Este módulo centraliza las reglas que el SII impone sobre los libros
electrónicos (ventas, compras, guías). Las reglas están expresadas en
dos formas paralelas:

1. **Cita literal** (constantes ``INSTRUCCION_*_SII``): el texto exacto
   que el SII entrega en el set de pruebas o en la documentación
   oficial. Snapshot auditable. Cualquier cambio al SII requiere
   actualizar la cita Y la implementación.

2. **Función ejecutable** (``filtrar_*``, ``parsear_*``): la regla
   aplicada. Todo el core que construye libros DEBE usar estas
   funciones — no reimplementar la lógica ad-hoc.

Los tests de ``tests/test_instructivo_sii.py`` verifican:
- Que las citas literales no hayan sido alteradas (snapshot).
- Que las funciones produzcan el resultado esperado sobre inputs
  conocidos (contratos).
- Que el código consumidor (``envio_libro_cert``, ``generador_iecv``)
  importe desde aquí y no reimplemente.

Violarlas produce rechazo automático del SII (estado SRH) con reparos
tipo "No Cuadra" / "No Informa Adecuadamente". Ya ocurrió con la
certificación 77829149-5 por ignorarlas; este módulo existe para que
NO vuelva a pasar.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping, Protocol


# ══════════════════════════════════════════════════════════════════
# Libro de Ventas — selección de documentos por set
# ══════════════════════════════════════════════════════════════════


INSTRUCCION_LIBRO_VENTAS_SII: str = (
    "CONSTRUYA EL LIBRO DE VENTAS CON LOS DOCUMENTOS CON QUE GENERO\n"
    "EL SET BASICO O EL SET DE FACTURA EXENTA, SEGUN CORRESPONDA.\n"
    "SI OBTUVO AMBOS SET, UTILICE LOS DOCUMENTOS DEL SET BASICO PARA\n"
    "CONSTRUIR EL LIBRO DE VENTAS."
)
"""Cita literal del campo ``libro_ventas_instrucciones`` del set de
pruebas SII. NO modificar sin actualizar ``tests/test_instructivo_sii``."""


# Orden de prioridad cuando un contribuyente emitió múltiples sets.
# El set de MAYOR prioridad gana; los demás se descartan del libro.
PRIORIDAD_SETS_LIBRO_VENTAS: tuple[str, ...] = ("BASICO", "EXENTA")


class _CasoVenta(Protocol):
    """Contrato mínimo que debe cumplir un caso para filtrarlo."""
    set_nombre: str
    dte_emitido_id: str | None


def elegir_set_libro_ventas(casos: Iterable[_CasoVenta]) -> str | None:
    """Devuelve el nombre del set que debe usarse para el libro de ventas.

    Regla SII aplicada (``INSTRUCCION_LIBRO_VENTAS_SII``):
        Si hay casos de múltiples sets, elegir el de mayor prioridad
        en ``PRIORIDAD_SETS_LIBRO_VENTAS``. Solo se consideran casos
        con ``dte_emitido_id`` no nulo (efectivamente emitidos).

    Returns:
        Nombre del set ("BASICO" o "EXENTA"), o ``None`` si no hay
        casos emitidos de ningún set conocido — en ese escenario el
        llamador debe caer al modo producción (libro mensual con
        todos los DTEs del período).
    """
    sets_presentes = {
        c.set_nombre for c in casos
        if c.dte_emitido_id is not None and c.set_nombre in PRIORIDAD_SETS_LIBRO_VENTAS
    }
    for preferido in PRIORIDAD_SETS_LIBRO_VENTAS:
        if preferido in sets_presentes:
            return preferido
    return None


def filtrar_dte_ids_para_libro_ventas(
    casos: Iterable[_CasoVenta],
) -> list[str]:
    """Devuelve los IDs de DTE que deben ir al libro de ventas.

    Solo los casos del set elegido por :func:`elegir_set_libro_ventas`
    que además estén en estado ``aprobado`` (cuando el caso expone un
    atributo ``estado`` — retrocompatibilidad con tests legacy y con
    el modo producción, donde no hay estado de certificación).

    Lista vacía si no hay casos aptos (señal al consumidor para caer
    a modo producción).

    Regla del usuario (cert 77829149-5, 2026-04-23): los libros se
    hidratan SOLO con documentos aprobados del run actual. Un caso
    emitido pero no aprobado aún no puede aparecer en el libro.
    """
    casos_list = list(casos)
    set_elegido = elegir_set_libro_ventas(casos_list)
    if set_elegido is None:
        return []
    return [
        c.dte_emitido_id  # type: ignore[misc]
        for c in casos_list
        if c.set_nombre == set_elegido and _caso_apto_para_libro(c)
    ]


# ══════════════════════════════════════════════════════════════════
# Libro de Guías — instrucciones sobre casos anulados / facturados
# ══════════════════════════════════════════════════════════════════


INSTRUCCION_LIBRO_GUIAS_SII_EJEMPLO: str = (
    "CONSTRUYA EL LIBRO CON LAS GUIAS CON QUE GENERO EL SET GUIA DE DESPACHO,\n"
    "TENIENDO EN CUENTA LAS SIGUIENTES CONSIDERACIONES\n"
    "\n"
    "- EL CASO N CORRESPONDE A UNA GUIA QUE SE FACTURO EN EL PERIODO\n"
    "- EL CASO N CORRESPONDE A UNA GUIA ANULADA"
)
"""Ejemplo literal del campo ``libro_guias_instrucciones``. El número
de caso (``N``) varía entre certificaciones pero el patrón es fijo.
NO modificar sin actualizar ``tests/test_instructivo_sii``."""


# Regex canónico — fuente de verdad para detectar guías anuladas
# desde el texto literal que entrega el SII.
RE_GUIA_ANULADA_INSTRUCCION: re.Pattern[str] = re.compile(
    r"EL\s+CASO\s+(\d+)\s+CORRESPONDE\s+A\s+UNA\s+GUIA\s+ANULADA",
    re.IGNORECASE,
)


# Regex canónico para detectar guías facturadas (informativo: no
# afecta totales del libro de guías, pero documenta la asociación
# guía↔factura en el libro de ventas para cuadre cruzado).
RE_GUIA_FACTURADA_INSTRUCCION: re.Pattern[str] = re.compile(
    r"EL\s+CASO\s+(\d+)\s+CORRESPONDE\s+A\s+UNA\s+GUIA\s+QUE\s+SE\s+FACTURO",
    re.IGNORECASE,
)


def parsear_casos_guias_anuladas(instrucciones: str | None) -> set[int]:
    """Devuelve el conjunto de números de caso marcados como ANULADOS.

    Aplica :data:`RE_GUIA_ANULADA_INSTRUCCION` al texto literal del
    campo ``libro_guias_instrucciones`` del set.
    """
    if not instrucciones:
        return set()
    return {int(m.group(1)) for m in RE_GUIA_ANULADA_INSTRUCCION.finditer(instrucciones)}


def parsear_casos_guias_facturadas(instrucciones: str | None) -> set[int]:
    """Devuelve el conjunto de números de caso marcados como FACTURADOS.

    Informativo: estas guías siguen siendo guías de venta normales en
    el libro de guías (no alteran ``TotGuiaVenta`` ni ``TotMntGuiaVta``).
    La regla se usa para cruce con el libro de ventas (la factura
    asociada debe referenciar esta guía).
    """
    if not instrucciones:
        return set()
    return {int(m.group(1)) for m in RE_GUIA_FACTURADA_INSTRUCCION.finditer(instrucciones)}


class _CasoGuia(Protocol):
    """Contrato mínimo que debe cumplir un caso para filtrarlo como guía."""
    tipo_dte: int
    dte_emitido_id: str | None
    # ``estado`` es opcional para mantener retrocompatibilidad con
    # tests antiguos. Cuando está presente, DEBE valer ``"aprobado"``
    # para que el caso entre al libro (ver regla abajo).


# Estados de ``CertificacionCaso`` que autorizan incluir el DTE en un libro.
# Regla literal del usuario (cert 77829149-5, 2026-04-23):
#     "si se emite una factura y esta en estado aprobado ahi recien los
#      libros se pueden generar, cuando estan todos los datos necesarios
#      aprobados y usando esos datos aprobados para hidratar sus libros,
#      no tomar de datos de otra parte y menos hardcodeados o de
#      certificaciones anteriores (...) debe tomar los datos de esos
#      documentos que pasaron la aprobacion en declaracion de avance"
#
# La palabra clave es "pasaron la declaración de avance": el libro
# solo puede consumir documentos donde ambos eventos del flujo SII
# quedaron registrados:
#   1. ``avance_declarado_at`` — el operador clickeó "Declarar avance"
#      (que en la práctica queda registrado en el portal SII).
#   2. ``aprobado_at`` — el operador confirmó "Marcar aprobado" tras
#      ver el resultado del SII.
#
# Se exige también ``estado == "aprobado"`` como triple-check (los
# tres deben estar alineados; cualquier desalineo es bug, no feature).
ESTADOS_CASO_APTO_PARA_LIBRO: frozenset[str] = frozenset({"aprobado"})


def _caso_apto_para_libro(caso) -> bool:
    """True si el caso puede alimentar un libro.

    Regla fuerte (cert 77829149-5):
    - Debe tener ``dte_emitido_id`` (DTE efectivamente emitido).
    - Si expone ``estado``, debe valer ``aprobado``.
    - Si expone ``avance_declarado_at``, debe ser no-None (el operador
      declaró avance en el portal SII).
    - Si expone ``aprobado_at``, debe ser no-None (confirmación final
      tras verificar el SII).

    Retrocompatibilidad:
    - Si el caso NO expone ninguno de los tres atributos de estado
      (``estado``, ``avance_declarado_at``, ``aprobado_at``), se
      acepta por retrocompatibilidad con tests legacy y modo
      producción (donde no hay certificación).
    """
    if getattr(caso, "dte_emitido_id", None) is None:
        return False

    # Si el caso no expone ningún atributo del flujo de certificación,
    # asumimos modo producción/legacy → aceptamos.
    tiene_estado = hasattr(caso, "estado")
    tiene_avance = hasattr(caso, "avance_declarado_at")
    tiene_aprobado = hasattr(caso, "aprobado_at")
    if not (tiene_estado or tiene_avance or tiene_aprobado):
        return True

    # Si expone ``estado``, debe ser aprobado.
    if tiene_estado and caso.estado not in ESTADOS_CASO_APTO_PARA_LIBRO:
        return False

    # Si expone timestamps del flujo, deben estar seteados.
    if tiene_avance and caso.avance_declarado_at is None:
        return False
    if tiene_aprobado and caso.aprobado_at is None:
        return False

    return True


def filtrar_dte_ids_para_libro_guias(
    casos: Iterable[_CasoGuia],
) -> list[str]:
    """Devuelve los IDs de DTE que deben ir al libro de guías.

    Regla SII + directiva del usuario (cert 77829149-5, 2026-04-23):
        Los libros solo pueden tomar datos de casos del run **actual**
        que estén en estado ``aprobado``. Nunca datos huérfanos de
        certificaciones anteriores, nunca datos hardcodeados, nunca
        datos "del período" sin cruzar por el set vigente. Paso a paso:
        si un caso no está aprobado, no entra.

    Concretamente devuelve los ``dte_emitido_id`` de los casos del run
    que:
      1. Sean guía de despacho (``tipo_dte == 52``).
      2. Tengan ``dte_emitido_id`` (emitido).
      3. Estén en ``estado == 'aprobado'`` (si el caso expone
         ``estado`` — retrocompatibilidad con tests que no lo
         expongan).

    Los DTEs tipo 52 que no estén asociados a un caso del run
    (huérfanos de certificaciones anteriores, o folios descartados
    que quedaron en la BD) **no entran** al libro.

    Devuelve lista vacía si no hay casos de guía aptos: señal al
    consumidor para caer al modo producción (query directa por período).

    Historia del bug (cert 77829149-5, 2026-04-23):
        El libro de guías tomaba todos los ``DteEmitido`` con
        ``tipo_dte=52`` y ``empresa_id=<emp>`` sin filtrar por run →
        mezclaba 3 guías del set anterior (folios 81-83, ya huérfanos)
        con 3 del set actual (folios 84-86). El SII aceptó schema pero
        rechazó SRH con "El Numero de Guias Venta/Traslado No Cuadra"
        porque esperaba 3 y recibió 6.
    """
    return [
        c.dte_emitido_id  # type: ignore[misc]
        for c in casos
        if c.tipo_dte == 52 and _caso_apto_para_libro(c)
    ]


# ══════════════════════════════════════════════════════════════════
# Libro de Compras — observaciones textuales del set
# ══════════════════════════════════════════════════════════════════


# Observaciones literales que el set de pruebas SII coloca en el
# campo ``observaciones`` de cada entrada de compras. Cada una
# cambia el XML emitido en el Detalle/ResumenPeriodo.
#
# Implementación en ``crumbpos.api.services.envio_libro_cert.enriquecer_entradas_compras``.
# Los tests de ``tests/test_envio_libro_cert.py::TestEnriquecerEntradasCompras``
# verifican cada obs.
OBS_ENTREGA_GRATUITA: str = "ENTREGA GRATUITA"
"""Trigger de ``IVANoRec`` con ``CodIVANoRec=4`` (Entrega Gratuita
del Proveedor). El IVA se reporta en ``<IVANoRec>`` y se OMITE de
``<MntIVA>`` del detalle y de ``<TotMntIVA>`` del resumen. El
``MntTotal`` sí conserva el IVA (MntExe + MntNeto + IVA)."""


OBS_FACTOR_PROPORCIONALIDAD: str = "FACTOR PROPORCIONALIDAD"
"""Trigger de ``IVAUsoComun`` (junto con ``USO COMUN``). Calcula
crédito proporcional con ``FctProp`` (default 0.60)."""


OBS_USO_COMUN: str = "USO COMUN"
"""Alias de :data:`OBS_FACTOR_PROPORCIONALIDAD` — mismo tratamiento."""


OBS_IVA_RETENIDO_TOTAL: str = "IVA RETENIDO TOTAL"
"""Trigger de ``IVARetTotal`` + ``OtrosImp`` con ``CodImp=15``.
``MntTotal`` se fija a ``MntNeto`` (regla 25 SII)."""


OBS_RETENCION_TOTAL: str = "RETENCION TOTAL"
"""Alias de :data:`OBS_IVA_RETENIDO_TOTAL` — mismo tratamiento."""


def detectar_observaciones_compra(observaciones: str | None) -> Mapping[str, bool]:
    """Devuelve un dict de flags por observación detectada.

    Permite al enriquecedor de compras decidir con una única función
    qué reglas aplicar sobre una entrada, en lugar de repetir checks
    ``in`` por todos lados.
    """
    obs = (observaciones or "").upper()
    return {
        "entrega_gratuita": OBS_ENTREGA_GRATUITA in obs,
        "iva_uso_comun": (
            OBS_FACTOR_PROPORCIONALIDAD in obs or OBS_USO_COMUN in obs
        ),
        "iva_retenido_total": (
            OBS_IVA_RETENIDO_TOTAL in obs or OBS_RETENCION_TOTAL in obs
        ),
    }
