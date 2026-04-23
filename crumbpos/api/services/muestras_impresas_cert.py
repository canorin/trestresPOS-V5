"""Generación de muestras impresas (PDFs) para certificación SII.

Lee los DTEs emitidos durante la certificación, genera PDFs en formato
carta (tributario + cedible donde aplica) y devuelve un ZIP organizado
por set, listo para subir al portal del SII.

El pipeline es:

1. Consulta ``CertificacionCaso`` de la run que tengan ``dte_emitido_id``.
2. Carga ``DteEmitido.xml_firmado`` (base64) de cada uno.
3. Parsea el XML firmado → extrae datos del DTE → ``DTEPrintData``.
4. Genera PDF tributario (1 página) con ``PDFCarta._render_page``.
5. Si el tipo es cedible (33, 34, 52), genera PDF cedible (1 página).
6. Empaqueta todo en un ZIP: ``{set}/{TXX_FYY_tributario.pdf}``.

Usa la clase de producción ``PDFCarta`` del módulo ``crumbpos.core.impresion``
para que las muestras sean idénticas a los PDFs que el software genera
en operación real. La única diferencia es que aquí se controla la
generación de 1 página por archivo (tributario o cedible), mientras que
``PDFCarta.generar()`` genera ambas páginas juntas.
"""
from __future__ import annotations

import base64
import io
import logging
import re
import zipfile
from typing import Any

from lxml import etree

from crumbpos.core.impresion.base import DTEPrintData, TIPOS_CEDIBLES
from crumbpos.core.impresion.formato_carta import PDFCarta
from crumbpos.db.models import (
    CertificacionCaso,
    CertificacionRun,
    DteEmitido,
    Empresa,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# Parseo XML firmado → DTEPrintData
# ══════════════════════════════════════════════════════════════════


def _find_text(parent, tag: str) -> str:
    """Busca texto de un tag dentro de un elemento XML (ignora namespace)."""
    for el in parent.iter():
        t = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if t == tag:
            return (el.text or "").strip()
    return ""


def _find_el(parent, tag: str):
    """Busca un elemento por tag (ignora namespace)."""
    for el in parent.iter():
        t = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if t == tag:
            return el
    return None


def _extraer_ted_raw(xml_bytes: bytes) -> str:
    """Extrae el bloque ``<TED>...</TED>`` como string del XML raw.

    Usa regex sobre los bytes crudos para preservar la codificación
    ISO-8859-1 exacta del TED (necesaria para el PDF417).
    """
    m = re.search(rb'<TED\b[^>]*>.*?</TED>', xml_bytes, re.DOTALL)
    if m:
        return m.group(0).decode("ISO-8859-1", errors="replace")
    return ""


def xml_to_print_data(
    xml_bytes: bytes,
    empresa: Empresa,
    ted_from_db: str | None = None,
) -> DTEPrintData:
    """Convierte XML firmado de un DTE a ``DTEPrintData`` para impresión.

    Args:
        xml_bytes: XML completo del EnvioDTE firmado (decodificado del
            base64 de ``DteEmitido.xml_firmado``).
        empresa: Empresa emisora — para resolución SII y datos faltantes.
        ted_from_db: TED XML como string (de ``DteEmitido.ted_xml``).
            Si no se provee, se extrae del XML raw.

    Returns:
        ``DTEPrintData`` listo para ``PDFCarta``.

    Raises:
        ValueError: si no encuentra ``<Documento>`` en el XML.
    """
    tree = etree.fromstring(xml_bytes)

    doc = _find_el(tree, "Documento")
    if doc is None:
        raise ValueError("No se encontró <Documento> en el XML firmado.")

    data = DTEPrintData()

    # ── Encabezado ──────────────────────────────────────────────
    enc = _find_el(doc, "Encabezado")
    if enc is not None:
        data.tipo_dte = int(_find_text(enc, "TipoDTE") or "0")
        data.folio = int(_find_text(enc, "Folio") or "0")
        data.fecha_emision = _find_text(enc, "FchEmis")

        id_doc = _find_el(enc, "IdDoc")
        if id_doc is not None:
            data.ind_traslado = _find_text(id_doc, "IndTraslado") or None
            data.tipo_despacho = _find_text(id_doc, "TipoDespacho") or None
            data.ind_servicio = _find_text(id_doc, "IndServicio") or None
            fma = _find_text(id_doc, "FmaPago")
            data.fma_pago = int(fma) if fma else None
            data.fecha_vencimiento = _find_text(id_doc, "FchVenc") or None

        emisor = _find_el(enc, "Emisor")
        if emisor is not None:
            data.emisor_rut = _find_text(emisor, "RUTEmisor")
            data.emisor_razon = (
                _find_text(emisor, "RznSoc")
                or _find_text(emisor, "RznSocEmisor")
            )
            data.emisor_giro = (
                _find_text(emisor, "GiroEmis")
                or _find_text(emisor, "GiroEmisor")
            )
            data.emisor_dir = _find_text(emisor, "DirOrigen")
            data.emisor_comuna = _find_text(emisor, "CmnaOrigen")
            data.emisor_ciudad = _find_text(emisor, "CiudadOrigen")
            data.emisor_acteco = _find_text(emisor, "Acteco")

        receptor = _find_el(enc, "Receptor")
        if receptor is not None:
            data.receptor_rut = _find_text(receptor, "RUTRecep")
            data.receptor_razon = _find_text(receptor, "RznSocRecep")
            data.receptor_giro = _find_text(receptor, "GiroRecep")
            data.receptor_dir = _find_text(receptor, "DirRecep")
            data.receptor_comuna = _find_text(receptor, "CmnaRecep")
            data.receptor_ciudad = _find_text(receptor, "CiudadRecep")

        totales = _find_el(enc, "Totales")
        if totales is not None:
            mn = _find_text(totales, "MntNeto")
            data.monto_neto = int(mn) if mn else None
            me = _find_text(totales, "MntExe")
            data.monto_exento = int(me) if me else None
            ti = _find_text(totales, "TasaIVA")
            data.tasa_iva = int(float(ti)) if ti else None
            iv = _find_text(totales, "IVA")
            data.iva = int(iv) if iv else None
            mt = _find_text(totales, "MntTotal")
            data.monto_total = int(mt) if mt else 0

    # ── Items (Detalle) ─────────────────────────────────────────
    for el in doc.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "Detalle":
            item = {
                "nro": _find_text(el, "NroLinDet"),
                "nombre": _find_text(el, "NmbItem"),
                "qty": _find_text(el, "QtyItem"),
                "unidad": _find_text(el, "UnmdItem"),
                "precio": _find_text(el, "PrcItem"),
                "descuento_pct": _find_text(el, "DescuentoPct"),
                "descuento_monto": _find_text(el, "DescuentoMonto"),
                "monto": _find_text(el, "MontoItem"),
                "ind_exe": _find_text(el, "IndExe"),
            }
            data.items.append(item)

    # ── Referencias ─────────────────────────────────────────────
    for el in doc.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "Referencia":
            ref = {
                "nro": _find_text(el, "NroLinRef"),
                "tipo_doc": _find_text(el, "TpoDocRef"),
                "folio": _find_text(el, "FolioRef"),
                "fecha": _find_text(el, "FchRef"),
                "codigo": _find_text(el, "CodRef"),
                "razon": _find_text(el, "RazonRef"),
            }
            data.referencias.append(ref)

    # ── Descuentos / Recargos Globales ──────────────────────────
    for el in doc.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "DscRcgGlobal":
            desc = {
                "nro": _find_text(el, "NroLinDR"),
                "tipo": _find_text(el, "TpoMov"),
                "glosa": _find_text(el, "GlosaDR"),
                "tipo_valor": _find_text(el, "TpoValor"),
                "valor": _find_text(el, "ValorDR"),
            }
            data.descuentos_globales.append(desc)

    # ── TED (Timbre Electrónico Digital) ────────────────────────
    if ted_from_db:
        data.ted_xml = ted_from_db
    else:
        data.ted_xml = _extraer_ted_raw(xml_bytes)

    # ── Resolución SII (de la empresa) ──────────────────────────
    data.numero_resolucion = empresa.numero_resolucion or 0
    data.fecha_resolucion = empresa.fecha_resolucion or ""

    return data


# ══════════════════════════════════════════════════════════════════
# Generación de PDFs individuales (1 página por archivo)
# ══════════════════════════════════════════════════════════════════


def _generar_pdf_tributario(data: DTEPrintData) -> bytes:
    """Genera PDF de 1 página: ejemplar tributario (sin acuse ni cedible)."""
    pdf = PDFCarta(data)
    pdf._render_page(es_cedible=False)
    return bytes(pdf.output())


def _generar_pdf_cedible(data: DTEPrintData) -> bytes:
    """Genera PDF de 1 página: copia cedible con acuse de recibo."""
    pdf = PDFCarta(data)
    pdf._render_page(es_cedible=True)
    return bytes(pdf.output())


# ══════════════════════════════════════════════════════════════════
# Generación de ZIP con todas las muestras
# ══════════════════════════════════════════════════════════════════


def generar_muestras_zip(
    session: Any,
    run: CertificacionRun,
    empresa: Empresa,
) -> tuple[bytes, dict]:
    """Genera ZIP con muestras impresas de todos los DTEs de certificación.

    El ZIP se organiza por set:

    .. code-block:: text

        basico/
          T33_F1_tributario.pdf
          T33_F1_cedible.pdf
          T56_F5_tributario.pdf
          T61_F6_tributario.pdf
          ...
        guias/
          T52_F10_tributario.pdf
          T52_F10_cedible.pdf
          ...
        exenta/
          T34_F20_tributario.pdf
          T34_F20_cedible.pdf
          ...

    Args:
        session: DB session de la empresa (certificación).
        run: La run activa de certificación.
        empresa: Empresa emisora.

    Returns:
        Tupla ``(zip_bytes, resumen)`` donde resumen es un dict con
        conteos de PDFs generados, errores y tamaño total.

    Raises:
        ValueError: si no hay DTEs emitidos en la run.
    """
    # 1. Cargar todos los casos con DTE emitido
    casos = (
        session.query(CertificacionCaso)
        .filter(
            CertificacionCaso.run_id == run.id,
            CertificacionCaso.dte_emitido_id.isnot(None),
        )
        .order_by(
            CertificacionCaso.set_nombre,
            CertificacionCaso.tipo_dte,
            CertificacionCaso.folio,
        )
        .all()
    )

    if not casos:
        raise ValueError(
            "No hay DTEs emitidos en esta certificación. "
            "Emite los casos del set primero."
        )

    # 2. Cargar DteEmitido de cada caso
    dte_ids = [c.dte_emitido_id for c in casos]
    dtes = session.query(DteEmitido).filter(DteEmitido.id.in_(dte_ids)).all()
    dtes_by_id = {d.id: d for d in dtes}

    # 3. Generar PDFs y empaquetar en ZIP
    buf = io.BytesIO()
    stats = {"tributarios": 0, "cedibles": 0, "errores": 0, "sets": set()}

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for caso in casos:
            dte = dtes_by_id.get(caso.dte_emitido_id)
            if not dte or not dte.xml_firmado:
                logger.warning(
                    "Caso %s sin xml_firmado, saltando.",
                    caso.numero_caso,
                )
                stats["errores"] += 1
                continue

            # Decode base64 → XML bytes
            try:
                xml_bytes = base64.b64decode(dte.xml_firmado)
            except Exception:
                logger.warning(
                    "Error decodificando xml_firmado del caso %s",
                    caso.numero_caso,
                )
                stats["errores"] += 1
                continue

            # Parse XML → DTEPrintData
            try:
                print_data = xml_to_print_data(
                    xml_bytes,
                    empresa,
                    ted_from_db=dte.ted_xml,
                )
            except Exception as e:
                logger.warning(
                    "Error parseando XML del caso %s: %s",
                    caso.numero_caso, e,
                )
                stats["errores"] += 1
                continue

            # Carpeta por set
            set_dir = caso.set_nombre.lower().replace(" ", "_")
            stats["sets"].add(set_dir)
            tipo = print_data.tipo_dte
            folio = print_data.folio

            # PDF tributario
            try:
                pdf_trib = _generar_pdf_tributario(print_data)
                zf.writestr(
                    f"{set_dir}/T{tipo}_F{folio}_tributario.pdf",
                    pdf_trib,
                )
                stats["tributarios"] += 1
            except Exception as e:
                logger.warning(
                    "Error generando PDF tributario T%d F%d: %s",
                    tipo, folio, e,
                )
                stats["errores"] += 1
                continue

            # PDF cedible (solo tipos 33, 34, 52)
            if tipo in TIPOS_CEDIBLES:
                try:
                    pdf_ced = _generar_pdf_cedible(print_data)
                    zf.writestr(
                        f"{set_dir}/T{tipo}_F{folio}_cedible.pdf",
                        pdf_ced,
                    )
                    stats["cedibles"] += 1
                except Exception as e:
                    logger.warning(
                        "Error generando PDF cedible T%d F%d: %s",
                        tipo, folio, e,
                    )
                    stats["errores"] += 1

    zip_bytes = buf.getvalue()
    resumen = {
        "tributarios": stats["tributarios"],
        "cedibles": stats["cedibles"],
        "total_pdfs": stats["tributarios"] + stats["cedibles"],
        "errores": stats["errores"],
        "sets": sorted(stats["sets"]),
        "zip_bytes": len(zip_bytes),
    }

    logger.info(
        "Muestras ZIP: %d tributarios, %d cedibles, %d errores, %d bytes",
        resumen["tributarios"],
        resumen["cedibles"],
        resumen["errores"],
        resumen["zip_bytes"],
    )
    return zip_bytes, resumen
