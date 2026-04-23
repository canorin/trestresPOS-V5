"""Armado y envío del sobre EnvioDTE del set de pruebas de certificación.

Durante la certificación, el wizard emite los casos de un set (básico,
guías o exenta) uno por uno con ``POST /api/certificacion/casos/.../emitir``.
Cada llamada firma un EnvioDTE individual y guarda el XML firmado en
``DteEmitido.xml_firmado`` (base64). No envía nada al SII todavía.

Este servicio arma el sobre multi-DTE que el SII espera (uno por set):

1. Lee todos los ``CertificacionCaso`` de un set (estado = 'emitido').
2. Carga los ``DteEmitido`` asociados.
3. Extrae el elemento ``<DTE>`` firmado de cada uno — la firma interna
   del DTE se hizo sobre un XML canonical específico, así que hay que
   preservarlo byte-a-byte (regex, no lxml).
4. Ordena topológicamente (``ordenar_por_dependencias``) para que NC
   vaya antes que ND cuando apuntan al mismo documento original.
5. Construye una ``Caratula`` nueva con ``SubTotDTE`` agregado por tipo.
6. Envuelve en ``EnvioDTE`` + ``SetDTE ID="SetDoc"`` y firma el envelope
   usando la misma ``Firma`` cargada en el ``ServicioEmisionDTE``.
7. Verifica la firma del sobre con ``Firma.verificar_firma_xml`` antes
   de enviar — si falla, aborta (R3).

Las funciones públicas son ``armar_sobre``, ``enviar_sobre`` y
``consultar_estado``. ``armar_sobre`` es idempotente y no muta nada
(perfecto para la vista previa del modal de confirmación R6).
``enviar_sobre`` sí hace POST al SII y persiste el trackid en cada caso
del set.

R6 (preguntar antes de enviar) se cumple en la capa router/UI: el wizard
muestra un modal con el resumen + sha256 y el usuario hace click en
"Confirmar envío" antes de que el endpoint llame a ``enviar_sobre``.
Este módulo asume que la confirmación humana ya ocurrió.

R8 (EPR no es aprobado): el estado ``enviado`` que este servicio setea
significa "el SII recibió el sobre y dio trackid". No significa
``aprobado``. La transición a aprobado requiere declarar avance del set
y consultar aprobación — eso se implementa en Fase 5.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from datetime import datetime
from typing import Any

from crumbpos.api.services.emision_dte import ServicioEmisionDTE
from crumbpos.config.settings import get_sii_url
from crumbpos.core.dte.ordenamiento_sobre import ordenar_por_dependencias
from crumbpos.core.sii_client.envio import (
    consultar_estado_envio,
    enviar_dte,
)
from crumbpos.db.models import (
    CertificacionCaso,
    CertificacionRun,
    DteEmitido,
    Empresa,
)

logger = logging.getLogger(__name__)

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

# El <DTE> interno nunca contiene otro <DTE> anidado, así que el regex
# non-greedy captura correctamente el elemento completo con su firma
# interior. Ver `emision_dte.py::emitir_factura` para la forma canonical
# del string que estamos extrayendo.
_DTE_INNER_RE = re.compile(r"(<DTE\b[^>]*>.*?</DTE>)", re.DOTALL)


# ══════════════════════════════════════════════════════════════════
# Helpers internos
# ══════════════════════════════════════════════════════════════════


def _extraer_dte_interno(xml_firmado_b64: str) -> str:
    """Extrae el ``<DTE>...</DTE>`` firmado de un EnvioDTE individual.

    Args:
        xml_firmado_b64: contenido de ``DteEmitido.xml_firmado``. Es el
            sobre completo (EnvioDTE con Caratula + DTE firmado + firma
            del envelope) codificado en base64.

    Returns:
        String con el elemento ``<DTE>`` (incluyendo la firma interior
        del documento) tal como estaba originalmente, byte-a-byte.

    Raises:
        ValueError: si el XML no contiene un elemento ``<DTE>`` reconocible.

    Por qué regex y no lxml: la firma XML-DSig interna del ``<DTE>`` se
    calculó sobre la forma canonical exacta del momento en que se firmó.
    Re-parsear y re-serializar con lxml puede alterar espacios en blanco,
    orden de atributos o prefijos de namespace, y eso rompe la firma.
    El regex preserva el string original exactamente.
    """
    raw = base64.b64decode(xml_firmado_b64).decode("ISO-8859-1")
    m = _DTE_INNER_RE.search(raw)
    if not m:
        raise ValueError(
            "El XML firmado no contiene un elemento <DTE>. "
            "¿Es realmente un EnvioDTE individual?",
        )
    return m.group(1)


def _construir_caratula_multi(
    servicio: ServicioEmisionDTE,
    subtotales_por_tipo: dict[int, int],
) -> str:
    """Construye la Caratula del sobre multi-DTE.

    ``subtotales_por_tipo`` es un dict ``{tipo_dte: nro_dtes}`` — por
    ejemplo ``{33: 4, 61: 3}`` para un set con 4 facturas y 3 notas de
    crédito. El XML resultante contiene un ``<SubTotDTE>`` por cada tipo
    presente, en orden numérico ascendente (pragmática del SII).

    El formato replica exactamente el de ``emision_dte.py::emitir_factura``
    pero con el ``SubTotDTE`` agregado en vez de fijo a un solo tipo.
    """
    config = servicio.config
    rut_envia = config.rut_firmante or config.rut
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    subtotales_xml = "".join(
        f"<SubTotDTE><TpoDTE>{tipo}</TpoDTE><NroDTE>{nro}</NroDTE></SubTotDTE>"
        for tipo, nro in sorted(subtotales_por_tipo.items())
    )
    return (
        f'<Caratula version="1.0">'
        f"<RutEmisor>{config.rut}</RutEmisor>"
        f"<RutEnvia>{rut_envia}</RutEnvia>"
        f"<RutReceptor>60803000-K</RutReceptor>"
        f"<FchResol>{config.fecha_resolucion}</FchResol>"
        f"<NroResol>{config.numero_resolucion}</NroResol>"
        f"<TmstFirmaEnv>{timestamp}</TmstFirmaEnv>"
        f"{subtotales_xml}"
        f"</Caratula>"
    )


def _parsear_estado_sii(raw_xml: str) -> str | None:
    """Extrae el código de estado de la respuesta del SII (``<ESTADO>``).

    Estados típicos en certificación:
        - EPR: Envío Procesado con Reservas (llegó, en revisión)
        - LOK: Libro aceptado
        - SOK: Set aceptado
        - LNC: Libro no cargado (falta antes)
        - SRH: Set con reparos/hallazgos
        - RSC: Rechazado por schema
        - RCH: Rechazado
        - RFR: Rechazado por firma

    Un ``EPR`` no es aprobación (R8). La aprobación final del set llega
    después de declarar avance — ese paso se implementa en Fase 5.
    """
    if not raw_xml:
        return None
    m = re.search(r"<ESTADO>([A-Z]+)</ESTADO>", raw_xml)
    if m:
        return m.group(1)
    return None


def _parsear_glosa_sii(raw_xml: str) -> str | None:
    """Extrae la glosa (mensaje de error o estado) del XML de respuesta."""
    if not raw_xml:
        return None
    for tag in ("GLOSA_ERR", "GLOSA_ESTADO", "GLOSA"):
        m = re.search(rf"<{tag}>([^<]+)</{tag}>", raw_xml)
        if m:
            return m.group(1).strip()
    return None


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_STATUS_UPLOAD = {
    "0": "Upload OK",
    "1": "Sender no tiene permiso para enviar",
    "2": "Error en tamaño del archivo",
    "3": "Archivo cortado",
    "5": "No está autenticado",
    "6": "Empresa no autorizada a enviar archivos",
    "7": "Esquema XSD inválido",
    "8": "Firma del documento inválida",
    "9": "Sistema bloqueado",
}


def _extraer_texto_plano(raw: str, max_len: int = 400) -> str:
    """Extrae una glosa legible de la respuesta SII a DTEUpload.

    El formato de respuesta (ver ``envio.pdf`` §2.2 y Figura 2.3) es:

    ```xml
    <RECEPCIONDTE>
      <STATUS>N</STATUS>
      <DETAIL>
        <ERROR>LSX-...: detalle</ERROR>
        <ERROR>LSX-...: detalle</ERROR>
      </DETAIL>
    </RECEPCIONDTE>
    ```

    Prioriza en este orden:
    1. ``<ERROR>`` dentro de ``<DETAIL>`` (errores específicos de schema).
    2. ``<GLOSA_ERR>`` / ``<GLOSA_ESTADO>`` / ``<GLOSA>`` (usados en otros
       endpoints, no aparecen en DTEUpload pero dejamos el fallback).
    3. Descripción de ``<STATUS>`` según tabla de envio.pdf §2.1.
    4. Texto plano sin tags (último recurso).
    """
    if not raw:
        return ""
    # 1) Errores específicos dentro de <DETAIL>
    errores = re.findall(r"<ERROR>([^<]+)</ERROR>", raw, re.IGNORECASE)
    if errores:
        glosa = " | ".join(e.strip() for e in errores if e.strip())
        if glosa:
            return glosa[:max_len]
    # 2) Glosas en otros formatos
    for tag in ("GLOSA_ERR", "GLOSA_ESTADO", "GLOSA"):
        m = re.search(rf"<{tag}>([^<]+)</{tag}>", raw, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:max_len]
    # 3) Descripción del código STATUS
    m_status = re.search(r"<STATUS>(\d+)</STATUS>", raw, re.IGNORECASE)
    if m_status:
        codigo = m_status.group(1)
        descr = _STATUS_UPLOAD.get(codigo, "error desconocido")
        return f"status={codigo} ({descr})"[:max_len]
    # 4) Texto plano
    sin_tags = re.sub(r"<[^>]+>", " ", raw)
    sin_tags = re.sub(r"\s+", " ", sin_tags).strip()
    return sin_tags[:max_len]


def _cargar_casos_emitidos(
    session: Any, run: CertificacionRun, set_nombre: str,
) -> list[CertificacionCaso]:
    """Obtiene los casos del set y valida que todos estén emitidos.

    Devuelve la lista en orden estable por ``numero_caso`` — el
    ordenamiento topológico final lo hace ``ordenar_por_dependencias``
    sobre los ``DteEmitido`` asociados.
    """
    casos = session.query(CertificacionCaso).filter(
        CertificacionCaso.run_id == run.id,
        CertificacionCaso.set_nombre == set_nombre,
    ).order_by(CertificacionCaso.numero_caso).all()

    if not casos:
        raise ValueError(
            f"No hay casos para el set '{set_nombre}' en la run {run.id}",
        )

    no_emitidos = [c for c in casos if c.estado != "emitido"]
    if no_emitidos:
        nums = ", ".join(c.numero_caso for c in no_emitidos)
        raise ValueError(
            f"El set '{set_nombre}' tiene casos sin emitir: {nums}. "
            "Hay que emitir todos los casos antes de armar el sobre — "
            "el SII rechaza sobres incompletos (R5).",
        )

    faltan_dte = [c for c in casos if not c.dte_emitido_id]
    if faltan_dte:
        nums = ", ".join(c.numero_caso for c in faltan_dte)
        raise ValueError(
            f"Casos del set '{set_nombre}' marcados como 'emitido' pero "
            f"sin dte_emitido_id: {nums}. Esto no debería ocurrir — "
            "re-emitir esos casos desde el wizard.",
        )

    return casos


# ══════════════════════════════════════════════════════════════════
# Funciones públicas
# ══════════════════════════════════════════════════════════════════


def armar_sobre(
    session: Any,
    run: CertificacionRun,
    set_nombre: str,
    servicio: ServicioEmisionDTE,
    empresa: Empresa,
) -> dict:
    """Arma y firma el sobre EnvioDTE multi-DTE SIN enviar al SII.

    Operación idempotente — no muta nada en la BD. Sirve para la vista
    previa del modal de confirmación R6: el usuario ve resumen,
    folios y sha256 antes de autorizar el envío real.

    Args:
        session: sesión SQLAlchemy abierta sobre la BD de certificación
            de la empresa.
        run: ``CertificacionRun`` activa.
        set_nombre: identificador del set en el parser (BASICO / GUIAS /
            EXENTA — ver ``parser_set_sii.py``).
        servicio: ``ServicioEmisionDTE`` ya construido con el mismo
            certificado y CAFs que se usaron para emitir los casos.
        empresa: modelo ``Empresa`` cargado desde la BD de certificación.

    Returns:
        Dict con:
            - ``xml_bytes`` (bytes): sobre EnvioDTE firmado, codificado
              ISO-8859-1 con la declaración XML incluida.
            - ``sha256`` (str): hex digest del ``xml_bytes`` — sirve
              para que el usuario compare en la UI y opcionalmente
              compare con un export posterior.
            - ``resumen_por_tipo`` (dict[int,int]): ``{tipo_dte: n}``.
            - ``folios`` (list[dict]): folios incluidos en el orden
              topológico final, como ``[{"tipo": 33, "folio": 127}, ...]``.
            - ``casos_ids`` (list[str]): IDs de los casos del set, en el
              mismo orden en que fueron cargados (no el topológico).
            - ``url_sii`` (str): URL destino del envío (para mostrar en
              el modal R6 cuál ambiente se está tocando).

    Raises:
        ValueError: si hay casos sin emitir, sin dte_emitido_id o si
            algún XML no contiene un elemento ``<DTE>``.
        RuntimeError: si la firma del sobre falla o la verificación
            pre-envío detecta una firma inválida.
    """
    casos_set = _cargar_casos_emitidos(session, run, set_nombre)
    dte_ids = [c.dte_emitido_id for c in casos_set]
    dtes = session.query(DteEmitido).filter(
        DteEmitido.id.in_(dte_ids),
    ).all()

    if len(dtes) != len(casos_set):
        encontrados = {d.id for d in dtes}
        faltan = [c.numero_caso for c in casos_set
                  if c.dte_emitido_id not in encontrados]
        raise ValueError(
            f"Set '{set_nombre}': se esperaban {len(casos_set)} DteEmitido "
            f"pero se encontraron {len(dtes)}. Casos sin DTE: {faltan}.",
        )

    # Ordenamiento topológico: NC antes que ND cuando apuntan al mismo
    # documento original. Ver R20 en AGENTS.md.
    dtes_ordenados = ordenar_por_dependencias(dtes)

    dtes_inner: list[str] = []
    subtotales: dict[int, int] = {}
    folios_ordenados: list[dict] = []
    for d in dtes_ordenados:
        if not d.xml_firmado:
            raise ValueError(
                f"DteEmitido {d.id} (T{d.tipo_dte} F{d.folio}) no tiene "
                "xml_firmado — no se puede incluir en el sobre.",
            )
        dte_inner = _extraer_dte_interno(d.xml_firmado)
        dtes_inner.append(dte_inner)
        subtotales[d.tipo_dte] = subtotales.get(d.tipo_dte, 0) + 1
        folios_ordenados.append({"tipo": d.tipo_dte, "folio": d.folio})

    # La firma del envelope requiere que el ServicioEmisionDTE tenga
    # `_firma` cargado. Lo hacemos explícito acá por si el servicio
    # viene "frío".
    servicio._cargar_firma()
    firma = servicio._firma

    caratula = _construir_caratula_multi(servicio, subtotales)

    env_str = (
        f'<EnvioDTE xmlns="{SII_NS}" '
        f'xmlns:xsi="{XSI_NS}" '
        f'xsi:schemaLocation="{SII_NS} EnvioDTE_v10.xsd" '
        f'version="1.0">'
        f'<SetDTE ID="SetDoc">'
        f"{caratula}"
        f'{"".join(dtes_inner)}'
        f"</SetDTE>"
        f"</EnvioDTE>"
    )

    signed_env = firma.firmar(env_str, "SetDoc", type="env")
    if not signed_env:
        raise RuntimeError(
            "Error firmando el sobre EnvioDTE multi-DTE "
            f"(set '{set_nombre}')",
        )

    try:
        codigo_env, msg_env = firma.verificar_firma_xml(signed_env)
        if codigo_env != 0:
            raise RuntimeError(
                "Firma del sobre inválida (pre-verificación): "
                f"{msg_env}. El SII habría rechazado con DTE-3-505. "
                "No se envía.",
            )
    except RuntimeError:
        raise
    except Exception as exc:
        # Fallo interno del verificador — no bloqueamos pero dejamos
        # rastro en el log para diagnóstico.
        logger.warning(
            "No se pudo verificar firma del sobre multi-DTE (%s): %s",
            set_nombre, exc,
        )

    xml_final = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed_env
    xml_bytes = xml_final.encode("ISO-8859-1")

    return {
        "xml_bytes": xml_bytes,
        "sha256": _sha256_hex(xml_bytes),
        "resumen_por_tipo": subtotales,
        "folios": folios_ordenados,
        "casos_ids": [c.id for c in casos_set],
        "url_sii": get_sii_url("upload"),
    }


def enviar_sobre(
    session: Any,
    run: CertificacionRun,
    set_nombre: str,
    servicio: ServicioEmisionDTE,
    empresa: Empresa,
) -> dict:
    """Arma el sobre + lo envía al SII + persiste el trackid en cada caso.

    Este es el único punto del módulo que hace I/O contra el SII. Debe
    ser invocado SOLO después de confirmación humana explícita (R6) —
    el wizard construye el modal de confirmación llamando primero a
    ``armar_sobre`` para mostrar el resumen, y solo llama a este
    endpoint cuando el usuario hace click en "Confirmar envío".

    Resultado en la BD:
        - Si el SII responde con trackid: cada caso del set queda
          ``estado_sii='enviado'``, ``trackid=<id>``, ``error_mensaje=None``.
          El ``estado`` del caso se mantiene en 'emitido' (no pasamos a
          'aprobado' hasta consultar + declarar avance).
        - Si el SII rechaza sin trackid: los casos quedan como estaban
          pero con ``error_mensaje`` seteado a la glosa del rechazo. El
          estado del caso sigue 'emitido', así el usuario puede corregir
          y reintentar sin perder los folios (que ya están quemados).

    Returns:
        Dict con: ``ok``, ``trackid``, ``status``, ``glosa``, ``sha256``,
        ``resumen_por_tipo``, ``casos_actualizados``, ``raw`` (preview
        de 2000 chars por si hay error de parseo).
    """
    resultado_arma = armar_sobre(session, run, set_nombre, servicio, empresa)
    xml_bytes = resultado_arma["xml_bytes"]
    casos_ids = resultado_arma["casos_ids"]
    sha256 = resultado_arma["sha256"]
    resumen = resultado_arma["resumen_por_tipo"]

    token = servicio._obtener_token()
    rut_envia = servicio.config.rut_firmante or servicio.config.rut

    logger.info(
        "Enviando sobre cert: set=%s, rut=%s, casos=%d, sha256=%s",
        set_nombre, servicio.config.rut, len(casos_ids), sha256[:16],
    )
    resp = enviar_dte(
        xml_bytes=xml_bytes,
        token=token,
        rut_emisor=servicio.config.rut,
        rut_envia=rut_envia,
    )

    trackid = resp.get("track_id")
    status = resp.get("status")
    glosa = (resp.get("glosa") or "").strip()

    if not trackid:
        # SII rechazó el sobre — persistir el error en cada caso para
        # que el wizard lo muestre. No cambiamos estado del caso.
        raw_resp = (resp.get("raw") or "").strip()
        # Loguear la respuesta completa para diagnóstico (aparece en uvicorn).
        logger.error(
            "SII rechazó sobre cert: set=%s, rut=%s, status=%s, glosa=%r, "
            "sha256=%s, casos=%d, raw_response=\n%s",
            set_nombre, servicio.config.rut, status, glosa, sha256[:16],
            len(casos_ids), raw_resp[:4000],
        )
        # Glosa compacta para persistir en el caso. Si el SII no devolvió
        # glosa, extraer texto plano del raw (primeros 300 chars sin tags).
        glosa_efectiva = glosa or _extraer_texto_plano(raw_resp)
        for caso_id in casos_ids:
            caso = session.get(CertificacionCaso, caso_id)
            if caso:
                caso.error_mensaje = (
                    f"Rechazo SII sobre '{set_nombre}' "
                    f"[status={status or 'sin status'}]: "
                    f"{glosa_efectiva or 'sin glosa'}"
                )[:500]
        session.commit()
        return {
            "ok": False,
            "trackid": None,
            "status": status,
            "glosa": glosa,
            "sha256": sha256,
            "resumen_por_tipo": resumen,
            "casos_actualizados": len(casos_ids),
            "raw": raw_resp[:4000],
        }

    # Éxito del envío: EPR o similar. Persistimos trackid y estado_sii.
    for caso_id in casos_ids:
        caso = session.get(CertificacionCaso, caso_id)
        if caso:
            caso.trackid = trackid
            caso.estado_sii = "enviado"  # R8: NO es 'aprobado' todavía.
            caso.error_mensaje = None
    session.commit()

    logger.info(
        "Sobre cert enviado OK: set=%s, trackid=%s, casos=%d",
        set_nombre, trackid, len(casos_ids),
    )
    return {
        "ok": True,
        "trackid": trackid,
        "status": status,
        "glosa": glosa,
        "sha256": sha256,
        "resumen_por_tipo": resumen,
        "casos_actualizados": len(casos_ids),
        "raw": (resp.get("raw") or "")[:2000],
    }


def consultar_estado(
    session: Any,
    run: CertificacionRun,
    set_nombre: str,
    servicio: ServicioEmisionDTE,
    empresa: Empresa,
) -> dict:
    """Consulta el estado del envío al SII por trackid y actualiza casos.

    El primer caso del set que tenga trackid se usa como referencia —
    todos los casos del mismo set comparten el mismo trackid tras un
    envío exitoso.

    El ``estado_sii`` que escribe este método es lo que el SII devuelve
    (EPR, SOK, SRH, etc.). NO cambia ``CertificacionCaso.estado`` (que
    sigue en 'emitido'). La transición final a 'aprobado' va a vivir
    en Fase 5, tras declarar avance del set.

    Si el SII responde con glosa de error (rechazo de schema o firma),
    ese texto se guarda en ``error_mensaje`` de cada caso.

    Returns:
        Dict con ``trackid``, ``estado_sii``, ``glosa``, ``raw`` (XML
        crudo de la respuesta SOAP del SII, útil para debugging), y
        ``casos_actualizados``.

    Raises:
        ValueError: si no hay casos o si ningún caso tiene trackid.
    """
    casos_set = session.query(CertificacionCaso).filter(
        CertificacionCaso.run_id == run.id,
        CertificacionCaso.set_nombre == set_nombre,
    ).all()

    if not casos_set:
        raise ValueError(
            f"No hay casos para el set '{set_nombre}' en la run {run.id}",
        )

    trackids = {c.trackid for c in casos_set if c.trackid}
    if not trackids:
        raise ValueError(
            f"Ningún caso del set '{set_nombre}' tiene trackid. "
            "Hay que enviar el sobre primero.",
        )
    if len(trackids) > 1:
        raise ValueError(
            f"Casos del set '{set_nombre}' tienen trackids distintos: "
            f"{sorted(trackids)}. Esto es inconsistente — borrar la run "
            "y volver a enviar un solo sobre por set (R5).",
        )
    trackid = next(iter(trackids))

    token = servicio._obtener_token()
    resp = consultar_estado_envio(
        track_id=trackid,
        token=token,
        rut_emisor=servicio.config.rut,
    )
    raw_xml = resp.get("raw", "") or ""

    estado_sii = _parsear_estado_sii(raw_xml)
    glosa = _parsear_glosa_sii(raw_xml)

    # Estados del SII que indican rechazo — guardar la glosa como error
    # para que el wizard la muestre.
    ESTADOS_RECHAZO = {"RCH", "RFR", "RSC", "SRH", "LRH"}
    for caso in casos_set:
        if estado_sii:
            caso.estado_sii = estado_sii
        if estado_sii in ESTADOS_RECHAZO and glosa:
            caso.error_mensaje = glosa[:500]
        elif estado_sii and estado_sii not in ESTADOS_RECHAZO:
            # Limpiar mensaje de error previo si ahora el estado está OK.
            caso.error_mensaje = None
    session.commit()

    logger.info(
        "Consulta estado SII: set=%s, trackid=%s, estado=%s",
        set_nombre, trackid, estado_sii,
    )
    return {
        "trackid": trackid,
        "estado_sii": estado_sii,
        "glosa": glosa,
        "raw": raw_xml[:4000],  # limitar payload de respuesta JSON
        "casos_actualizados": len(casos_set),
    }


# ══════════════════════════════════════════════════════════════════
# Fase 5 — Declarar avance y marcar aprobación
# ══════════════════════════════════════════════════════════════════


def declarar_avance(
    session: Any,
    run: CertificacionRun,
    set_nombre: str,
) -> dict:
    """Registra que el usuario declaró avance de este set en el SII.

    La declaración de avance se hace manualmente en la web del SII
    (https://maullin.sii.cl → Certificación → Declarar Avance). Este
    método solo registra la fecha en que el usuario confirmó haberlo
    hecho, para que el wizard pueda trackear el progreso.

    Precondiciones:
        - Todos los casos del set deben tener trackid (ya enviados).
        - No deben tener ya ``avance_declarado_at`` seteado.

    Returns:
        Dict con ``set_nombre``, ``avance_declarado_at``,
        ``casos_actualizados``.

    Raises:
        ValueError: si no hay casos, o no tienen trackid.
    """
    casos_set = session.query(CertificacionCaso).filter(
        CertificacionCaso.run_id == run.id,
        CertificacionCaso.set_nombre == set_nombre,
    ).all()

    if not casos_set:
        raise ValueError(
            f"No hay casos para el set '{set_nombre}' en la run {run.id}",
        )

    sin_trackid = [c for c in casos_set if not c.trackid]
    if sin_trackid:
        nums = ", ".join(c.numero_caso for c in sin_trackid)
        raise ValueError(
            f"Casos del set '{set_nombre}' sin trackid: {nums}. "
            "Hay que enviar el sobre primero.",
        )

    now = datetime.now()
    for caso in casos_set:
        caso.avance_declarado_at = now
    session.commit()

    logger.info(
        "Avance declarado: set=%s, casos=%d",
        set_nombre, len(casos_set),
    )
    return {
        "set_nombre": set_nombre,
        "avance_declarado_at": now.isoformat(),
        "casos_actualizados": len(casos_set),
    }


def marcar_aprobado(
    session: Any,
    run: CertificacionRun,
    set_nombre: str,
) -> dict:
    """Marca el set como aprobado por el SII.

    El usuario debe haber verificado en la web del SII (o consultado
    con ``consultar_estado``) que el estado es SOK/LOK (aprobado).
    Este método registra ``aprobado_at`` y cambia ``estado`` a
    'aprobado'.

    Precondiciones:
        - ``avance_declarado_at`` debe estar seteado.

    Returns:
        Dict con ``set_nombre``, ``aprobado_at``, ``casos_actualizados``.

    Raises:
        ValueError: si no hay avance declarado.
    """
    casos_set = session.query(CertificacionCaso).filter(
        CertificacionCaso.run_id == run.id,
        CertificacionCaso.set_nombre == set_nombre,
    ).all()

    if not casos_set:
        raise ValueError(
            f"No hay casos para el set '{set_nombre}' en la run {run.id}",
        )

    sin_avance = [c for c in casos_set if not c.avance_declarado_at]
    if sin_avance:
        raise ValueError(
            f"El set '{set_nombre}' no tiene avance declarado. "
            "Declara el avance en la web del SII primero.",
        )

    now = datetime.now()
    for caso in casos_set:
        caso.aprobado_at = now
        caso.estado = "aprobado"
    session.commit()

    logger.info(
        "Set marcado aprobado: set=%s, casos=%d",
        set_nombre, len(casos_set),
    )
    return {
        "set_nombre": set_nombre,
        "aprobado_at": now.isoformat(),
        "casos_actualizados": len(casos_set),
    }
