"""Polling de estado de DTEs y Libros en el SII.

Consulta automatizada del estado de envios pendientes,
actualizando los registros en la base de datos.

Boletas (T39/T41) usan la REST API del SII con token de boleta,
mientras DTEs tradicionales usan SOAP.
"""
import logging
import re
from datetime import datetime

from lxml import etree
from sqlalchemy.orm import Session

from crumbpos.db.models import DteEmitido, LibroGenerado, Empresa
from crumbpos.core.sii_client.envio import consultar_estado_envio, consultar_estado_boleta
from crumbpos.core.sii_client.consulta import consultar_estado_dte

logger = logging.getLogger(__name__)

# ── Mapeo de estados SII para DTEs / Envios (SOAP QueryEstUp) ──
# Tabla oficial: estado_envio.pdf (OI2004_CEUPDTE_MDE_1.10) seccion 3.3
# RSC / SOK / CRT / RFR / FOK / PDR / RCT / EPR
# CRT = "Caratula OK" (estado intermedio positivo, NO rechazo)
# PDR = "Envio en Proceso" (NO "PRD" — ese codigo no existe en el SII)
# Rechazos definitivos: RSC (Schema), RFR (Firma), RCT (Caratula)

ESTADO_DTE_MAP = {
    "EPR": "aceptado",      # Envio Procesado
    "RSC": "rechazado",     # Rechazado por Error en Schema
    "SOK": "pendiente",     # Schema Validado (intermedio)
    "CRT": "pendiente",     # Caratula OK (intermedio positivo)
    "RFR": "rechazado",     # Rechazado por Error en Firma
    "FOK": "pendiente",     # Firma de Envio Validada (intermedio)
    "PDR": "pendiente",     # Envio en Proceso
    "RCT": "rechazado",     # Rechazado por Error en Caratula
    "RPR": "reparo",        # Aceptado con Reparos (compatibilidad, no en tabla oficial)
    "DNK": "pendiente",     # Aun no verificado (pertenece a QueryEstDte)
    "-11": "pendiente",     # Reintentando
}

# ── Mapeo de estados SII para Boletas (REST API) ──
# Documentado en https://www4c.sii.cl/bolcoreinternetui/api/ openapi.yaml

ESTADO_BOLETA_MAP = {
    "EPR": "aceptado",      # Envío Procesado OK
    "RCH": "rechazado",     # Rechazado
    "RCO": "rechazado",     # Rechazado por consistencia
    "RPR": "reparo",        # Aceptado con Reparos
    "SOK": "pendiente",     # Schema OK, pendiente
    "REC": "pendiente",     # Recibido, en proceso
    "PRD": "pendiente",     # En proceso
}

# ── Mapeo de estados SII para Libros ──

ESTADO_LIBRO_MAP = {
    "LOK": "aceptado",      # Libro OK
    "LNC": "rechazado",     # Libro ya cerrado (no se puede reenviar)
    "LRH": "rechazado",     # Libro Rechazado
    "LRE": "rechazado",     # Libro Rechazado con errores
    "SOK": "pendiente",     # Schema OK, pendiente
    "EPR": "aceptado",      # Procesado
    "-11": "pendiente",     # Reintentando
}


def _parse_estado_envio(raw_xml: str) -> tuple[str | None, str | None]:
    """Parsea la respuesta XML de consultar_estado_envio.

    Extrae ESTADO y GLOSA del SII:RESP_HDR.

    Returns:
        (estado_sii, glosa) o (None, None) si no se puede parsear.
    """
    try:
        # Limpiar posibles declaraciones XML duplicadas
        clean = raw_xml.strip()
        if not clean:
            return None, None

        # Intentar parsear como XML
        root = etree.fromstring(clean.encode("utf-8") if isinstance(clean, str) else clean)

        # Buscar ESTADO en cualquier namespace
        estado = None
        glosa = None

        # Buscar con namespace SII
        sii_ns = "http://www.sii.cl/XMLSchema"
        estado_elem = root.find(f".//{{{sii_ns}}}ESTADO")
        if estado_elem is not None and estado_elem.text:
            estado = estado_elem.text.strip()

        glosa_elem = root.find(f".//{{{sii_ns}}}GLOSA")
        if glosa_elem is not None and glosa_elem.text:
            glosa = glosa_elem.text.strip()

        # Fallback sin namespace
        if estado is None:
            estado_elem = root.find(".//ESTADO")
            if estado_elem is not None and estado_elem.text:
                estado = estado_elem.text.strip()

        if glosa is None:
            glosa_elem = root.find(".//GLOSA")
            if glosa_elem is not None and glosa_elem.text:
                glosa = glosa_elem.text.strip()

        # Fallback regex
        if estado is None:
            match = re.search(r'<(?:\w+:)?ESTADO>([^<]+)</(?:\w+:)?ESTADO>', raw_xml)
            if match:
                estado = match.group(1).strip()

        if glosa is None:
            match = re.search(r'<(?:\w+:)?GLOSA>([^<]+)</(?:\w+:)?GLOSA>', raw_xml)
            if match:
                glosa = match.group(1).strip()

        return estado, glosa

    except Exception as e:
        logger.warning("Error parseando respuesta SII: %s — raw: %s", e, raw_xml[:200])
        # Ultimo fallback con regex sobre string crudo
        estado_match = re.search(r'<(?:\w+:)?ESTADO>([^<]+)</(?:\w+:)?ESTADO>', raw_xml)
        glosa_match = re.search(r'<(?:\w+:)?GLOSA>([^<]+)</(?:\w+:)?GLOSA>', raw_xml)
        return (
            estado_match.group(1).strip() if estado_match else None,
            glosa_match.group(1).strip() if glosa_match else None,
        )


def _parse_estado_boleta(resp_json: dict) -> tuple[str | None, str | None]:
    """Parsea la respuesta JSON de consultar_estado_boleta.

    La REST API devuelve JSON con:
    - estado: REC, EPR, RCH, RCO, RPR, SOK
    - estadistica: lista de documentos por tipo y estado
    - detalle_rep_rech: detalles de rechazos/reparos

    Returns:
        (estado_sii, glosa) o (None, None) si no se puede parsear.
    """
    try:
        estado = resp_json.get("estado")
        if not estado:
            return None, None

        # Construir glosa descriptiva
        glosa_parts = [estado]

        # Agregar estadísticas si existen
        estadistica = resp_json.get("estadistica")
        if estadistica:
            for stat in estadistica:
                tipo = stat.get("tipo", "?")
                informados = stat.get("informados", 0)
                aceptados = stat.get("aceptados", 0)
                rechazados = stat.get("rechazados", 0)
                reparos = stat.get("reparos", 0)
                glosa_parts.append(
                    f"T{tipo}: {informados} informados, "
                    f"{aceptados} aceptados, {rechazados} rechazados, "
                    f"{reparos} reparos"
                )

        # Agregar detalles de rechazo si existen
        detalle = resp_json.get("detalle_rep_rech")
        if detalle:
            for d in detalle[:5]:  # Máximo 5 detalles
                desc = d.get("descripcion", "")
                if desc:
                    glosa_parts.append(desc)

        glosa = " | ".join(glosa_parts)
        return estado, glosa

    except Exception as e:
        logger.warning("Error parseando respuesta boleta: %s", e)
        return None, None


def poll_dtes(
    db: Session,
    empresa: Empresa,
    token: str,
    token_boleta: str | None = None,
) -> dict:
    """Consulta el estado de todos los DTEs pendientes de una empresa.

    Soporta tanto DTEs tradicionales (SOAP) como boletas (REST API).
    Las boletas (T39/T41) requieren un token_boleta obtenido via
    la REST API de boletas del SII.

    Args:
        db: Sesion SQLAlchemy
        empresa: Empresa a consultar
        token: Token SII vigente (SOAP, para DTEs tradicionales)
        token_boleta: Token REST API para boletas (opcional)

    Returns:
        Resumen con conteo de actualizaciones por estado.
    """
    pendientes = (
        db.query(DteEmitido)
        .filter(
            DteEmitido.empresa_id == empresa.id,
            DteEmitido.estado_sii.in_(["pendiente", "enviado"]),
            DteEmitido.track_id.isnot(None),
            DteEmitido.track_id != "",
        )
        .all()
    )

    # Separar DTEs tradicionales de boletas
    dtes_regular = [d for d in pendientes if d.tipo_dte not in (39, 41)]
    dtes_boleta = [d for d in pendientes if d.tipo_dte in (39, 41)]

    resumen = {
        "total_consultados": 0,
        "aceptados": 0,
        "rechazados": 0,
        "reparos": 0,
        "sin_cambio": 0,
        "errores": 0,
        "detalle": [],
    }

    # ── DTEs tradicionales (SOAP) ──
    for dte in dtes_regular:
        resumen["total_consultados"] += 1
        try:
            resp = consultar_estado_envio(
                track_id=dte.track_id,
                token=token,
                rut_emisor=empresa.rut,
            )

            raw_xml = resp.get("raw", "")
            estado_sii, glosa = _parse_estado_envio(raw_xml)

            if estado_sii is None:
                resumen["errores"] += 1
                resumen["detalle"].append({
                    "tipo_dte": dte.tipo_dte,
                    "folio": dte.folio,
                    "track_id": dte.track_id,
                    "error": "No se pudo parsear respuesta SII",
                })
                continue

            nuevo_estado = ESTADO_DTE_MAP.get(estado_sii, "pendiente")
            estado_anterior = dte.estado_sii

            dte.estado_sii = nuevo_estado
            dte.glosa_sii = glosa or estado_sii
            dte.fecha_consulta_sii = datetime.utcnow()

            if nuevo_estado == estado_anterior:
                resumen["sin_cambio"] += 1
            elif nuevo_estado == "aceptado":
                resumen["aceptados"] += 1
            elif nuevo_estado == "rechazado":
                resumen["rechazados"] += 1
            elif nuevo_estado == "reparo":
                resumen["reparos"] += 1
            else:
                resumen["sin_cambio"] += 1

            resumen["detalle"].append({
                "tipo_dte": dte.tipo_dte,
                "folio": dte.folio,
                "track_id": dte.track_id,
                "estado_anterior": estado_anterior,
                "estado_nuevo": nuevo_estado,
                "estado_sii_raw": estado_sii,
                "glosa": glosa,
            })

            logger.info(
                "DTE T%d F%d track=%s: %s -> %s (%s)",
                dte.tipo_dte, dte.folio, dte.track_id,
                estado_anterior, nuevo_estado, estado_sii,
            )

        except Exception as e:
            resumen["errores"] += 1
            resumen["detalle"].append({
                "tipo_dte": dte.tipo_dte,
                "folio": dte.folio,
                "track_id": dte.track_id,
                "error": str(e),
            })
            logger.error(
                "Error consultando DTE T%d F%d track=%s: %s",
                dte.tipo_dte, dte.folio, dte.track_id, e,
                exc_info=True,
            )

    # ── Boletas (REST API) ──
    if dtes_boleta and not token_boleta:
        logger.warning(
            "%d boletas pendientes pero sin token_boleta — omitiendo consulta REST",
            len(dtes_boleta),
        )
        for dte in dtes_boleta:
            resumen["total_consultados"] += 1
            resumen["errores"] += 1
            resumen["detalle"].append({
                "tipo_dte": dte.tipo_dte,
                "folio": dte.folio,
                "track_id": dte.track_id,
                "error": "Sin token_boleta para consulta REST API",
            })

    elif dtes_boleta and token_boleta:
        # Agrupar por track_id para no consultar el mismo track múltiples veces
        tracks_consultados = {}
        for dte in dtes_boleta:
            resumen["total_consultados"] += 1
            try:
                track = dte.track_id
                if track not in tracks_consultados:
                    resp_json = consultar_estado_boleta(
                        track_id=track,
                        token=token_boleta,
                        rut_emisor=empresa.rut,
                    )
                    tracks_consultados[track] = resp_json

                resp_json = tracks_consultados[track]
                estado_sii, glosa = _parse_estado_boleta(resp_json)

                if estado_sii is None:
                    resumen["errores"] += 1
                    resumen["detalle"].append({
                        "tipo_dte": dte.tipo_dte,
                        "folio": dte.folio,
                        "track_id": track,
                        "error": f"No se pudo parsear respuesta boleta: {resp_json}",
                    })
                    continue

                nuevo_estado = ESTADO_BOLETA_MAP.get(estado_sii, "pendiente")
                estado_anterior = dte.estado_sii

                dte.estado_sii = nuevo_estado
                dte.glosa_sii = glosa or estado_sii
                dte.fecha_consulta_sii = datetime.utcnow()

                if nuevo_estado == estado_anterior:
                    resumen["sin_cambio"] += 1
                elif nuevo_estado == "aceptado":
                    resumen["aceptados"] += 1
                elif nuevo_estado == "rechazado":
                    resumen["rechazados"] += 1
                elif nuevo_estado == "reparo":
                    resumen["reparos"] += 1
                else:
                    resumen["sin_cambio"] += 1

                resumen["detalle"].append({
                    "tipo_dte": dte.tipo_dte,
                    "folio": dte.folio,
                    "track_id": track,
                    "estado_anterior": estado_anterior,
                    "estado_nuevo": nuevo_estado,
                    "estado_sii_raw": estado_sii,
                    "glosa": glosa,
                })

                logger.info(
                    "Boleta T%d F%d track=%s: %s -> %s (%s)",
                    dte.tipo_dte, dte.folio, track,
                    estado_anterior, nuevo_estado, estado_sii,
                )

            except Exception as e:
                resumen["errores"] += 1
                resumen["detalle"].append({
                    "tipo_dte": dte.tipo_dte,
                    "folio": dte.folio,
                    "track_id": dte.track_id,
                    "error": str(e),
                })
                logger.error(
                    "Error consultando Boleta T%d F%d track=%s: %s",
                    dte.tipo_dte, dte.folio, dte.track_id, e,
                    exc_info=True,
                )

    return resumen


def poll_libros(
    db: Session,
    empresa: Empresa,
    token: str,
) -> dict:
    """Consulta el estado de todos los Libros pendientes de una empresa.

    Args:
        db: Sesion SQLAlchemy
        empresa: Empresa a consultar
        token: Token SII vigente

    Returns:
        Resumen con conteo de actualizaciones por estado.
    """
    pendientes = (
        db.query(LibroGenerado)
        .filter(
            LibroGenerado.empresa_id == empresa.id,
            LibroGenerado.estado_sii.in_(["pendiente", "enviado"]),
            LibroGenerado.track_id.isnot(None),
            LibroGenerado.track_id != "",
        )
        .all()
    )

    resumen = {
        "total_consultados": 0,
        "aceptados": 0,
        "rechazados": 0,
        "sin_cambio": 0,
        "errores": 0,
        "detalle": [],
    }

    for libro in pendientes:
        resumen["total_consultados"] += 1
        try:
            resp = consultar_estado_envio(
                track_id=libro.track_id,
                token=token,
                rut_emisor=empresa.rut,
            )

            raw_xml = resp.get("raw", "")
            estado_sii, glosa = _parse_estado_envio(raw_xml)

            if estado_sii is None:
                resumen["errores"] += 1
                resumen["detalle"].append({
                    "tipo_libro": libro.tipo_libro,
                    "periodo": libro.periodo,
                    "track_id": libro.track_id,
                    "error": "No se pudo parsear respuesta SII",
                })
                continue

            nuevo_estado = ESTADO_LIBRO_MAP.get(estado_sii, "pendiente")
            estado_anterior = libro.estado_sii

            # Actualizar registro
            libro.estado_sii = nuevo_estado

            # Contabilizar
            if nuevo_estado == estado_anterior:
                resumen["sin_cambio"] += 1
            elif nuevo_estado == "aceptado":
                resumen["aceptados"] += 1
            elif nuevo_estado == "rechazado":
                resumen["rechazados"] += 1
            else:
                resumen["sin_cambio"] += 1

            resumen["detalle"].append({
                "tipo_libro": libro.tipo_libro,
                "periodo": libro.periodo,
                "track_id": libro.track_id,
                "estado_anterior": estado_anterior,
                "estado_nuevo": nuevo_estado,
                "estado_sii_raw": estado_sii,
                "glosa": glosa,
            })

            logger.info(
                "Libro %s %s track=%s: %s -> %s (%s)",
                libro.tipo_libro, libro.periodo, libro.track_id,
                estado_anterior, nuevo_estado, estado_sii,
            )

        except Exception as e:
            resumen["errores"] += 1
            resumen["detalle"].append({
                "tipo_libro": libro.tipo_libro,
                "periodo": libro.periodo,
                "track_id": libro.track_id,
                "error": str(e),
            })
            logger.error(
                "Error consultando Libro %s %s track=%s: %s",
                libro.tipo_libro, libro.periodo, libro.track_id, e,
                exc_info=True,
            )

    return resumen


def poll_all(
    db: Session,
    empresa: Empresa,
    token: str,
    token_boleta: str | None = None,
) -> dict:
    """Ejecuta polling completo de DTEs, Boletas y Libros pendientes.

    Args:
        db: Sesion SQLAlchemy
        empresa: Empresa a consultar
        token: Token SII vigente (SOAP)
        token_boleta: Token REST API para boletas (opcional).
            Si no se proporciona, las boletas pendientes no se consultaran.

    Returns:
        Resumen combinado de DTEs, Boletas y Libros.
    """
    resumen_dtes = poll_dtes(db, empresa, token, token_boleta=token_boleta)
    resumen_libros = poll_libros(db, empresa, token)

    return {
        "empresa_rut": empresa.rut,
        "dtes": resumen_dtes,
        "libros": resumen_libros,
        "total_consultados": (
            resumen_dtes["total_consultados"] + resumen_libros["total_consultados"]
        ),
        "total_actualizados": (
            resumen_dtes["aceptados"] + resumen_dtes["rechazados"] + resumen_dtes["reparos"]
            + resumen_libros["aceptados"] + resumen_libros["rechazados"]
        ),
    }
