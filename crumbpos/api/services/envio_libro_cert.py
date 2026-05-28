"""Generación, envío y consulta de Libros (Ventas, Compras, Guías)
para certificación SII.

Sigue el mismo patrón de ``envio_sobre_cert.py`` — tres operaciones:

1. ``generar_libro``: genera XML, firma con type="libro", persiste en
   ``CertificacionLibro.xml_libro`` (base64). Idempotente — puede
   llamarse varias veces para regenerar.
2. ``enviar_libro``: genera (si no hay XML) + envía al SII + persiste
   trackid y estado_sii en ``CertificacionLibro``.
3. ``consultar_estado_libro``: polling SII por trackid + actualiza
   ``estado_sii`` del libro.

La firma usa la misma ``Firma`` que ``ServicioEmisionDTE`` pero con
``type="libro"`` (el XML del libro usa un esquema distinto al de
EnvioDTE).

Para libro de **ventas** y **guías**: lee los ``DteEmitido`` de la BD
de certificación de la empresa (los DTEs emitidos por el wizard en los
sets). Para libro de **compras**: toma las entradas del parser
almacenadas en ``CertificacionLibro.datos.entradas`` y las enriquece
al formato que espera ``generar_libro_compras``.

R6 (preguntar antes de enviar): se cumple en la capa UI, igual que
para los sobres. El wizard muestra resumen + SHA-256 antes de que el
usuario confirme el envío.

R8 (EPR no es aprobado): ``estado='enviado'`` solo significa que el
SII recibió el libro. LOK/SOK no implican que el SET de pruebas pasó.
La aprobación final es Fase 5.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

# ── Tipos válidos para TpoDocRef en Detalle del libro de VENTAS ──────────────
# Solo liquidaciones: T40 (liquidación papel), T43 (liquidación electrónica),
# T103 (liquidación electrónica DTE). Cualquier otro valor (ej: T33, T34, T52)
# produce reparo LBR-2 y BLOQUEA la declaración de avance en certificación.
_TIPOS_REF_VALIDOS_VENTA: frozenset[int] = frozenset({40, 43, 103})

from crumbpos.api.services.emision_dte import ServicioEmisionDTE
from crumbpos.core.libros.generador_iecv import (
    generar_libro_compras,
    generar_libro_guias,
    generar_libro_ventas,
)
from crumbpos.core.libros.instructivo_sii import (
    detectar_observaciones_compra,
    filtrar_dte_ids_para_libro_guias,
    filtrar_dte_ids_para_libro_ventas,
    parsear_casos_guias_anuladas,
)
from crumbpos.core.sii_client.envio import (
    consultar_estado_envio,
    enviar_dte,
)
from crumbpos.db.models import (
    CertificacionCaso,
    CertificacionLibro,
    CertificacionRun,
    DteEmitido,
    Empresa,
)

logger = logging.getLogger(__name__)

# Estados SII que indican rechazo — el libro no fue aceptado.
ESTADOS_RECHAZO = {"RCH", "RFR", "RSC", "SRH", "LRH", "LNC", "LRE"}


# ══════════════════════════════════════════════════════════════════
# Helpers internos
# ══════════════════════════════════════════════════════════════════


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parsear_estado_sii(raw_xml: str) -> str | None:
    """Extrae ``<ESTADO>`` de la respuesta SOAP del SII."""
    if not raw_xml:
        return None
    m = re.search(r"<ESTADO>([A-Z0-9_-]+)</ESTADO>", raw_xml)
    return m.group(1) if m else None


def _parsear_glosa_sii(raw_xml: str) -> str | None:
    """Extrae glosa de la respuesta SOAP del SII."""
    if not raw_xml:
        return None
    for tag in ("GLOSA_ERR", "GLOSA_ESTADO", "GLOSA"):
        m = re.search(rf"<{tag}>([^<]+)</{tag}>", raw_xml)
        if m:
            return m.group(1).strip()
    return None


def _derivar_periodo(session: Any, empresa: Empresa) -> str:
    """Derivar YYYY-MM desde los DteEmitido de la BD de certificación.

    Si no hay DTEs, usa el mes actual como fallback.
    """
    dte = session.query(DteEmitido).filter(
        DteEmitido.empresa_id == empresa.id,
    ).order_by(DteEmitido.fecha_emision.desc()).first()
    if dte and dte.fecha_emision:
        if hasattr(dte.fecha_emision, "strftime"):
            return dte.fecha_emision.strftime("%Y-%m")
        return str(dte.fecha_emision)[:7]
    return datetime.now().strftime("%Y-%m")


def _derivar_fecha_emision(session: Any, empresa: Empresa) -> str:
    """Derivar YYYY-MM-DD de la primera emisión (para FchDoc de compras)."""
    dte = session.query(DteEmitido).filter(
        DteEmitido.empresa_id == empresa.id,
    ).order_by(DteEmitido.fecha_emision).first()
    if dte and dte.fecha_emision:
        if hasattr(dte.fecha_emision, "strftime"):
            return dte.fecha_emision.strftime("%Y-%m-%d")
        return str(dte.fecha_emision)[:10]
    return datetime.now().strftime("%Y-%m-%d")


def enriquecer_entradas_compras(
    entradas_raw: list[dict],
    empresa: Empresa,
    fecha: str,
) -> list[dict]:
    """Mapea entradas del parser (CompraLibro) al formato del generador.

    El parser captura: ``tipo_doc``, ``folio``, ``observaciones``,
    ``monto_exento``, ``monto_afecto``.

    El generador (``generar_libro_compras``) necesita: ``TpoDoc``,
    ``NroDoc``, ``FchDoc``, ``RUTDoc``, ``RznSoc``, ``MntNeto``,
    ``MntIVA``, ``MntTotal``, y campos opcionales ``IVANoRec``,
    ``OtrosImp``, ``IVARetTotal`` según las observaciones del set.

    En certificación, el RUT/RznSoc del proveedor es el de la propia
    empresa (ella se compra a sí misma en el test).

    Si la entrada ya tiene ``TpoDoc`` (fue enriquecida previamente),
    se devuelve tal cual.
    """
    result = []
    tasa_imp = 19

    for raw in entradas_raw:
        # Si ya está en formato generador, pasar directo
        if "TpoDoc" in raw and "RUTDoc" in raw:
            result.append(raw)
            continue

        tpo_doc = raw.get("tipo_doc")
        nro_doc = raw.get("folio")
        mnt_exento = raw.get("monto_exento", 0) or 0
        mnt_afecto = raw.get("monto_afecto", 0) or 0

        # Flags de observaciones SII (fuente de verdad:
        # instructivo_sii.detectar_observaciones_compra).
        flags = detectar_observaciones_compra(raw.get("observaciones"))

        mnt_neto = mnt_afecto
        mnt_iva = round(mnt_neto * tasa_imp / 100) if mnt_neto else 0
        mnt_total = mnt_exento + mnt_neto + mnt_iva

        entry: dict[str, Any] = {
            "TpoDoc": tpo_doc,
            "NroDoc": nro_doc,
            "TpoImp": 1,
            "TasaImp": tasa_imp,
            "FchDoc": fecha,
            "RUTDoc": empresa.rut,
            "RznSoc": empresa.razon_social,
        }

        if mnt_exento:
            entry["MntExe"] = mnt_exento
        if mnt_neto:
            entry["MntNeto"] = mnt_neto

        # ── Observaciones → campos especiales de IVA ──
        # Regla 28 de certificación: CodIVANoRec debe coincidir con
        # la observación del set.
        tiene_iva_no_rec = False
        tiene_iva_ret_total = False

        # MntTotal se mantiene = MntExe + MntNeto + IVA en ambos branches:
        # el IVA es parte del total de la operación aunque no sea
        # recuperable por el receptor (entrega gratuita) o sólo lo sea
        # parcialmente (uso común). El SII valida la identidad
        # MntTotal = MntExe + MntNeto + IVA total y levanta
        # ``LBR-2 Reparo en Calculo de [MntTotal]`` cuando no cuadra
        # (bug detectado en cert 77829149-5, 2026-04-22).
        if flags["entrega_gratuita"]:
            entry["IVANoRec"] = {
                "CodIVANoRec": 4,
                "MntIVANoRec": mnt_iva,
            }
            # Solo NO emitimos ``MntIVA`` en el detalle; el IVA ya fue
            # contabilizado en ``mnt_total`` arriba.
            mnt_iva = 0
            tiene_iva_no_rec = True

        elif flags["iva_uso_comun"]:
            entry["IVAUsoComun"] = round(mnt_neto * tasa_imp / 100) if mnt_neto else 0
            # Idem: IVA sigue en ``mnt_total``, solo lo sacamos de MntIVA.
            mnt_iva = 0

        if flags["iva_retenido_total"]:
            iva_ret = round(mnt_neto * tasa_imp / 100) if mnt_neto else 0
            entry["IVARetTotal"] = iva_ret
            entry["OtrosImp"] = {
                "CodImp": 15,
                "TasaImp": tasa_imp,
                "MntImp": iva_ret,
            }
            # Regla 25: MntTotal = MntNeto para retención total
            mnt_total = mnt_neto
            tiene_iva_ret_total = True

        # Solo emitir MntIVA cuando hay IVA real (no NoRec, no retenido)
        if mnt_iva and not tiene_iva_no_rec and not tiene_iva_ret_total:
            entry["MntIVA"] = mnt_iva

        entry["MntTotal"] = mnt_total
        result.append(entry)

    return result


# ── Selección de DTEs por set (certificación) ──────────────────────
#
# La regla canónica vive en ``crumbpos.core.libros.instructivo_sii``
# (véase ``INSTRUCCION_LIBRO_VENTAS_SII``). Este módulo solo orquesta
# la carga desde la BD — NUNCA reimplementa el criterio de selección.


def _cargar_dtes_venta(session: Any, run: CertificacionRun, empresa: Empresa) -> list:
    """Devuelve los DTEs que deben ir al libro de ventas.

    Delega la regla de selección a
    :func:`crumbpos.core.libros.instructivo_sii.filtrar_dte_ids_para_libro_ventas`
    (fuente de verdad del instructivo SII).

    Modo:
    - **Certificación**: hay casos del set BASICO/EXENTA con DTEs
      asociados → usar los IDs devueltos por el módulo canónico
      (BASICO tiene prioridad sobre EXENTA).
    - **Producción**: no hay casos de certificación → fallback a la
      query clásica por tipo_dte, que incluye todos los DTEs de venta
      del período.
    """
    casos = session.query(CertificacionCaso).filter(
        CertificacionCaso.run_id == run.id,
    ).all()

    dte_ids = filtrar_dte_ids_para_libro_ventas(casos)
    if dte_ids:
        return (
            session.query(DteEmitido)
            .filter(DteEmitido.id.in_(dte_ids))
            .order_by(DteEmitido.tipo_dte, DteEmitido.folio)
            .all()
        )

    # Modo producción: todos los DTEs del tipo venta.
    tipos_venta = [33, 34, 56, 61]
    return (
        session.query(DteEmitido)
        .filter(
            DteEmitido.empresa_id == empresa.id,
            DteEmitido.tipo_dte.in_(tipos_venta),
        )
        .order_by(DteEmitido.tipo_dte, DteEmitido.folio)
        .all()
    )


def _cargar_dtes_guia(session: Any, run: CertificacionRun, empresa: Empresa) -> list:
    """Devuelve los DTEs que deben ir al libro de guías.

    Delega la regla de selección a
    :func:`crumbpos.core.libros.instructivo_sii.filtrar_dte_ids_para_libro_guias`
    (fuente de verdad del instructivo SII). La regla es:

        Solo los casos del run con ``tipo_dte=52`` y ``dte_emitido_id``
        seteado. Adicionalmente, si el caso expone los timestamps del
        flujo (``avance_declarado_at``, ``aprobado_at``) deben estar
        seteados — el libro solo consume documentos que **pasaron la
        declaración de avance**.

    Modo:
    - **Certificación**: hay casos del set de guías con DTEs asociados
      y aprobados → usar los IDs devueltos por el módulo canónico.
    - **Producción**: no hay casos de certificación (``filtrar_*``
      devuelve lista vacía) → fallback a la query clásica por
      ``tipo_dte=52``, que incluye todos los DTEs de guía del período.

    Historia del bug (cert 77829149-5, 2026-04-23):
        La query antigua era ``query(DteEmitido).filter(empresa_id=X,
        tipo_dte=52)`` — sin cruzar por casos del run. Resultado: se
        mezclaban folios huérfanos de certificaciones anteriores con
        los del set vigente → SRH "El Numero de Guias ... No Cuadra"
        en la declaración de avance.
    """
    casos = session.query(CertificacionCaso).filter(
        CertificacionCaso.run_id == run.id,
    ).all()

    dte_ids = filtrar_dte_ids_para_libro_guias(casos)
    if dte_ids:
        return (
            session.query(DteEmitido)
            .filter(DteEmitido.id.in_(dte_ids))
            .order_by(DteEmitido.folio)
            .all()
        )

    # Modo producción: todos los DTEs de guía del período.
    return (
        session.query(DteEmitido)
        .filter(
            DteEmitido.empresa_id == empresa.id,
            DteEmitido.tipo_dte == 52,
        )
        .order_by(DteEmitido.folio)
        .all()
    )


def _folios_guias_anuladas_por_instruccion(
    session: Any, run: CertificacionRun,
) -> set[int]:
    """Devuelve los folios de guías que el set marca como ANULADAS.

    Delega el parsing de la instrucción literal a
    :func:`crumbpos.core.libros.instructivo_sii.parsear_casos_guias_anuladas`
    (fuente de verdad del instructivo SII) y solo resuelve
    número_caso → folio contra la BD de certificación.

    Si no hay instrucciones o ningún caso matchea, devuelve set vacío.
    """
    datos = run.datos_parseados or {}
    instrucciones = datos.get("libro_guias_instrucciones", "") or ""
    numeros_caso = parsear_casos_guias_anuladas(instrucciones)
    if not numeros_caso:
        return set()

    folios: set[int] = set()
    casos_guias = session.query(CertificacionCaso).filter(
        CertificacionCaso.run_id == run.id,
        CertificacionCaso.set_nombre == "GUIAS",
    ).all()
    for caso in casos_guias:
        # numero_caso tiene formato "NNNNNNN-N" (ej. "4788486-3"). El
        # sufijo después del último guión es el número dentro del set.
        sufijo = caso.numero_caso.rsplit("-", 1)[-1]
        if sufijo.isdigit() and int(sufijo) in numeros_caso and caso.folio:
            folios.add(caso.folio)
    return folios


# ══════════════════════════════════════════════════════════════════
# Guardia pre-firma: validación anti-LBR-2
# ══════════════════════════════════════════════════════════════════


def _validar_xml_libro_ventas_sin_lbr2(xml_str: str) -> None:
    """Verifica que el XML del libro de ventas no contenga entradas que
    producirían reparo LBR-2 en el SII.

    Regla SII: en el Detalle del libro de ventas, el campo ``<TpoDocRef>``
    SOLO es válido para liquidaciones (T40, T43, T103). Si aparece con
    cualquier otro valor (T33, T34, T52…) el SII emite reparo LBR-2
    "Reparo en Calculo de [TpoDoc] debe ser [40, 43, 103]", que BLOQUEA
    la declaración de avance en certificación.

    Esta función se llama **después de generar el XML y antes de firmarlo**:
    actúa como circuito de corte para que cualquier regresión en el
    generador o en código auxiliar sea detectada antes de desperdiciar
    folios enviando al SII.

    Raises:
        ValueError: si encuentra algún ``<TpoDocRef>`` con valor inválido
        dentro de un ``<Detalle>`` de TpoDoc 56 o 61.
    """
    # Buscar pares (TpoDoc, TpoDocRef) dentro de cada <Detalle>...</Detalle>.
    # El XML del libro puede estar en una sola línea o multi-línea.
    detalle_bloques = re.findall(
        r"<Detalle>.*?</Detalle>", xml_str, re.DOTALL,
    )
    for bloque in detalle_bloques:
        tpo_doc_m = re.search(r"<TpoDoc>(\d+)</TpoDoc>", bloque)
        tpo_ref_m = re.search(r"<TpoDocRef>(\d+)</TpoDocRef>", bloque)
        if tpo_ref_m is None:
            continue  # sin TpoDocRef → OK
        tpo_doc = int(tpo_doc_m.group(1)) if tpo_doc_m else None
        tpo_ref = int(tpo_ref_m.group(1))
        if tpo_ref not in _TIPOS_REF_VALIDOS_VENTA:
            raise ValueError(
                f"[ANTI-LBR-2] Libro de ventas contiene TpoDocRef={tpo_ref} "
                f"en Detalle de TpoDoc={tpo_doc}. "
                f"Solo se permiten {sorted(_TIPOS_REF_VALIDOS_VENTA)} "
                f"(liquidaciones). Este XML produciría reparo LBR-2 en SII "
                f"y bloquearía la declaración de avance. "
                f"Verifica crumbpos/core/libros/generador_iecv.py — "
                f"_TIPOS_REF_VALIDOS_VENTA."
            )


# ══════════════════════════════════════════════════════════════════
# Funciones públicas
# ══════════════════════════════════════════════════════════════════


def generar_libro(
    session: Any,
    run: CertificacionRun,
    libro_id: str,
    servicio: ServicioEmisionDTE,
    empresa: Empresa,
) -> dict:
    """Genera, firma y persiste el XML del libro en CertificacionLibro.

    Idempotente: puede llamarse varias veces. Cada llamada regenera
    el XML y lo sobreescribe en ``xml_libro``. Útil para la vista
    previa del modal de confirmación R6.

    Args:
        session: sesión SQLAlchemy sobre la BD de certificación.
        run: ``CertificacionRun`` activa.
        libro_id: ID del ``CertificacionLibro``.
        servicio: ``ServicioEmisionDTE`` (para la Firma).
        empresa: modelo ``Empresa``.

    Returns:
        Dict con ``xml_bytes``, ``sha256``, ``tipo_libro``,
        ``tamano_bytes``.

    Raises:
        ValueError: libro no encontrado, sin datos, o sin DTEs.
        RuntimeError: error de firma.
    """
    libro = session.get(CertificacionLibro, libro_id)
    if not libro:
        raise ValueError(f"Libro {libro_id} no encontrado.")
    if libro.run_id != run.id:
        raise ValueError(f"Libro {libro_id} no pertenece a la run {run.id}.")

    folio_notificacion = libro.numero_atencion or 0
    rut_envia = servicio.config.rut_firmante or servicio.config.rut
    periodo = _derivar_periodo(session, empresa)

    # TipoEnvio para IECV (ventas y compras):
    #   - TOTAL  → primer envío del período (primer_envio_sii_at es None).
    #   - AJUSTE → re-envío correctivo después de un TOTAL ya aceptado
    #              por el SII (primer_envio_sii_at está seteado y se
    #              preserva en ``reiniciar_envio_libro``).
    #
    # El SII devuelve LNC ("Tipo de Envio de Libro No Corresponde") si se
    # envía TOTAL cuando ya existe un libro para ese FolioNotificacion/
    # período — exige AJUSTE.  Para IECV el AJUSTE usa el mismo
    # FolioNotificacion; no requiere un N° de Atención nuevo.
    #
    # ── SEMÁNTICA DELTA DEL AJUSTE IECV ──────────────────────────────────
    # El SII procesa el AJUSTE como REEMPLAZO PARCIAL del libro existente:
    # solo las entradas presentes en el Detalle del AJUSTE se "actualizan".
    # Enviar el libro completo (T33 + T56 + T61) como AJUSTE causa LBR-3
    # ("No Hay Resumen Para Informacion de Detalle") en los tipos no
    # cambiados, porque el SII detecta que T33/T56 en el Detalle son
    # idénticos al TOTAL y no tienen justificación en el ResumenPeriodo.
    #
    # Para un AJUSTE correcto: incluir SOLO los documentos que cambiaron
    # en el Detalle, y SOLO los tipos afectados en el ResumenPeriodo.
    # TODO: implementar generación de Detalle delta para AJUSTE IECV.
    #
    # ── REPAROS LBR-2 — BLOQUEAN AVANCE EN CERTIFICACIÓN ────────────────
    # "Reparo en Calculo de [TpoDoc] debe ser [40, 43, 103]" para NC (T61)
    # que referencian T33/T34/T52.
    #
    # CAUSA RAÍZ (código antiguo, corregido 2026-05-27):
    #   El generador ponía <TpoDocRef>33</TpoDocRef> en el Detalle del
    #   LIBRO para entradas T61, campo válido SOLO para {40, 43, 103}.
    #   Fix: _TIPOS_REF_VALIDOS_VENTA = frozenset({40, 43, 103}) en
    #   generador_iecv.py — nuevos libros ya NO producen LBR-2.
    #
    # IMPACTO REAL (confirmado por evaluador SII 2026-05-27):
    #   LBR-2 SÍ BLOQUEA la declaración de avance en el portal de
    #   certificación — el portal NO permite avance si hay reparos.
    #
    # QUÉ NO HACER si el TOTAL ya está LOK+LBR-2:
    #   • NO enviar AJUSTE: T61-solo → LBR-3 ("No Hay Resumen Para
    #     Información de Detalle") + LBR-2 cascada → LRH.
    #   • NO resetear primer_envio_sii_at y enviar nuevo TOTAL: SII
    #     rechaza con LNC ("Tipo de Envio de Libro No Corresponde").
    #   • Contactar al evaluador SII con el N° de atención para que
    #     procese el avance manualmente.
    #
    # Con el fix aplicado (2026-05-27) los libros nuevos no tendrán
    # LBR-2 y el avance procede normalmente.
    #
    # LibroGuia: SIEMPRE TOTAL. El esquema LibroGuia_v10.xsd solo acepta
    # TOTAL/PARCIAL. Un re-envío de guías necesita N° de Atención nuevo.
    es_reenvio_iecv = (
        libro.tipo_libro in ("ventas", "compras")
        and libro.primer_envio_sii_at is not None
    )
    tipo_envio_iecv = "AJUSTE" if es_reenvio_iecv else "TOTAL"

    servicio._cargar_firma()
    firma = servicio._firma

    # ── Generar XML según tipo_libro ──
    if libro.tipo_libro == "ventas":
        dtes = _cargar_dtes_venta(session, run, empresa)
        if not dtes:
            raise ValueError(
                "No hay DTEs de venta emitidos para generar el libro. "
                "Emite los sets básico y/o exenta primero."
            )
        # ── AJUSTE delta: solo los tipos que cambiaron ────────────────────
        # El SII procesa IECV AJUSTE como reemplazo parcial por TipoDoc:
        # solo los TpoDoc presentes en el Detalle/ResumenPeriodo del AJUSTE
        # se actualizan; los no incluidos se conservan del TOTAL original.
        # Enviar tipos no modificados en un AJUSTE provoca LBR-3 ("No Hay
        # Resumen Para Informacion de Detalle") — el SII detecta entradas
        # idénticas al TOTAL sin justificación de corrección.
        #
        # En certificación, el único tipo que cambió es T61: se eliminó
        # TpoDocRef del libro para NCs que referencian T33/T34/T52 (ese
        # campo es válido solo para liquidaciones T40/T43/T103). T33 y T56
        # son idénticos al TOTAL y NO deben aparecer en el AJUSTE.
        #
        # Fallback: si no hay T61 en el set (p.ej. producción con solo
        # T33/T34), se envía la lista completa — todos los tipos cambiaron.
        if es_reenvio_iecv:
            dtes_delta = [d for d in dtes if d.tipo_dte == 61]
            if dtes_delta:
                logger.info(
                    "AJUSTE delta ventas: %d DTEs totales → %d T61 solamente",
                    len(dtes), len(dtes_delta),
                )
                dtes = dtes_delta
        xml_str, xml_libro_id = generar_libro_ventas(
            dtes=dtes,
            empresa=empresa,
            periodo=periodo,
            rut_envia=rut_envia,
            folio_notificacion=folio_notificacion,
            tipo_envio=tipo_envio_iecv,
        )

    elif libro.tipo_libro == "guias":
        dtes = _cargar_dtes_guia(session, run, empresa)
        if not dtes:
            raise ValueError(
                "No hay Guías de Despacho emitidas y aprobadas para "
                "generar el libro. Emite el set de guías y aprueba "
                "los casos antes de generar el libro — los libros se "
                "hidratan solo con documentos que pasaron la "
                "declaración de avance."
            )
        folios_anulados = _folios_guias_anuladas_por_instruccion(session, run)
        xml_str, xml_libro_id = generar_libro_guias(
            dtes=dtes,
            empresa=empresa,
            periodo=periodo,
            rut_envia=rut_envia,
            folio_notificacion=folio_notificacion,
            folios_anulados=folios_anulados,
        )

    elif libro.tipo_libro == "compras":
        datos = libro.datos or {}
        entradas_raw = datos.get("entradas", [])
        if not entradas_raw:
            raise ValueError(
                "Libro de compras sin entradas. "
                "El set de pruebas no incluye libro de compras o no fue parseado."
            )
        fecha = _derivar_fecha_emision(session, empresa)
        entradas = enriquecer_entradas_compras(entradas_raw, empresa, fecha)
        xml_str, xml_libro_id = generar_libro_compras(
            dtes=entradas,
            empresa=empresa,
            periodo=periodo,
            rut_envia=rut_envia,
            folio_notificacion=folio_notificacion,
            tipo_envio=tipo_envio_iecv,
        )

    else:
        raise ValueError(f"tipo_libro desconocido: {libro.tipo_libro}")

    # ── Guardia anti-LBR-2 (solo libro de ventas) ────────────────────────
    # Valida que el XML generado no contenga TpoDocRef con valores fuera de
    # {40, 43, 103}. Detecta regresiones en el generador ANTES de firmar y
    # enviar, evitando desperdiciar el TOTAL del período con LBR-2.
    if libro.tipo_libro == "ventas":
        _validar_xml_libro_ventas_sin_lbr2(xml_str)

    # ── Firmar con type="libro" ──
    signed = firma.firmar(xml_str, xml_libro_id, type="libro")
    if not signed:
        raise RuntimeError(
            f"Error firmando libro {libro.tipo_libro}: "
            f"{getattr(firma, 'errores', 'sin detalle')}"
        )

    xml_final = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed
    xml_bytes = xml_final.encode("ISO-8859-1")
    sha256 = _sha256_hex(xml_bytes)

    # Persistir XML en el modelo
    libro.xml_libro = base64.b64encode(xml_bytes).decode("ascii")
    libro.estado = "generando"
    libro.error_mensaje = None
    session.commit()

    logger.info(
        "Libro cert generado: id=%s, tipo=%s, sha256=%s, bytes=%d",
        libro_id, libro.tipo_libro, sha256[:16], len(xml_bytes),
    )

    return {
        "xml_bytes": xml_bytes,
        "sha256": sha256,
        "tipo_libro": libro.tipo_libro,
        "tamano_bytes": len(xml_bytes),
    }


def enviar_libro(
    session: Any,
    run: CertificacionRun,
    libro_id: str,
    servicio: ServicioEmisionDTE,
    empresa: Empresa,
) -> dict:
    """Genera (si falta XML) + envía al SII + persiste trackid.

    Debe ser invocado SOLO después de confirmación humana explícita
    (R6). El wizard muestra el resumen/SHA-256 primero.

    Resultado en la BD:
        - Si el SII responde con trackid: ``estado='enviado'``,
          ``estado_sii='enviado'``, ``trackid=<id>``.
        - Si el SII rechaza: ``error_mensaje`` seteado, estado no
          cambia (para poder reintentar).

    Returns:
        Dict con ``ok``, ``trackid``, ``status``, ``glosa``,
        ``sha256``, ``raw``.
    """
    libro = session.get(CertificacionLibro, libro_id)
    if not libro:
        raise ValueError(f"Libro {libro_id} no encontrado.")
    if libro.run_id != run.id:
        raise ValueError(f"Libro {libro_id} no pertenece a la run {run.id}.")

    # Generar si no hay XML persisted
    if not libro.xml_libro:
        resultado_gen = generar_libro(session, run, libro_id, servicio, empresa)
        xml_bytes = resultado_gen["xml_bytes"]
        sha256 = resultado_gen["sha256"]
    else:
        xml_bytes = base64.b64decode(libro.xml_libro)
        sha256 = _sha256_hex(xml_bytes)

    # Enviar al SII
    token = servicio._obtener_token()
    rut_envia = servicio.config.rut_firmante or servicio.config.rut

    logger.info(
        "Enviando libro cert: tipo=%s, rut=%s, sha256=%s",
        libro.tipo_libro, servicio.config.rut, sha256[:16],
    )
    resp = enviar_dte(
        xml_bytes=xml_bytes,
        token=token,
        rut_emisor=servicio.config.rut,
        ambiente=servicio.config.ambiente,
        rut_envia=rut_envia,
    )

    trackid = resp.get("track_id")
    status = resp.get("status")
    glosa = (resp.get("glosa") or "").strip()

    if not trackid:
        raw_resp = resp.get("raw") or ""
        libro.error_mensaje = (
            f"Rechazo SII libro {libro.tipo_libro}: "
            f"{glosa or status or 'sin glosa'}"
        )[:500]
        # Resetear estado: ``generar_libro`` dejó ``estado='generando'``
        # antes del envío; si no lo devolvemos a ``pendiente`` la UI
        # queda pegada mostrando "Generando…" y el usuario no puede
        # re-intentar sin reiniciar el libro manualmente.
        libro.estado = "pendiente"
        session.commit()
        # Loggear el raw completo (no el truncado): sin esto, rechazos
        # con glosa vacía ("Rechazo SII libro X: ERROR") son imposibles
        # de diagnosticar. El raw queda en stdout del server, no en la BD.
        logger.warning(
            "Rechazo SII libro cert sin trackid: tipo=%s, status=%s, "
            "glosa=%r, raw=%s",
            libro.tipo_libro, status, glosa, raw_resp,
        )
        return {
            "ok": False,
            "trackid": None,
            "status": status,
            "glosa": glosa,
            "sha256": sha256,
            "raw": raw_resp[:2000],
        }

    # Éxito: el SII aceptó el envío
    libro.trackid = trackid
    libro.estado = "enviado"
    libro.estado_sii = "enviado"
    libro.enviado_at = datetime.now(timezone.utc)
    libro.error_mensaje = None
    # Registrar la PRIMERA vez que el SII aceptó (generó trackid) — el
    # timestamp sobrevive a ``reiniciar_envio_libro`` y sirve como
    # forensics/auditoría para reconstruir cuándo el libro entró al SII
    # por primera vez aunque luego se reintente. En certificación NO se
    # usa para cambiar a ``AJUSTE`` automáticamente (ver nota al inicio
    # de ``enviar_libro``: esa ruta fue revertida y requiere N° de
    # Atención nuevo del SII para corregir un envío ya recibido).
    if libro.primer_envio_sii_at is None:
        libro.primer_envio_sii_at = libro.enviado_at
    session.commit()

    logger.info(
        "Libro cert enviado OK: tipo=%s, trackid=%s",
        libro.tipo_libro, trackid,
    )
    return {
        "ok": True,
        "trackid": trackid,
        "status": status,
        "glosa": glosa,
        "sha256": sha256,
        "raw": (resp.get("raw") or "")[:2000],
    }


def consultar_estado_libro(
    session: Any,
    run: CertificacionRun,
    libro_id: str,
    servicio: ServicioEmisionDTE,
    empresa: Empresa,
) -> dict:
    """Consulta SII por trackid y actualiza ``estado_sii`` del libro.

    No cambia ``libro.estado`` — eso es Fase 5 (declarar avance).
    Solo actualiza ``estado_sii`` y ``error_mensaje``.

    Returns:
        Dict con ``trackid``, ``estado_sii``, ``glosa``, ``raw``.

    Raises:
        ValueError: si el libro no tiene trackid (no se envió aún).
    """
    libro = session.get(CertificacionLibro, libro_id)
    if not libro:
        raise ValueError(f"Libro {libro_id} no encontrado.")
    if libro.run_id != run.id:
        raise ValueError(f"Libro {libro_id} no pertenece a la run {run.id}.")
    if not libro.trackid:
        raise ValueError(
            f"Libro {libro.tipo_libro} no tiene trackid. "
            "Hay que enviarlo primero."
        )

    token = servicio._obtener_token()
    resp = consultar_estado_envio(
        track_id=libro.trackid,
        token=token,
        rut_emisor=servicio.config.rut,
        ambiente=servicio.config.ambiente,
    )
    raw_xml = resp.get("raw", "") or ""

    estado_sii = _parsear_estado_sii(raw_xml)
    glosa = _parsear_glosa_sii(raw_xml)

    if estado_sii:
        libro.estado_sii = estado_sii
    if estado_sii in ESTADOS_RECHAZO and glosa:
        libro.error_mensaje = glosa[:500]
    elif estado_sii and estado_sii not in ESTADOS_RECHAZO:
        libro.error_mensaje = None
    session.commit()

    logger.info(
        "Consulta estado libro cert: tipo=%s, trackid=%s, estado=%s",
        libro.tipo_libro, libro.trackid, estado_sii,
    )
    return {
        "trackid": libro.trackid,
        "estado_sii": estado_sii,
        "glosa": glosa,
        "raw": raw_xml[:4000],
    }


# ══════════════════════════════════════════════════════════════════
# Fase 5 — Declarar avance y marcar aprobación (libros)
# ══════════════════════════════════════════════════════════════════


def declarar_avance_libro(
    session: Any,
    run: CertificacionRun,
    libro_id: str,
) -> dict:
    """Registra que el usuario declaró avance del libro en el SII.

    La declaración se hace manualmente en la web del SII. Este método
    solo registra la fecha para trackear el progreso en el wizard.

    Raises:
        ValueError: si el libro no tiene trackid.
    """
    libro = session.get(CertificacionLibro, libro_id)
    if not libro:
        raise ValueError(f"Libro {libro_id} no encontrado.")
    if libro.run_id != run.id:
        raise ValueError(f"Libro {libro_id} no pertenece a la run {run.id}.")
    if not libro.trackid:
        raise ValueError(
            f"Libro {libro.tipo_libro} no tiene trackid. "
            "Hay que enviarlo primero."
        )

    now = datetime.now(timezone.utc)
    libro.avance_declarado_at = now
    session.commit()

    logger.info(
        "Avance declarado libro: tipo=%s, libro_id=%s",
        libro.tipo_libro, libro_id,
    )
    return {
        "libro_id": libro_id,
        "tipo_libro": libro.tipo_libro,
        "avance_declarado_at": now.isoformat(),
    }


def marcar_aprobado_libro(
    session: Any,
    run: CertificacionRun,
    libro_id: str,
) -> dict:
    """Marca el libro como aprobado por el SII.

    Precondición: ``avance_declarado_at`` debe estar seteado.

    Raises:
        ValueError: si no se declaró avance.
    """
    libro = session.get(CertificacionLibro, libro_id)
    if not libro:
        raise ValueError(f"Libro {libro_id} no encontrado.")
    if libro.run_id != run.id:
        raise ValueError(f"Libro {libro_id} no pertenece a la run {run.id}.")
    if not libro.avance_declarado_at:
        raise ValueError(
            f"Libro {libro.tipo_libro} no tiene avance declarado. "
            "Declara el avance en la web del SII primero."
        )

    now = datetime.now(timezone.utc)
    libro.aprobado_at = now
    libro.estado = "aprobado"
    session.commit()

    logger.info(
        "Libro marcado aprobado: tipo=%s, libro_id=%s",
        libro.tipo_libro, libro_id,
    )
    return {
        "libro_id": libro_id,
        "tipo_libro": libro.tipo_libro,
        "aprobado_at": now.isoformat(),
    }


def reiniciar_envio_libro(
    session: Any, run: CertificacionRun, libro_id: str,
) -> dict:
    """Resetea el estado de envío del libro para permitir re-emitirlo.

    Análogo al ``descartar-folio`` de los DTEs, pero aplicado a libros.
    Útil cuando el libro ya fue enviado pero necesita regenerarse (p.ej.
    el usuario olvidó el N° Atención y el libro salió como MENSUAL, o el
    SII devolvió reparos de cálculo que requieren un fix al core).

    Limpia trackid, estado_sii, error_mensaje, estado='pendiente' y
    enviado_at=None. **Preserva** ``xml_libro``, ``datos`` y
    ``numero_atencion``: el siguiente ``generar_libro`` sobreescribe el
    XML idempotentemente, y si ya se había completado el N° Atención, no
    tiene sentido pedirlo de nuevo.

    Bloquea la operación si el libro tiene ``avance_declarado_at`` o
    ``aprobado_at`` seteados — rebobinar eso sería deshacer registros
    inmutables de progreso ante el SII.

    Args:
        session: sesión SQLAlchemy sobre la BD de certificación.
        run: run activa. Debe ser la dueña del libro; de lo contrario se
            levanta ``ValueError`` (defensa cross-run).
        libro_id: ID del ``CertificacionLibro`` a reiniciar.

    Returns:
        Dict con ``ok=True``, ``libro_id``, ``estado='pendiente'``.

    Raises:
        ValueError: libro no encontrado, no pertenece a la run, o tiene
            avance declarado / aprobación registrada.
    """
    libro = session.query(CertificacionLibro).filter(
        CertificacionLibro.id == libro_id,
    ).first()
    if libro is None:
        raise ValueError(f"Libro {libro_id} no encontrado")
    if libro.run_id != run.id:
        raise ValueError(
            f"Libro {libro_id} no pertenece a la run {run.id}.",
        )

    if libro.avance_declarado_at is not None:
        raise ValueError(
            f"No se puede reiniciar el libro {libro.tipo_libro}: ya tiene "
            f"avance declarado al SII ({libro.avance_declarado_at}). "
            "Reiniciar rebobinaría un registro inmutable de progreso.",
        )
    if libro.aprobado_at is not None:
        raise ValueError(
            f"No se puede reiniciar el libro {libro.tipo_libro}: ya está "
            f"aprobado por el SII ({libro.aprobado_at}).",
        )
    # ADVERTENCIA (no bloqueo): reiniciar un libro LOK para enviar como
    # AJUSTE puede ser legítimo (corrección de datos).  Pero si el único
    # problema es un reparo LBR-2, el AJUSTE volverá a fallar y bloqueará
    # el avance.  El caller recibe la advertencia vía el campo
    # ``advertencia`` del dict de retorno para que el wizard la muestre.
    _advertencia_lok = (
        libro.estado_sii == "LOK"
        and libro.primer_envio_sii_at is not None
    )

    trackid_previo = libro.trackid
    libro.trackid = None
    libro.estado_sii = None
    libro.error_mensaje = None
    libro.estado = "pendiente"
    libro.enviado_at = None
    session.commit()

    if _advertencia_lok:
        logger.warning(
            "Libro LOK reiniciado — el siguiente envío será AJUSTE "
            "(primer_envio_sii_at conservado). Si el problema era solo "
            "un reparo LBR-2, el AJUSTE volverá a fallar. "
            "Libro=%s, trackid_previo=%s",
            libro.tipo_libro, trackid_previo,
        )

    logger.info(
        "Libro reiniciado: tipo=%s, libro_id=%s, trackid_previo=%s",
        libro.tipo_libro, libro_id, trackid_previo,
    )

    advertencia = (
        "El libro estaba LOK — el siguiente envío será AJUSTE. "
        "Si el único problema era un reparo LBR-2, el AJUSTE volverá "
        "a fallar con LRH. Declare avance directamente en ese caso."
        if _advertencia_lok else None
    )
    return {
        "ok": True,
        "libro_id": libro_id,
        "estado": libro.estado,
        "advertencia": advertencia,
    }
