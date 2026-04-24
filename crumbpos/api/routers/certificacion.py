"""Endpoints del módulo de certificación SII.

Este router permite al super admin certificar cualquier empresa nueva sin
intervención manual sobre el código fuente. La idea: el super admin sube
el archivo SIISetDePruebas{RUT}.txt que envía el SII, el parser lo entiende
y el wizard del frontend ejecuta paso a paso la certificación.

Por seguridad, todos los endpoints requieren rol super_admin.

Persistencia:
  Las "runs" del wizard se guardan en la BD de certificación de cada empresa
  (data/{rut}/certificacion.db, tablas certificacion_run/caso/libro). Esto
  permite al super admin abandonar el wizard y retomarlo donde lo dejó.
"""
from __future__ import annotations

import base64
import logging
import os
import tempfile
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

from crumbpos.api.dependencies import require_super_admin
from crumbpos.api.services.emision_dte import (
    EmisionResult,
    EmisorConfig,
    FacturaRequest,
    ServicioEmisionDTE,
)
from crumbpos.api.services import envio_libro_cert, envio_sobre_cert
from crumbpos.api.services.muestras_impresas_cert import generar_muestras_zip
from crumbpos.certificacion.cleanup import limpiar_certificacion
from crumbpos.certificacion.reiniciar import reiniciar_certificacion
from crumbpos.certificacion.parser_set_sii import (
    SET_BASICO,
    SET_EXENTA,
    SET_GUIAS,
    SetParseado,
    parse_set_sii_content,
)
from crumbpos.core.caf.caf_manager_db import CAFManagerDB
from crumbpos.db.models import (
    CafFolio,
    CertificacionCaso,
    CertificacionLibro,
    CertificacionRun,
    CERT_CASO_ESTADOS,
    CERT_LIBRO_ESTADOS,
    CERT_RUN_ESTADOS,
    DteEmitido,
    Empresa,
)
from crumbpos.db.multi_tenant import (
    cambiar_etapa,
    get_empresa_db_session,
    get_empresa_registro,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/certificacion",
    tags=["certificacion"],
    dependencies=[Depends(require_super_admin)],
)


# ══════════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════════

class RunPatchIn(BaseModel):
    """Campos opcionales a actualizar en una run."""
    estado: str | None = None
    screen_actual: int | None = None
    datos_setup: dict | None = None


class CasoPatchIn(BaseModel):
    estado: str | None = None
    folio: int | None = None
    dte_emitido_id: str | None = None
    trackid: str | None = None
    estado_sii: str | None = None
    error_mensaje: str | None = None


class LibroPatchIn(BaseModel):
    estado: str | None = None
    trackid: str | None = None
    estado_sii: str | None = None
    error_mensaje: str | None = None


class LibroGenerarIn(BaseModel):
    """Parámetros opcionales al generar un libro de certificación."""
    numero_atencion: int | None = None


# ══════════════════════════════════════════════════════════════════
# Endpoint legacy — solo parseo, sin persistencia
# ══════════════════════════════════════════════════════════════════

@router.post("/parse-set")
async def parse_set(file: UploadFile = File(...)) -> dict:
    """Parsea un set de pruebas SIN persistir.

    Endpoint legacy que devuelve la estructura parseada para vista previa.
    Para el flujo real del wizard, usar POST /runs que además persiste.
    """
    if not file.filename:
        raise HTTPException(400, "Archivo sin nombre")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Archivo vacío")
    content = _decodificar_set(raw)
    rut_hint = _rut_from_filename(file.filename)
    parseado = parse_set_sii_content(content, rut_hint=rut_hint)
    if not parseado.sets and not parseado.libro_compras:
        raise HTTPException(
            422,
            "El archivo no parece ser un set de pruebas válido del SII.",
        )
    return _serializar_parseado(parseado, file.filename)


# ══════════════════════════════════════════════════════════════════
# Runs — crear / leer / actualizar / borrar
# ══════════════════════════════════════════════════════════════════

@router.post("/runs")
async def crear_run(
    rut: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """Crea (o actualiza) la run de certificación de una empresa.

    Recibe el archivo SIISetDePruebas{RUT}.txt, lo parsea y persiste
    los casos y libros en la BD de certificación de la empresa.

    Si ya existe una run activa (no completada ni cancelada) para la
    empresa, reutiliza el mismo registro y reemplaza los casos/libros
    con los del nuevo set (caso típico: el super admin sube una versión
    corregida del set).

    Hook de etapa: si la empresa está en `pendiente_certificacion`,
    avanza automáticamente a `proceso_certificacion`.
    """
    if not file.filename:
        raise HTTPException(400, "Archivo sin nombre")

    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Archivo vacío")
    content = _decodificar_set(raw)
    parseado = parse_set_sii_content(content, rut_hint=rut)
    if not parseado.sets and not parseado.libro_compras:
        raise HTTPException(
            422,
            "El archivo no parece ser un set de pruebas válido del SII.",
        )

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _buscar_run_activa(session, rut)
        if run is not None:
            # Re-upload: reemplazar casos/libros con los del nuevo set
            for c in list(run.casos):
                session.delete(c)
            for lib in list(run.libros):
                session.delete(lib)
            run.archivo_nombre = file.filename
            run.archivo_contenido = content
            run.datos_parseados = _serializar_parseado(parseado, file.filename)
            run.estado = "set_cargado"
        else:
            run = CertificacionRun(
                rut_empresa=rut,
                estado="set_cargado",
                archivo_nombre=file.filename,
                archivo_contenido=content,
                datos_parseados=_serializar_parseado(parseado, file.filename),
            )
            session.add(run)
            session.flush()  # necesita id para los FKs

        _poblar_casos_y_libros(session, run, parseado)
        session.commit()
        session.refresh(run)
        run_dict = _serializar_run(run)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # Hook de etapa: pendiente → proceso_certificacion
    if registro.etapa == "pendiente_certificacion":
        try:
            cambiar_etapa(rut, "proceso_certificacion")
            run_dict["etapa_transicion"] = "proceso_certificacion"
        except Exception as e:  # no rompemos la request si falla
            logger.warning("No se pudo actualizar etapa para %s: %s", rut, e)

    return run_dict


@router.get("/runs/by-empresa/{rut}")
def obtener_run_activa(rut: str) -> dict:
    """Devuelve la run activa de una empresa (si existe).

    Una run está "activa" si su estado no es `completado` ni `cancelado`.
    Si no hay run activa, devuelve 404 — el frontend debe interpretar
    eso como "el wizard arranca desde cero".
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _buscar_run_activa(session, rut)
        if run is None:
            raise HTTPException(404, "No hay run activa para esta empresa")
        return _serializar_run(run)
    finally:
        session.close()


@router.get("/runs/{rut}/{run_id}")
def obtener_run(rut: str, run_id: str) -> dict:
    """Devuelve una run por id (necesita RUT para resolver la BD)."""
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")
    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = session.query(CertificacionRun).filter(
            CertificacionRun.id == run_id,
        ).first()
        if run is None:
            raise HTTPException(404, "Run no encontrada")
        return _serializar_run(run)
    finally:
        session.close()


def _hidratar_empresa_desde_datos_setup(
    session, rut: str, datos_setup: dict,
) -> None:
    """Upsertea la fila ``Empresa`` de la BD cert con los valores del form.

    El wizard manda los datos del form de screen 1 como ``datos_setup``.
    Ese diccionario tiene la info fiscal completa (razón social, giro,
    dirección, etc.) — lo aprovechamos para crear la fila ``Empresa`` si
    falta o actualizar los campos si ya existe.

    Sin esto, cualquier endpoint que pase por ``get_tenant`` (CAF upload,
    emisión de DTE, consulta de folios, ...) falla con
    ``Empresa {rut} no inicializada en BD certificacion`` después de un
    reset o restore parcial desde papelera. Observado con 77829149-5 en
    2026-04-21.

    El ``get_empresa_engine`` también hace un seed stub por su cuenta
    (defensa en profundidad). Esta función además *actualiza* los campos
    con los datos reales del formulario — el stub solo tiene RUT + razón
    social del registro master y strings vacíos en el resto.
    """
    razon = (datos_setup.get("razon_social") or "").strip()
    giro = (datos_setup.get("giro") or "").strip()
    direccion = (datos_setup.get("direccion") or "").strip()
    comuna = (datos_setup.get("comuna") or "").strip()
    ciudad = (datos_setup.get("ciudad") or "").strip()
    acteco_raw = datos_setup.get("acteco")
    firmante = (datos_setup.get("firmante") or "").strip() or None
    fecha_res = (datos_setup.get("fecha_resolucion") or "").strip() or None
    numero_res_raw = datos_setup.get("numero_resolucion")

    acteco: int | None = None
    if acteco_raw not in (None, "", 0, "0"):
        try:
            acteco = int(str(acteco_raw).strip())
        except (TypeError, ValueError):
            acteco = None

    numero_res: int = 0
    if numero_res_raw not in (None, ""):
        try:
            numero_res = int(str(numero_res_raw).strip())
        except (TypeError, ValueError):
            numero_res = 0

    empresa = session.query(Empresa).filter(Empresa.rut == rut).first()
    if empresa is None:
        import uuid as _uuid
        empresa = Empresa(
            id=str(_uuid.uuid4()),
            rut=rut,
            razon_social=razon,
            giro=giro,
            direccion=direccion,
            comuna=comuna,
            ciudad=ciudad,
            acteco=acteco,
            cert_rut_firmante=firmante,
            fecha_resolucion=fecha_res,
            numero_resolucion=numero_res,
            ambiente_sii="certificacion",
        )
        session.add(empresa)
        session.flush()
        return

    # Update: solo sobreescribimos con los campos no-vacíos provistos en
    # datos_setup — no queremos borrar datos existentes con "" si el
    # wizard mandó un patch parcial.
    if razon:
        empresa.razon_social = razon
    if giro:
        empresa.giro = giro
    if direccion:
        empresa.direccion = direccion
    if comuna:
        empresa.comuna = comuna
    if ciudad:
        empresa.ciudad = ciudad
    if acteco is not None:
        empresa.acteco = acteco
    if firmante:
        empresa.cert_rut_firmante = firmante
    if fecha_res:
        empresa.fecha_resolucion = fecha_res
    if numero_res:
        empresa.numero_resolucion = numero_res
    session.flush()


@router.patch("/runs/{rut}/{run_id}")
def actualizar_run(rut: str, run_id: str, patch: RunPatchIn) -> dict:
    """Actualiza campos de una run (estado, screen actual, datos de setup).

    Hook de etapa: cuando estado pasa a `completado`, la empresa avanza
    automáticamente a etapa `produccion` en master.db.
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    if patch.estado is not None and patch.estado not in CERT_RUN_ESTADOS:
        raise HTTPException(
            400, f"Estado inválido. Válidos: {CERT_RUN_ESTADOS}",
        )

    session = get_empresa_db_session(rut, "certificacion")
    nueva_etapa: str | None = None
    try:
        run = session.query(CertificacionRun).filter(
            CertificacionRun.id == run_id,
        ).first()
        if run is None:
            raise HTTPException(404, "Run no encontrada")

        if patch.estado is not None:
            run.estado = patch.estado
            if patch.estado == "completado":
                run.completed_at = datetime.utcnow()
                nueva_etapa = "produccion"
        if patch.screen_actual is not None:
            run.screen_actual = patch.screen_actual
        if patch.datos_setup is not None:
            run.datos_setup = patch.datos_setup
            _hidratar_empresa_desde_datos_setup(
                session, rut, patch.datos_setup,
            )
        session.commit()
        session.refresh(run)
        run_dict = _serializar_run(run)
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    if nueva_etapa is not None:
        try:
            cambiar_etapa(rut, nueva_etapa)
            run_dict["etapa_transicion"] = nueva_etapa
        except Exception as e:
            logger.warning("No se pudo actualizar etapa para %s: %s", rut, e)

    return run_dict


@router.delete("/runs/{rut}/{run_id}")
def eliminar_run(rut: str, run_id: str) -> dict:
    """Cancela y borra una run (y sus casos/libros en cascada).

    Útil si el super admin quiere reiniciar el wizard desde cero.
    NO revierte la etapa de la empresa — si estaba en `proceso_certificacion`,
    ahí se queda hasta que se cree una nueva run o se vuelva manualmente.
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")
    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = session.query(CertificacionRun).filter(
            CertificacionRun.id == run_id,
        ).first()
        if run is None:
            raise HTTPException(404, "Run no encontrada")
        session.delete(run)
        session.commit()
        return {"ok": True, "deleted": run_id}
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════
# Casos y libros
# ══════════════════════════════════════════════════════════════════

@router.patch("/casos/{rut}/{caso_id}")
def actualizar_caso(rut: str, caso_id: str, patch: CasoPatchIn) -> dict:
    """Actualiza el estado de un caso (cuando el wizard lo emite)."""
    if patch.estado is not None and patch.estado not in CERT_CASO_ESTADOS:
        raise HTTPException(
            400, f"Estado inválido. Válidos: {CERT_CASO_ESTADOS}",
        )
    session = get_empresa_db_session(rut, "certificacion")
    try:
        caso = session.query(CertificacionCaso).filter(
            CertificacionCaso.id == caso_id,
        ).first()
        if caso is None:
            raise HTTPException(404, "Caso no encontrado")

        if patch.estado is not None:
            caso.estado = patch.estado
            if patch.estado in ("emitido", "aprobado"):
                caso.emitido_at = caso.emitido_at or datetime.utcnow()
        if patch.folio is not None:
            caso.folio = patch.folio
        if patch.dte_emitido_id is not None:
            caso.dte_emitido_id = patch.dte_emitido_id
        if patch.trackid is not None:
            caso.trackid = patch.trackid
        if patch.estado_sii is not None:
            caso.estado_sii = patch.estado_sii
        if patch.error_mensaje is not None:
            caso.error_mensaje = patch.error_mensaje
        session.commit()
        session.refresh(caso)
        return _serializar_caso(caso)
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/casos/{rut}/{caso_id}/emitir")
def emitir_caso(
    rut: str,
    caso_id: str,
) -> dict:
    """Emite un caso del set de pruebas SIN enviar al SII (genera XML firmado).

    Esto es el primer paso del flujo de certificación que ejecuta el wizard:
      1. Carga el caso y los datos de la empresa (cert, CAFs, dirección).
      2. Construye el FacturaRequest desde caso.datos.
      3. Llama a ServicioEmisionDTE.emitir_factura(req, enviar_sii=False).
         → Genera XML, firma, asigna folio del CAF correspondiente.
         → NO envía al SII.
      4. Persiste DteEmitido (con xml_firmado en base64) y actualiza el caso
         (folio, dte_emitido_id, estado='emitido', emitido_at).
      5. Devuelve el caso actualizado para que el wizard refresque la UI.

    El envío real al SII (armar el sobre y obtener trackid) es otro paso
    posterior del wizard. Separar "armar XML válido" de "enviarlo al SII"
    permite revisar el documento antes de enviarlo y evita quemar folios si
    el sobre se rechaza por un detalle no relacionado al folio.

    Re-emisión — UN SOLO modo:
      - Si el caso ya está 'emitido', se regenera el XML con el MISMO folio
        (útil cuando el SII rechaza el sobre completo con STATUS=7 por XSD —
        nada se quemó, el folio sigue intacto).
      - Cuando un folio sí quedó quemado (DTE-3-100/101), primero hay que
        llamar a ``POST /descartar-folio`` para resetear el caso a
        'pendiente', y luego volver a emitir (segundo paso del flujo).
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        caso = session.query(CertificacionCaso).filter(
            CertificacionCaso.id == caso_id,
        ).first()
        if caso is None:
            raise HTTPException(404, "Caso no encontrado")

        # ── Gating de re-emisión ──────────────────────────────────────
        # Un caso ya en 'emitido' puede regenerarse reutilizando el MISMO
        # folio (sobre rechazado por XSD, folio intacto). Requiere que el
        # caso NO tenga avance declarado ni aprobación — después de eso
        # re-emitirlo con distinto contenido sería fraude.
        es_reemision_mismo_folio = False
        if caso.estado == "emitido":
            if caso.avance_declarado_at is not None:
                raise HTTPException(
                    409,
                    f"No se puede re-emitir el caso {caso.numero_caso}: ya tiene "
                    f"avance declarado al SII ({caso.avance_declarado_at}). "
                    "Emitir una NC si necesitas corregirlo.",
                )
            if caso.aprobado_at is not None:
                raise HTTPException(
                    409,
                    f"No se puede re-emitir el caso {caso.numero_caso}: ya está "
                    f"aprobado por el SII ({caso.aprobado_at}).",
                )
            if caso.folio is None:
                raise HTTPException(
                    409,
                    f"El caso {caso.numero_caso} está en 'emitido' pero no tiene "
                    "folio — estado inconsistente, revisar manualmente.",
                )
            es_reemision_mismo_folio = True
        elif caso.estado not in ("pendiente", "error", "rechazado"):
            raise HTTPException(
                409,
                f"El caso ya está en estado '{caso.estado}'. "
                "No se puede emitir en este estado.",
            )

        servicio, empresa = _get_servicio_for_certificacion(session, rut)
        req = _caso_a_factura_request(session, caso, empresa)

        try:
            # session+empresa_id habilitan enriquecimiento CodRef=3 en el core:
            # la NC/ND que modifica monto lee los ítems originales desde
            # DteEmitido del documento referenciado. Directriz del usuario:
            # "todos los fixes deben ser globales, no parches" — un solo core
            # procesa documentos tanto en certificación como en producción.
            #
            # folio_override: si el caso ya tiene folio asignado, lo
            # respetamos (no consumimos otro del CAF). Dos escenarios:
            #   1. Re-emisión del mismo folio en set de pruebas (sobre SII
            #      rechazado por XSD; folio intacto).
            #   2. Set de simulación: el preview PRE-RESERVA el folio con
            #      CAFManagerDB.siguiente_folio y lo guarda en caso.folio;
            #      al emitir hay que reusarlo, no pedir uno nuevo.
            # Para casos del set de pruebas con estado 'pendiente',
            # caso.folio es None y el servicio asigna uno con siguiente_folio.
            resultado: EmisionResult = servicio.emitir_factura(
                req,
                enviar_sii=False,
                folio_override=caso.folio,
                session=session,
                empresa_id=empresa.id,
            )
        except Exception as exc:
            logger.error(
                "Error emitiendo caso %s (%s): %s",
                caso.numero_caso, caso_id, exc, exc_info=True,
            )
            caso.estado = "error"
            caso.error_mensaje = str(exc)[:500]
            session.commit()
            raise HTTPException(500, f"Error al emitir el caso: {exc}")

        if not resultado.ok:
            caso.estado = "error"
            caso.error_mensaje = (resultado.error or "Error desconocido")[:500]
            session.commit()
            session.refresh(caso)
            return _serializar_caso(caso)

        # Persistir DteEmitido (xml_firmado en base64 → texto)
        xml_text = None
        if resultado.xml_firmado:
            xml_text = base64.b64encode(resultado.xml_firmado).decode("ascii")

        dte_record = None

        if es_reemision_mismo_folio and caso.dte_emitido_id:
            # Actualizar el DteEmitido existente en vez de crear otro —
            # preserva el id y evita huérfanos. El folio no cambia.
            dte_record = session.get(DteEmitido, caso.dte_emitido_id)
            if dte_record is None:
                # Defensive: si el puntero quedó colgando, creamos uno nuevo
                logger.warning(
                    "dte_emitido_id=%s no encontrado en re-emisión de caso %s. "
                    "Creando DteEmitido nuevo.",
                    caso.dte_emitido_id, caso.numero_caso,
                )
            else:
                dte_record.fecha_emision = date.fromisoformat(
                    datetime.now().strftime("%Y-%m-%d"),
                )
                dte_record.receptor_rut = req.receptor_rut
                dte_record.receptor_razon = req.receptor_razon
                dte_record.monto_neto = resultado.monto_neto
                dte_record.monto_exento = resultado.monto_exento
                dte_record.iva = resultado.iva
                dte_record.monto_total = resultado.monto_total or 0
                dte_record.xml_firmado = xml_text
                dte_record.ted_xml = resultado.ted_xml
                dte_record.estado_sii = "pendiente"
                session.flush()
        if dte_record is None:
            dte_record = DteEmitido(
                empresa_id=empresa.id,
                tipo_dte=caso.tipo_dte,
                folio=resultado.folio,
                fecha_emision=date.fromisoformat(datetime.now().strftime("%Y-%m-%d")),
                receptor_rut=req.receptor_rut,
                receptor_razon=req.receptor_razon,
                monto_neto=resultado.monto_neto,
                monto_exento=resultado.monto_exento,
                iva=resultado.iva,
                monto_total=resultado.monto_total or 0,
                xml_firmado=xml_text,
                ted_xml=resultado.ted_xml,
                estado_sii="pendiente",  # aún no se envió al SII
            )
            session.add(dte_record)
            session.flush()

        caso.folio = resultado.folio
        caso.dte_emitido_id = dte_record.id
        caso.estado = "emitido"
        caso.emitido_at = datetime.utcnow()
        caso.error_mensaje = None
        # Limpiar trackid viejo: el sobre anterior fue rechazado, el
        # próximo envío pedirá uno nuevo.
        caso.trackid = None
        caso.estado_sii = None
        session.commit()
        session.refresh(caso)
        return _serializar_caso(caso)
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/casos/{rut}/{caso_id}/descartar-folio")
def descartar_folio_caso(rut: str, caso_id: str) -> dict:
    """Descarta el folio emitido de un caso SIN re-emitir — 1er paso del flujo.

    Cuando el SII responde EPR al sobre pero rechaza el DTE individual con
    DTE-3-100 / DTE-3-101, ese folio queda quemado en la historia del SII.
    Este endpoint:

    1. Marca el DteEmitido asociado como ``estado_sii='descartado'``. NO se
       borra — queda en la tabla para auditoría del folio perdido (con su
       XML firmado original, fecha, monto y todo).
    2. Resetea el caso: ``folio=None``, ``dte_emitido_id=None``,
       ``estado='pendiente'``, limpia ``trackid`` / ``estado_sii`` /
       ``error_mensaje`` / ``emitido_at``.

    El caso queda visiblemente en PENDIENTE en la UI, para que el usuario
    entienda qué pasó (antes, cuando esta operación era atómica dentro del
    flujo de emisión, el caso nunca salía de "emitido" y era confuso).

    Segundo paso del flujo: el usuario hace click en "Emitir" normalmente
    y el servicio de emisión asigna el próximo folio libre del CAF, como
    si fuese la primera vez.

    Bloqueado si el caso tiene ``avance_declarado_at`` o ``aprobado_at``:
    esos folios ya quedaron en la historia oficial del SII.

    Errores:
        - 404 si el caso no existe.
        - 409 si el caso tiene avance declarado o está aprobado.
        - 422 si el caso no tiene folio para descartar (ya pendiente).
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        caso = session.query(CertificacionCaso).filter(
            CertificacionCaso.id == caso_id,
        ).first()
        if caso is None:
            raise HTTPException(404, "Caso no encontrado")

        if caso.avance_declarado_at is not None:
            raise HTTPException(
                409,
                f"No se puede descartar el folio del caso {caso.numero_caso}: "
                f"ya tiene avance declarado al SII "
                f"({caso.avance_declarado_at}).",
            )
        if caso.aprobado_at is not None:
            raise HTTPException(
                409,
                f"No se puede descartar el folio del caso {caso.numero_caso}: "
                f"ya está aprobado por el SII ({caso.aprobado_at}).",
            )
        if caso.estado != "emitido" or caso.dte_emitido_id is None:
            raise HTTPException(
                422,
                f"El caso {caso.numero_caso} no tiene un folio emitido para "
                f"descartar (estado='{caso.estado}').",
            )

        # Preservar el DteEmitido viejo con estado 'descartado' — auditable.
        # Usamos 'descartado' (10 chars) en vez de 'rechazado_folio_quemado_sii'
        # (27 chars) para mantener compatibilidad con VARCHAR(15) en PostgreSQL;
        # SQLite es permisivo pero producción no lo será.
        dte_viejo = session.get(DteEmitido, caso.dte_emitido_id)
        if dte_viejo is not None:
            dte_viejo.estado_sii = "descartado"
            logger.info(
                "Caso %s: folio %s (tipo %s) marcado como descartado. "
                "El caso vuelve a estado 'pendiente' — próxima emisión "
                "usará folio nuevo del CAF.",
                caso.numero_caso, dte_viejo.folio, dte_viejo.tipo_dte,
            )
        else:
            logger.warning(
                "dte_emitido_id=%s apunta a un DTE inexistente al descartar "
                "el folio del caso %s. Se resetea el caso igual.",
                caso.dte_emitido_id, caso.numero_caso,
            )

        caso.folio = None
        caso.dte_emitido_id = None
        caso.estado = "pendiente"
        caso.emitido_at = None
        caso.trackid = None
        caso.estado_sii = None
        caso.error_mensaje = None
        session.commit()
        session.refresh(caso)
        return _serializar_caso(caso)
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════
# Sobre multi-DTE del set (armar / enviar al SII / consultar)
# ══════════════════════════════════════════════════════════════════

def _cargar_run_por_id(session, rut: str, run_id: str) -> CertificacionRun:
    """Helper común — carga una run o devuelve 404."""
    run = session.query(CertificacionRun).filter(
        CertificacionRun.id == run_id,
        CertificacionRun.rut_empresa == rut,
    ).first()
    if run is None:
        raise HTTPException(404, f"Run {run_id} no encontrada para {rut}")
    return run


@router.post("/sets/{rut}/{run_id}/{set_nombre}/armar-sobre")
def armar_sobre_set(rut: str, run_id: str, set_nombre: str) -> dict:
    """Vista previa del sobre EnvioDTE del set — NO envía nada al SII.

    Arma + firma el sobre multi-DTE con todos los casos emitidos del
    set y devuelve resumen + sha256 para el modal de confirmación R6.
    El XML completo NO se devuelve (es grande y el modal no lo necesita);
    si hace falta debuggearlo hay que mirar los logs del server.

    Este endpoint es idempotente: no modifica nada en la BD, solo lee.
    Errores esperados (422):
        - Algún caso del set no está emitido.
        - Algún caso emitido no tiene dte_emitido_id.
        - La firma del sobre no verifica (cert corrupto o mal firmado).
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        servicio, empresa = _get_servicio_for_certificacion(session, rut)
        try:
            resultado = envio_sobre_cert.armar_sobre(
                session, run, set_nombre, servicio, empresa,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            raise HTTPException(500, str(e))

        # El XML crudo no sale de aquí — solo el resumen y el hash.
        return {
            "set_nombre": set_nombre,
            "run_id": run_id,
            "sha256": resultado["sha256"],
            "resumen_por_tipo": resultado["resumen_por_tipo"],
            "folios": resultado["folios"],
            "total_dtes": sum(resultado["resumen_por_tipo"].values()),
            "tamano_bytes": len(resultado["xml_bytes"]),
            "url_sii": resultado["url_sii"],
            "casos_ids": resultado["casos_ids"],
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/sets/{rut}/{run_id}/{set_nombre}/enviar-sobre")
def enviar_sobre_set(rut: str, run_id: str, set_nombre: str) -> dict:
    """Envía el sobre EnvioDTE del set al SII.

    Este endpoint dispara I/O contra el SII (POST a palena/maullin). Por
    R6, el wizard del frontend DEBE haber mostrado antes el resumen del
    sobre (vía ``/armar-sobre``) y el usuario debe haber hecho click en
    "Confirmar envío". El servidor no puede verificar la confirmación —
    confía en que el frontend respetó el flujo.

    Persiste trackid en cada caso del set si el envío fue exitoso. Si
    el SII rechaza el sobre, guarda la glosa del rechazo en
    ``error_mensaje`` de cada caso pero mantiene el estado 'emitido'
    (para poder reintentar sin perder los folios, que ya están quemados).
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        servicio, empresa = _get_servicio_for_certificacion(session, rut)
        try:
            resultado = envio_sobre_cert.enviar_sobre(
                session, run, set_nombre, servicio, empresa,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            logger.error(
                "Error enviando sobre cert (set=%s, rut=%s): %s",
                set_nombre, rut, e, exc_info=True,
            )
            raise HTTPException(500, str(e))

        return {
            "set_nombre": set_nombre,
            "run_id": run_id,
            **resultado,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/sets/{rut}/{run_id}/{set_nombre}/consultar-estado")
def consultar_estado_set(rut: str, run_id: str, set_nombre: str) -> dict:
    """Consulta al SII el estado del envío del set por trackid.

    Usa el trackid guardado en los casos del set tras el envío previo.
    Actualiza ``estado_sii`` de los casos con lo que responde el SII
    (EPR, SOK, SRH, etc.) y, si hay rechazo, guarda la glosa como
    ``error_mensaje``.

    R8: estado ``enviado`` + ``estado_sii=EPR`` NO significa aprobado.
    La aprobación final del set requiere declarar avance — eso va a
    vivir en Fase 5.
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        servicio, empresa = _get_servicio_for_certificacion(session, rut)
        try:
            resultado = envio_sobre_cert.consultar_estado(
                session, run, set_nombre, servicio, empresa,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            logger.error(
                "Error consultando estado sobre cert "
                "(set=%s, rut=%s): %s",
                set_nombre, rut, e, exc_info=True,
            )
            raise HTTPException(500, str(e))

        return {
            "set_nombre": set_nombre,
            "run_id": run_id,
            **resultado,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════
# Declarar avance y marcar aprobado — sets (Fase 5)
# ══════════════════════════════════════════════════════════════════


@router.post("/sets/{rut}/{run_id}/{set_nombre}/declarar-avance")
def declarar_avance_set(rut: str, run_id: str, set_nombre: str) -> dict:
    """Registra que el usuario declaró avance de este set en la web del SII.

    La declaración de avance es un paso manual en el portal del SII
    (maullin.sii.cl → Certificación → Declarar Avance). Este endpoint
    solo registra la fecha en ``avance_declarado_at`` de cada caso del
    set para que el wizard pueda mostrar el progreso.

    Precondición: todos los casos deben tener trackid (sobre enviado).
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        try:
            resultado = envio_sobre_cert.declarar_avance(
                session, run, set_nombre,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))

        return {
            "run_id": run_id,
            **resultado,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/sets/{rut}/{run_id}/{set_nombre}/marcar-aprobado")
def marcar_aprobado_set(rut: str, run_id: str, set_nombre: str) -> dict:
    """Marca el set como aprobado por el SII.

    El usuario debe haber verificado en la web del SII (o vía
    "Consultar estado") que el estado es SOK/aprobado. Este endpoint
    registra ``aprobado_at`` y cambia ``estado`` a 'aprobado' en cada
    caso del set.

    Precondición: ``avance_declarado_at`` debe estar seteado.
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        try:
            resultado = envio_sobre_cert.marcar_aprobado(
                session, run, set_nombre,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))

        return {
            "run_id": run_id,
            **resultado,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════
# Libros de certificación (generar / enviar al SII / consultar)
# ══════════════════════════════════════════════════════════════════


@router.post("/libros/{rut}/{run_id}/{libro_id}/generar")
def generar_libro_endpoint(
    rut: str, run_id: str, libro_id: str,
    body: LibroGenerarIn | None = None,
) -> dict:
    """Genera y firma el XML del libro SIN enviar al SII.

    Operación idempotente — puede llamarse varias veces para
    regenerar el XML. Devuelve SHA-256 y tamaño para el modal de
    confirmación R6.

    Si ``body.numero_atencion`` viene seteado, actualiza el libro
    antes de generar (evita que el frontend tenga que hacer un PATCH
    previo).
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        servicio, empresa = _get_servicio_for_certificacion(session, rut)

        # Actualizar numero_atencion si viene en el body
        if body and body.numero_atencion is not None:
            libro = session.get(CertificacionLibro, libro_id)
            if libro:
                libro.numero_atencion = body.numero_atencion
                session.flush()

        try:
            resultado = envio_libro_cert.generar_libro(
                session, run, libro_id, servicio, empresa,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            raise HTTPException(500, str(e))

        return {
            "libro_id": libro_id,
            "run_id": run_id,
            "tipo_libro": resultado["tipo_libro"],
            "sha256": resultado["sha256"],
            "tamano_bytes": resultado["tamano_bytes"],
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/libros/{rut}/{run_id}/{libro_id}/enviar")
def enviar_libro_endpoint(rut: str, run_id: str, libro_id: str) -> dict:
    """Genera (si falta) + envía el libro al SII.

    R6: el wizard DEBE mostrar antes el resumen del libro (vía
    ``/generar``) y el usuario debe confirmar. Este endpoint confía
    en que la confirmación ya ocurrió.

    Persiste trackid y estado_sii si el envío fue exitoso.
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        servicio, empresa = _get_servicio_for_certificacion(session, rut)
        try:
            resultado = envio_libro_cert.enviar_libro(
                session, run, libro_id, servicio, empresa,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            logger.error(
                "Error enviando libro cert (tipo=%s, rut=%s): %s",
                libro_id, rut, e, exc_info=True,
            )
            raise HTTPException(500, str(e))

        return {
            "libro_id": libro_id,
            "run_id": run_id,
            **resultado,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/libros/{rut}/{run_id}/{libro_id}/consultar-estado")
def consultar_estado_libro_endpoint(
    rut: str, run_id: str, libro_id: str,
) -> dict:
    """Consulta al SII el estado del libro por trackid.

    Actualiza ``estado_sii`` del libro con lo que responde el SII
    (LOK, SOK, LNC, SRH, etc.) y guarda glosa si hay rechazo.

    R8: LOK/SOK no implica que el SET de pruebas completo haya sido
    aprobado. Eso se verifica en Fase 5.
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        servicio, empresa = _get_servicio_for_certificacion(session, rut)
        try:
            resultado = envio_libro_cert.consultar_estado_libro(
                session, run, libro_id, servicio, empresa,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            logger.error(
                "Error consultando estado libro cert "
                "(libro=%s, rut=%s): %s",
                libro_id, rut, e, exc_info=True,
            )
            raise HTTPException(500, str(e))

        return {
            "libro_id": libro_id,
            "run_id": run_id,
            **resultado,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/libros/{rut}/{run_id}/{libro_id}/reiniciar-envio")
def reiniciar_envio_libro_endpoint(
    rut: str, run_id: str, libro_id: str,
) -> dict:
    """Resetea el estado de envío del libro para poder regenerarlo.

    Análogo al ``descartar-folio`` de DTEs: cuando el libro ya fue
    enviado al SII pero necesita regenerarse (ej: faltó N° Atención,
    el SII devolvió reparos de cálculo, o el core tenía un bug que ya
    se arregló), este endpoint limpia trackid/estado_sii/error_mensaje
    y deja el libro en ``pendiente`` para volver a ``generar`` +
    ``enviar``.

    Preserva ``xml_libro``, ``datos`` y ``numero_atencion``. Bloquea
    con 422 si el libro ya tiene avance declarado o está aprobado —
    esos son estados inmutables del progreso ante el SII.
    """
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        try:
            resultado = envio_libro_cert.reiniciar_envio_libro(
                session, run, libro_id,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))

        return {"run_id": run_id, **resultado}
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════
# Declarar avance y marcar aprobado — libros (Fase 5)
# ══════════════════════════════════════════════════════════════════


@router.post("/libros/{rut}/{run_id}/{libro_id}/declarar-avance")
def declarar_avance_libro_endpoint(
    rut: str, run_id: str, libro_id: str,
) -> dict:
    """Registra que el usuario declaró avance del libro en la web del SII."""
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        try:
            resultado = envio_libro_cert.declarar_avance_libro(
                session, run, libro_id,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))

        return {"run_id": run_id, **resultado}
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/libros/{rut}/{run_id}/{libro_id}/marcar-aprobado")
def marcar_aprobado_libro_endpoint(
    rut: str, run_id: str, libro_id: str,
) -> dict:
    """Marca el libro como aprobado por el SII."""
    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = _cargar_run_por_id(session, rut, run_id)
        try:
            resultado = envio_libro_cert.marcar_aprobado_libro(
                session, run, libro_id,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))

        return {"run_id": run_id, **resultado}
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.patch("/libros/{rut}/{libro_id}")
def actualizar_libro(rut: str, libro_id: str, patch: LibroPatchIn) -> dict:
    """Actualiza el estado de un libro (cuando el wizard lo envía)."""
    if patch.estado is not None and patch.estado not in CERT_LIBRO_ESTADOS:
        raise HTTPException(
            400, f"Estado inválido. Válidos: {CERT_LIBRO_ESTADOS}",
        )
    session = get_empresa_db_session(rut, "certificacion")
    try:
        libro = session.query(CertificacionLibro).filter(
            CertificacionLibro.id == libro_id,
        ).first()
        if libro is None:
            raise HTTPException(404, "Libro no encontrado")

        if patch.estado is not None:
            libro.estado = patch.estado
            if patch.estado in ("enviado", "aprobado"):
                libro.enviado_at = libro.enviado_at or datetime.utcnow()
        if patch.trackid is not None:
            libro.trackid = patch.trackid
        if patch.estado_sii is not None:
            libro.estado_sii = patch.estado_sii
        if patch.error_mensaje is not None:
            libro.error_mensaje = patch.error_mensaje
        session.commit()
        session.refresh(libro)
        return _serializar_libro(libro)
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════
# Set de Simulación (paso 2/6 de la certificación)
# ══════════════════════════════════════════════════════════════════


class SimulacionConfigIn(BaseModel):
    """Configuración del Set de Simulación enviada desde el wizard.

    ``slots`` es una lista de 16 dicts, uno por cada slot del esquema
    aprobado (9×T33 + 3×T34 + 2×T52 + 1×T61 + 1×T56). Cada slot trae:

        {
            "slot": 1,
            "items": [{"nombre": "...", "cantidad": 50, "precio": 1890}, ...],
            "razon": "..."  # solo slots 15 y 16 (opcional)
        }

    ``receptor_rut`` vacío implica autofactura (único válido en
    ambiente certificación).
    """
    slots: list[dict]
    receptor_rut: str = ""


SIMULACION_SET_NOMBRE = "SIMULACION"
# Base para numero_atencion de casos de simulación (no viene del SII).
# 80000+ evita chocar con los numero_atencion reales del set de pruebas
# (~4M) y hace evidente en la UI que es simulación.
_NUM_ATENCION_BASE_SIMULACION = 80_000


@router.post("/simulacion/{rut}/{run_id}/configurar")
def configurar_simulacion(
    rut: str, run_id: str, body: SimulacionConfigIn,
) -> dict:
    """Valida y persiste la config del Set de Simulación en la run.

    La config queda en ``CertificacionRun.datos_setup["simulacion"]`` (no
    se crea tabla nueva). Esto permite al super admin volver a la
    pantalla 4 del wizard y editar la config antes de generar el preview.

    Validaciones (ver ``crumbpos.certificacion.simulacion.validador``):

    - Exactamente 16 slots con los patrones del esquema aprobado.
    - Cada slot respeta su ``min_items``/``max_items`` (ver ``SLOT_SPECS``).
    - Items con nombre no vacío, cantidad > 0, precio > 0 (salvo slot 14
      que acepta precio 0 por traslado interno).
    - Receptor RUT vacío (autofactura) o formato ``XXXXXXXX-Y``.
    """
    # Import local para no pagar costo al cargar el router.
    from crumbpos.certificacion.simulacion.validador import (
        validar_config_simulacion,
    )

    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    config = body.model_dump()
    try:
        validar_config_simulacion(config)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = session.query(CertificacionRun).filter(
            CertificacionRun.id == run_id,
        ).first()
        if run is None:
            raise HTTPException(404, "Run no encontrada")

        # Merge dentro de datos_setup para preservar otros campos del wizard.
        datos = dict(run.datos_setup or {})
        datos["simulacion"] = config
        run.datos_setup = datos
        # SQLAlchemy no detecta mutaciones dentro de JSON columns; reasignar
        # el dict completo fuerza el UPDATE. (Lo mismo aplica en el resto
        # del router para datos_parseados.)
        session.commit()
        session.refresh(run)
        return {
            "ok": True,
            "run_id": run.id,
            "config": datos["simulacion"],
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/simulacion/{rut}/{run_id}/preview")
def preview_simulacion(rut: str, run_id: str) -> dict:
    """Reserva folios del CAF, genera los DTEs de simulación y persiste casos.

    Precondición: la run debe tener ``datos_setup["simulacion"]`` seteado
    por ``/configurar``.

    Este endpoint es IRREVERSIBLE desde la perspectiva del CAF: consume
    los folios secuencialmente (``CAFManagerDB.siguiente_folio``). Si el
    usuario quiere reshuffle, usa ``/regenerar`` que reutiliza los
    folios ya reservados en vez de consumir nuevos.

    Devuelve la tabla de preview con todos los DTEs listos para el paso
    de emisión (``POST /casos/{rut}/{caso_id}/emitir`` ya existente).
    """
    from crumbpos.certificacion.simulacion.generador import (
        CANTIDAD_POR_TIPO,
        armar_dtes_simulacion,
    )

    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = session.query(CertificacionRun).filter(
            CertificacionRun.id == run_id,
        ).first()
        if run is None:
            raise HTTPException(404, "Run no encontrada")

        config = (run.datos_setup or {}).get("simulacion")
        if not config:
            raise HTTPException(
                422,
                "Simulación sin configurar. Llama "
                "POST /simulacion/.../configurar primero.",
            )

        # Si ya hay casos, bloquear — el usuario debe usar /regenerar.
        existentes = session.query(CertificacionCaso).filter(
            CertificacionCaso.run_id == run.id,
            CertificacionCaso.set_nombre == SIMULACION_SET_NOMBRE,
        ).count()
        if existentes > 0:
            raise HTTPException(
                409,
                f"La simulación ya tiene {existentes} casos generados. "
                "Usa POST /simulacion/.../regenerar si quieres rehacer el "
                "preview, o emitirlos con el flujo normal.",
            )

        empresa = session.query(Empresa).filter(Empresa.rut == rut).first()
        if not empresa:
            raise HTTPException(
                404, f"Empresa {rut} no inicializada en BD certificación",
            )

        caf_mgr = CAFManagerDB(session, empresa.id)

        # Reservar folios del CAF en las cantidades fijas del esquema
        # aprobado (9 T33, 3 T34, 2 T52, 1 T61, 1 T56). Las cantidades
        # no son configurables — son contrato con el XML que el SII
        # aprobó a 77051056-2 el 2026-03-26.
        folios_por_tipo: dict[int, list[int]] = {}
        for tipo, cantidad in CANTIDAD_POR_TIPO.items():
            folios_por_tipo[tipo] = []
            for _ in range(cantidad):
                try:
                    folio, _caf = caf_mgr.siguiente_folio(tipo)
                except ValueError as e:
                    raise HTTPException(
                        422,
                        f"Sin folios disponibles para T{tipo}: {e}",
                    ) from None
                folios_por_tipo[tipo].append(folio)

        # Generar estructura pura (sin tocar DB).
        try:
            dtes = armar_dtes_simulacion(config, folios_por_tipo)
        except ValueError as e:
            raise HTTPException(500, f"Generador: {e}") from None

        # Persistir como CertificacionCaso con set_nombre=SIMULACION.
        for idx, dte in enumerate(dtes, start=1):
            caso = CertificacionCaso(
                run_id=run.id,
                set_nombre=SIMULACION_SET_NOMBRE,
                numero_caso=dte["numero_caso"],
                numero_atencion=_NUM_ATENCION_BASE_SIMULACION + idx,
                tipo_dte=dte["tipo_dte"],
                folio=dte["folio"],
                datos=dte,
                estado="pendiente",
            )
            session.add(caso)

        session.commit()
        return {
            "ok": True,
            "total": len(dtes),
            "dtes": dtes,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/simulacion/{rut}/{run_id}/regenerar")
def regenerar_simulacion(rut: str, run_id: str) -> dict:
    """Re-genera el preview con la config actual reutilizando folios.

    A diferencia de ``/preview``, este endpoint NO consume folios nuevos
    del CAF — reusa los folios de los casos existentes. Esto cumple la
    regla "no quemar folios innecesariamente" (CLAUDE.md #3): si el
    usuario editó items/precios de algún slot, puede re-crear los casos
    sin perder los folios ya reservados.

    Bloquea si algún caso ya tiene ``trackid`` o ``avance_declarado_at``
    — eso significa que el sobre ya se envió al SII y regenerar sería
    cambiar DTEs declarados (fraude).
    """
    from crumbpos.certificacion.simulacion.generador import (
        armar_dtes_simulacion,
    )

    registro = get_empresa_registro(rut)
    if not registro:
        raise HTTPException(404, f"Empresa {rut} no encontrada")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = session.query(CertificacionRun).filter(
            CertificacionRun.id == run_id,
        ).first()
        if run is None:
            raise HTTPException(404, "Run no encontrada")

        config = (run.datos_setup or {}).get("simulacion")
        if not config:
            raise HTTPException(
                422,
                "Simulación sin configurar. Llama "
                "POST /simulacion/.../configurar primero.",
            )

        casos = session.query(CertificacionCaso).filter(
            CertificacionCaso.run_id == run.id,
            CertificacionCaso.set_nombre == SIMULACION_SET_NOMBRE,
        ).all()

        if not casos:
            raise HTTPException(
                409,
                "No hay casos de simulación para regenerar. Llama "
                "POST /simulacion/.../preview primero.",
            )

        # Bloquear si ya hay emisión real al SII.
        for c in casos:
            if c.trackid or c.avance_declarado_at or c.aprobado_at:
                raise HTTPException(
                    409,
                    f"No se puede regenerar: el caso {c.numero_caso} ya "
                    "tiene trackid o avance declarado al SII.",
                )

        # Extraer folios ya reservados desde los casos existentes.
        folios_por_tipo: dict[int, list[int]] = {}
        for c in casos:
            folios_por_tipo.setdefault(c.tipo_dte, []).append(c.folio)

        # Borrar casos y DteEmitido asociados (si hubo emitir sin trackid).
        for c in casos:
            if c.dte_emitido_id:
                dte = session.query(DteEmitido).filter(
                    DteEmitido.id == c.dte_emitido_id,
                ).first()
                if dte is not None:
                    session.delete(dte)
            session.delete(c)
        session.flush()

        # Re-generar con la config actual (esquema fijo, sin randomness).
        try:
            dtes = armar_dtes_simulacion(config, folios_por_tipo)
        except ValueError as e:
            raise HTTPException(500, f"Generador: {e}") from None

        for idx, dte in enumerate(dtes, start=1):
            caso = CertificacionCaso(
                run_id=run.id,
                set_nombre=SIMULACION_SET_NOMBRE,
                numero_caso=dte["numero_caso"],
                numero_atencion=_NUM_ATENCION_BASE_SIMULACION + idx,
                tipo_dte=dte["tipo_dte"],
                folio=dte["folio"],
                datos=dte,
                estado="pendiente",
            )
            session.add(caso)

        session.commit()
        return {
            "ok": True,
            "total": len(dtes),
            "dtes": dtes,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════
# Muestras impresas — ZIP de PDFs para subir al portal SII
# ══════════════════════════════════════════════════════════════════


@router.get("/muestras/{rut}/{run_id}/zip")
def descargar_muestras_zip(rut: str, run_id: str) -> Response:
    """Genera y descarga un ZIP con las muestras impresas de la certificación.

    Cada DTE emitido produce un PDF tributario. Los tipos cedibles
    (33, 34, 52) producen además un PDF cedible. El ZIP se organiza
    por set — cubre tanto el set de pruebas (``basico/``, ``guias/``,
    ``exenta/``) como el set de simulación (``simulacion/``) — el
    servicio agrupa por ``set_nombre`` sin filtrar, así que cualquier
    caso con ``dte_emitido_id`` entra automáticamente.
    """
    session = get_empresa_db_session(rut, "certificacion")
    try:
        run = session.get(CertificacionRun, run_id)
        if not run:
            raise HTTPException(404, f"Run {run_id} no encontrada.")
        if run.rut_empresa != rut:
            raise HTTPException(403, "Run no pertenece a esta empresa.")

        empresa = session.query(Empresa).filter(Empresa.rut == rut).first()
        if not empresa:
            raise HTTPException(
                404, f"Empresa {rut} no encontrada en BD de certificación.",
            )

        zip_bytes, resumen = generar_muestras_zip(session, run, empresa)

        if resumen["total_pdfs"] == 0:
            raise HTTPException(
                422,
                "No se generaron PDFs. Verifica que los DTEs estén emitidos.",
            )

        filename = f"muestras_impresas_{rut}_{run_id[:8]}.zip"
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Muestras-Tributarios": str(resumen["tributarios"]),
                "X-Muestras-Cedibles": str(resumen["cedibles"]),
                "X-Muestras-Errores": str(resumen["errores"]),
            },
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        logger.exception("Error generando muestras impresas: %s", e)
        raise HTTPException(500, f"Error interno: {e}")
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════
# Cleanup post-certificación — limpia datos ficticios de cert
# ══════════════════════════════════════════════════════════════════


@router.post("/cleanup/{rut}")
def cleanup_certificacion(
    rut: str,
    user=Depends(require_super_admin),
) -> dict:
    """Limpia los datos de certificación después de pasar a producción.

    Borra runs/casos/libros/DTEs/CAFs ficticios de certificacion.db,
    preservando Empresa/Sucursal/Usuario. Marca cert_archivada_at en
    master.db y registra el evento en el log de auditoría.

    Solo ejecutable si la empresa ya está en etapa ``produccion``.
    Idempotente: si ya fue archivada, devuelve 422.
    """
    try:
        result = limpiar_certificacion(
            rut=rut,
            user_id=user.id,
            user_email=user.email if hasattr(user, "email") else None,
        )
        return result
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        logger.exception("Error en cleanup de certificación para %s: %s", rut, e)
        raise HTTPException(500, f"Error interno: {e}")


@router.post("/reiniciar/{rut}")
def reiniciar_certificacion_endpoint(
    rut: str,
    user=Depends(require_super_admin),
) -> dict:
    """Reinicia la certificación preservando los CAFs (folio_actual intacto).

    Borra ``CertificacionRun`` (y sus casos/libros por cascada) y los
    ``DteEmitido`` de la certificación. **Preserva** los ``CafFolio`` con
    ``folio_actual`` intacto: la próxima emisión continúa desde el siguiente
    folio disponible (no retrocede).

    Caso de uso: los N° de Atención del set actual se quemaron (SII bloquea
    el combo FolioNotificacion+Periodo+TipoLibro=ESPECIAL una vez
    recibido) y hay que empezar con un set nuevo. El usuario no debe
    perder el avance de folios — los CAFs nuevos tardan en otorgarse.

    Solo ejecutable si ``etapa != 'produccion'``. Si la empresa ya está en
    producción, usar ``cleanup`` en su lugar.
    """
    try:
        result = reiniciar_certificacion(
            rut=rut,
            user_id=user.id,
            user_email=user.email if hasattr(user, "email") else None,
        )
        return result
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        logger.exception("Error reiniciando certificación para %s: %s", rut, e)
        raise HTTPException(500, f"Error interno: {e}")


# ══════════════════════════════════════════════════════════════════
# Etapa 7 — Intercambio de Información (OPCIONAL)
# ══════════════════════════════════════════════════════════════════
# El SII envía un `ENVIO_DTE.xml` simulando un proveedor. El contribuyente
# debe responder con 3 XMLs firmados. Esta etapa no se pide en todas las
# certificaciones (ej: TRESTRES PUBLICIDAD no la tuvo), por eso el wizard
# la deja como opcional y solo se activa si el usuario sube un XML.


@router.post("/intercambio/{rut}/parsear")
async def parsear_intercambio(
    rut: str,
    file: UploadFile = File(...),
) -> dict:
    """Parsea el ENVIO_DTE.xml del SII y devuelve resumen para preview.

    No toca disco ni la BD. Marca cada DTE como aceptado/rechazado según
    si su RUTRecep coincide con el RUT de la empresa.
    """
    if not file.filename:
        raise HTTPException(400, "Archivo sin nombre")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Archivo vacío")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        empresa = session.query(Empresa).filter(Empresa.rut == rut).first()
        if not empresa:
            raise HTTPException(
                404, f"Empresa {rut} no encontrada en su BD de certificación",
            )
        from crumbpos.api.services.intercambio import ServicioIntercambio
        from crumbpos.config import settings as cfg_settings

        servicio = ServicioIntercambio(empresa, cfg_settings.DATA_DIR)
        try:
            return servicio.parsear_preview(raw, nombre_archivo=file.filename)
        except ValueError as e:
            raise HTTPException(422, f"XML inválido: {e}")
    finally:
        session.close()


@router.post("/intercambio/{rut}/generar")
async def generar_intercambio(
    rut: str,
    file: UploadFile = File(...),
    nombre_contacto: str = Form(""),
    email_contacto: str = Form(""),
    rut_firma: str = Form(""),
) -> Response:
    """Genera los 3 XMLs de respuesta firmados y devuelve un ZIP.

    El ZIP incluye:
      - 0_SobreOriginal.xml      (el XML del SII, para trazabilidad)
      - 1_RecepcionDTE.xml       (acuse de formato)
      - 2_EnvioRecibos.xml       (acuse comercial — opcional)
      - 3_ResultadoDTE.xml       (resultado comercial)

    Los archivos también quedan en disco en
    `data/{rut}/intercambio/{timestamp}/` para trazabilidad.

    El usuario descarga el ZIP, extrae los 3 XMLs y los sube al portal
    del SII en `https://www4.sii.cl/pfeInternet/#subirArchivos` (uno por
    cada uploadFile{1,2,3}).
    """
    import io
    import zipfile

    if not file.filename:
        raise HTTPException(400, "Archivo sin nombre")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Archivo vacío")

    session = get_empresa_db_session(rut, "certificacion")
    try:
        empresa = session.query(Empresa).filter(Empresa.rut == rut).first()
        if not empresa:
            raise HTTPException(
                404, f"Empresa {rut} no encontrada en su BD de certificación",
            )
        if not (empresa.cert_data or (empresa.cert_path and Path(empresa.cert_path).exists())):
            raise HTTPException(
                422,
                f"Empresa {rut} sin certificado .pfx cargado. "
                "Subir desde el wizard antes de generar intercambio.",
            )

        from crumbpos.api.services.intercambio import ServicioIntercambio
        from crumbpos.config import settings as cfg_settings

        servicio = ServicioIntercambio(empresa, cfg_settings.DATA_DIR)
        try:
            resultado = servicio.generar_respuestas(
                raw,
                nombre_archivo=file.filename,
                nombre_contacto=nombre_contacto,
                email_contacto=email_contacto,
                rut_firma=rut_firma or None,
            )
        except ValueError as e:
            raise HTTPException(422, f"XML inválido: {e}")
        except RuntimeError as e:
            raise HTTPException(422, str(e))
        except Exception as e:
            logger.exception("Error generando intercambio para %s: %s", rut, e)
            raise HTTPException(500, f"Error interno: {e}")

        # Armar ZIP en memoria.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("0_SobreOriginal.xml", resultado.sobre.xml_bytes)
            zf.writestr(resultado.recepcion.nombre_archivo, resultado.recepcion.contenido)
            if resultado.recibos is not None:
                zf.writestr(resultado.recibos.nombre_archivo, resultado.recibos.contenido)
            zf.writestr(resultado.resultado.nombre_archivo, resultado.resultado.contenido)
        buf.seek(0)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_name = f"intercambio_{rut}_{timestamp}.zip"
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_name}"',
                # Exponemos metadata para que el frontend pueda mostrar
                # el resumen después de descargar.
                "X-Intercambio-Dir": resultado.directorio_salida,
                "X-Intercambio-Dtes-Total": str(len(resultado.sobre.dtes)),
                "X-Intercambio-Dtes-Aceptados": str(
                    sum(
                        1 for d in resultado.sobre.dtes
                        if d.rut_recep.replace(".", "").upper()
                        == empresa.rut.replace(".", "").upper()
                    )
                ),
                "X-Intercambio-Recibos-Incluidos": (
                    "1" if resultado.recibos is not None else "0"
                ),
            },
        )
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════
# Helpers — emisión real desde el wizard de certificación
# ══════════════════════════════════════════════════════════════════

def _get_servicio_for_certificacion(
    session, rut: str,
) -> tuple[ServicioEmisionDTE, Empresa]:
    """Construye un ServicioEmisionDTE para emitir DTEs desde el wizard.

    A diferencia de `_get_servicio` en facturacion.py, este helper no usa
    TenantContext (el wizard lo ejecuta el super_admin sin tenant). Recibe
    la session ya abierta sobre la BD de certificación de la empresa y
    arma el EmisorConfig leyendo Empresa + cert + CAFs desde esa BD.
    """
    empresa = session.query(Empresa).filter(Empresa.rut == rut).first()
    if not empresa:
        raise HTTPException(
            404, f"Empresa {rut} no encontrada en su BD de certificación",
        )
    if not empresa.fecha_resolucion:
        raise HTTPException(
            422, f"Empresa {rut} sin fecha_resolucion configurada",
        )
    if not empresa.cert_rut_firmante:
        raise HTTPException(
            422, f"Empresa {rut} sin cert_rut_firmante configurado",
        )

    # Certificado: prioridad cert_data (base64 en DB) → cert_path (archivo)
    cert_path = None
    cert_password = None
    if empresa.cert_data:
        pfx_bytes = base64.b64decode(empresa.cert_data)
        fd, tmp_pfx = tempfile.mkstemp(suffix=".pfx")
        os.write(fd, pfx_bytes)
        os.close(fd)
        cert_path = tmp_pfx
        cert_password = empresa.cert_password
    elif empresa.cert_path and Path(empresa.cert_path).exists():
        cert_path = empresa.cert_path
        cert_password = empresa.cert_password
    if not cert_path:
        raise HTTPException(
            422,
            f"Empresa {rut} sin certificado .pfx cargado. "
            "Subir desde el wizard antes de emitir.",
        )

    # CAFs: deben existir en la BD de certificación
    caf_count = session.query(CafFolio).filter(
        CafFolio.empresa_id == empresa.id,
    ).count()
    if caf_count == 0:
        raise HTTPException(
            422,
            f"Empresa {rut} sin CAFs cargados. "
            "Subir CAFs desde el wizard antes de emitir.",
        )
    caf_mgr_db = CAFManagerDB(session, empresa.id)

    config = EmisorConfig(
        rut=rut,
        razon_social=empresa.razon_social,
        giro=empresa.giro,
        acteco=empresa.acteco or 0,
        direccion=empresa.direccion,
        comuna=empresa.comuna,
        ciudad=empresa.ciudad,
        fecha_resolucion=empresa.fecha_resolucion,
        numero_resolucion=empresa.numero_resolucion,
        cert_path=cert_path,
        ambiente=empresa.ambiente_sii,
        cert_password=cert_password,
        rut_firmante=empresa.cert_rut_firmante,
    )
    return ServicioEmisionDTE(config, caf_manager_db=caf_mgr_db), empresa


def _caso_a_factura_request(
    session, caso: CertificacionCaso, empresa: Empresa,
) -> FacturaRequest:
    """Mapea un CertificacionCaso (datos del parser) a FacturaRequest.

    Receptor: el mismo emisor (práctica estándar de certificación SII).
    Las dos certificaciones aprobadas en este repo — TRESTRES PUBLICIDAD
    y GRUPO TRESTRES — usaron este patrón. El SII no rebota el receptor
    en certificación; solo verifica firma y formato del DTE.

    Para NC/ND con referencia a otro caso del mismo set, busca el folio
    real del caso referido en CertificacionCaso (debe estar emitido).

    El campo `caso_set` se completa con "CASO {numero_caso}" para que
    el generador XML agregue automáticamente la Referencia SET al DTE.
    """
    datos = caso.datos or {}

    def _normalizar_items(raw: list[dict]) -> list[dict]:
        """Normaliza la forma de items parseados al shape esperado por FacturaRequest.

        Importante: preserva ``cantidad=None`` (no fuerza a 1). El SET SII
        para NC/ND CodRef=3 (MODIFICA MONTO) puede declarar SOLO el precio
        (VALOR UNITARIO) sin cantidad — la cantidad real se hereda del DTE
        referenciado. El core (``_enriquecer_items_codref3``) distingue
        ``None`` de ``1`` y decide si enriquecer. Si forzáramos ``1`` aquí,
        el core no podría saber si el caller quiso cantidad=1 o no declaró.
        """
        out: list[dict] = []
        for it in raw or []:
            out.append({
                "nombre": it.get("nombre") or "",
                "cantidad": it.get("cantidad"),  # preservar None intencional
                "precio_unitario": it.get("precio_unitario") or 0,
                "unidad_medida": it.get("unidad_medida"),
                "exento": bool(it.get("exento", False)),
                "descuento_pct": it.get("descuento_pct"),
            })
        return out

    def _items_de_caso_referido(numero_caso_ref: str, depth: int = 0) -> list[dict]:
        """Resuelve ítems de un caso referenciado para NC/ND con CodRef=1 (ANULA).
        SII exige replicar los ítems del documento original.
        Si el referenciado es a su vez una NC con CodRef=2 (CORRIGE TEXTO),
        devolvemos el placeholder — transitivo."""
        if depth > 3:  # seguro anti-loop
            return []
        caso_ref_db = session.query(CertificacionCaso).filter(
            CertificacionCaso.run_id == caso.run_id,
            CertificacionCaso.numero_caso == numero_caso_ref,
        ).first()
        if caso_ref_db is None:
            return []
        datos_ref = caso_ref_db.datos or {}
        items_ref = _normalizar_items(datos_ref.get("items") or [])
        if items_ref:
            return items_ref
        # Referenciado sin ítems propios → aplicar la misma lógica
        ref2 = datos_ref.get("referencia") or {}
        cr2 = ref2.get("cod_ref")
        if cr2 == 2:
            return [{
                "nombre": (ref2.get("razon") or "CORRIGE TEXTO").strip(),
                "cantidad": 0,
                "precio_unitario": 0,
                "unidad_medida": None,
                "exento": False,
                "descuento_pct": None,
            }]
        if cr2 == 1 and ref2.get("caso_referido"):
            return _items_de_caso_referido(ref2["caso_referido"], depth + 1)
        return []

    # Items — parseados directos del SET
    items = _normalizar_items(datos.get("items") or [])

    # Referencia: NC/ND apuntan a otro caso del mismo set por numero_caso.
    # Aquí resolvemos a folio real (el caso referido debe estar emitido).
    referencias: list[dict] | None = None
    ref = datos.get("referencia")
    if ref and ref.get("caso_referido"):
        caso_ref = session.query(CertificacionCaso).filter(
            CertificacionCaso.run_id == caso.run_id,
            CertificacionCaso.numero_caso == ref["caso_referido"],
        ).first()
        if caso_ref is None:
            raise HTTPException(
                422,
                f"Caso {caso.numero_caso}: la referencia apunta al caso "
                f"{ref['caso_referido']} pero no existe en la run.",
            )
        if not caso_ref.folio:
            raise HTTPException(
                422,
                f"Caso {caso.numero_caso}: el caso referido "
                f"{ref['caso_referido']} todavía no se emitió. "
                "Emítelo primero para poder referenciarlo.",
            )
        referencias = [{
            "tipo_doc": ref.get("tipo_doc_referido"),
            "folio": caso_ref.folio,
            "razon": ref.get("razon"),
            "codigo": ref.get("cod_ref"),
        }]

    # Síntesis de items cuando el SET no los trae (NC/ND CodRef=1 o 2).
    # SII spec:
    #  - CodRef=2 (CORRIGE TEXTO/GIRO): exactamente 1 ítem placeholder con
    #    NmbItem=razón, cantidad=0, precio_unitario=0 (MontoItem=0).
    #  - CodRef=1 (ANULA doc completo): replicar los ítems del doc original.
    if not items and ref and ref.get("caso_referido"):
        cr = ref.get("cod_ref")
        if cr == 2:
            items = [{
                "nombre": (ref.get("razon") or "CORRIGE TEXTO").strip(),
                "cantidad": 0,
                "precio_unitario": 0,
                "unidad_medida": None,
                "exento": False,
                "descuento_pct": None,
            }]
        elif cr == 1:
            items = _items_de_caso_referido(ref["caso_referido"])

    # NOTA: El enriquecimiento de precios para CodRef=3 (MODIFICA MONTO) se hizo
    # antes aquí, pero se movió al core (``ServicioEmisionDTE._enriquecer_items_codref3``)
    # para cumplir la directriz: un solo core procesa documentos tanto en
    # certificación como en producción. El mapper queda puro: solo traduce el
    # caso SII al FacturaRequest sin efectos laterales ni lookups.
    # Ver: ``emision_dte.py::ServicioEmisionDTE._enriquecer_items_codref3``
    # y tests en ``tests/test_enriquecer_codref3_core.py``.

    if not items:
        raise HTTPException(
            422,
            f"Caso {caso.numero_caso} no tiene items en datos parseados.",
        )

    # Descuento global
    descuentos_globales: list[dict] | None = None
    pct = datos.get("descuento_global_pct")
    if pct:
        descuentos_globales = [{
            "tipo": "D",
            "descripcion": "Descuento global",
            "tipo_valor": "%",
            "valor": pct,
        }]

    # Guía de despacho — normalización defensiva.
    # Regla SII (validada en emision_dte._validar_guia_despacho): TipoDespacho
    # solo aplica con IndTraslado=1 (venta) o IndTraslado=3 (consignación).
    # Traslado interno (5), otros (6), devolución (7), venta por efectuar (2)
    # NO llevan TipoDespacho. Si en caso.datos quedó un TipoDespacho stale
    # (ej: generador de simulación previo al fix 2026-04-23 ponía
    # TipoDespacho=1 en slot 14), lo descartamos aquí para que el reintentar
    # funcione sin necesidad de regenerar preview y descartar DTEs ya
    # emitidos del mismo set.
    ind_traslado_val = datos.get("ind_traslado")
    tipo_despacho_val = datos.get("tipo_despacho")
    if (
        ind_traslado_val is not None
        and ind_traslado_val not in (1, 3)
        and tipo_despacho_val is not None
    ):
        tipo_despacho_val = None

    return FacturaRequest(
        tipo_dte=caso.tipo_dte,
        receptor_rut=empresa.rut,
        receptor_razon=empresa.razon_social,
        receptor_giro=empresa.giro,
        receptor_dir=empresa.direccion,
        receptor_comuna=empresa.comuna,
        receptor_ciudad=empresa.ciudad,
        items=items,
        referencias=referencias,
        descuentos_globales=descuentos_globales,
        ind_traslado=ind_traslado_val,
        tipo_despacho=tipo_despacho_val,
        caso_set=f"CASO {caso.numero_caso}",
    )


# ══════════════════════════════════════════════════════════════════
# Helpers — parseo, persistencia y serialización
# ══════════════════════════════════════════════════════════════════

def _decodificar_set(raw: bytes) -> str:
    """Decodifica bytes del set intentando ISO-8859-1 → UTF-8."""
    try:
        return raw.decode("iso-8859-1")
    except UnicodeDecodeError:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HTTPException(
                400,
                f"No se pudo decodificar el archivo (probar ISO-8859-1 o UTF-8): {e}",
            )


def _buscar_run_activa(session, rut: str) -> CertificacionRun | None:
    """Busca la run activa (no completada ni cancelada) de una empresa."""
    return session.query(CertificacionRun).filter(
        CertificacionRun.rut_empresa == rut,
        CertificacionRun.estado.notin_(("completado", "cancelado")),
    ).order_by(CertificacionRun.created_at.desc()).first()


def _poblar_casos_y_libros(
    session, run: CertificacionRun, parseado: SetParseado,
) -> None:
    """Crea los registros normalizados de casos y libros para una run."""
    for set_nombre, casos in parseado.sets.items():
        for caso in casos:
            session.add(CertificacionCaso(
                run_id=run.id,
                set_nombre=set_nombre,
                numero_caso=caso.numero_caso,
                numero_atencion=caso.numero_atencion,
                tipo_dte=caso.tipo_dte,
                datos=asdict(caso),
            ))

    if parseado.libro_compras:
        session.add(CertificacionLibro(
            run_id=run.id,
            tipo_libro="compras",
            datos={
                "entradas": [asdict(c) for c in parseado.libro_compras],
                "observaciones": parseado.libro_compras_observaciones,
            },
        ))
    if parseado.libro_ventas_instrucciones:
        session.add(CertificacionLibro(
            run_id=run.id,
            tipo_libro="ventas",
            datos={"instrucciones": parseado.libro_ventas_instrucciones},
        ))
    if parseado.libro_guias_instrucciones:
        session.add(CertificacionLibro(
            run_id=run.id,
            tipo_libro="guias",
            datos={"instrucciones": parseado.libro_guias_instrucciones},
        ))


def _serializar_parseado(parseado: SetParseado, filename: str | None = None) -> dict:
    """Convierte el resultado del parser a dict JSON-serializable."""
    resumen_sets: dict = {}
    for nombre in (SET_BASICO, SET_GUIAS, SET_EXENTA):
        casos = parseado.sets.get(nombre, [])
        if not casos:
            continue
        resumen_sets[nombre] = {
            "numero_atencion": casos[0].numero_atencion,
            "total_casos": len(casos),
            "tipos_dte_unicos": sorted({c.tipo_dte for c in casos}),
            "casos": [asdict(c) for c in casos],
        }
    return {
        "filename": filename,
        "rut_emisor": parseado.rut_emisor,
        "sets": resumen_sets,
        "libro_compras": {
            "total": len(parseado.libro_compras),
            "entradas": [asdict(c) for c in parseado.libro_compras],
            "observaciones": parseado.libro_compras_observaciones,
        },
        "libro_ventas_instrucciones": parseado.libro_ventas_instrucciones,
        "libro_guias_instrucciones": parseado.libro_guias_instrucciones,
        "resumen": _construir_resumen(parseado),
    }


def _construir_resumen(parseado: SetParseado) -> dict:
    total_casos = sum(len(c) for c in parseado.sets.values())
    casos_por_tipo: dict[int, int] = {}
    for casos in parseado.sets.values():
        for c in casos:
            casos_por_tipo[c.tipo_dte] = casos_por_tipo.get(c.tipo_dte, 0) + 1
    return {
        "total_casos_dte": total_casos,
        "total_compras": len(parseado.libro_compras),
        "casos_por_tipo": [
            {"tipo": t, "cantidad": n}
            for t, n in sorted(casos_por_tipo.items())
        ],
        "sets_presentes": [
            nombre for nombre in (SET_BASICO, SET_GUIAS, SET_EXENTA)
            if parseado.sets.get(nombre)
        ],
        "tiene_libro_compras": bool(parseado.libro_compras),
    }


def _serializar_run(run: CertificacionRun) -> dict:
    return {
        "id": run.id,
        "rut_empresa": run.rut_empresa,
        "estado": run.estado,
        "screen_actual": run.screen_actual,
        "archivo_nombre": run.archivo_nombre,
        "datos_parseados": run.datos_parseados,
        "datos_setup": run.datos_setup,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "casos": [_serializar_caso(c) for c in run.casos],
        "libros": [_serializar_libro(lib) for lib in run.libros],
    }


def _serializar_caso(caso: CertificacionCaso) -> dict:
    return {
        "id": caso.id,
        "run_id": caso.run_id,
        "set_nombre": caso.set_nombre,
        "numero_caso": caso.numero_caso,
        "numero_atencion": caso.numero_atencion,
        "tipo_dte": caso.tipo_dte,
        "datos": caso.datos,
        "estado": caso.estado,
        "folio": caso.folio,
        "dte_emitido_id": caso.dte_emitido_id,
        "trackid": caso.trackid,
        "estado_sii": caso.estado_sii,
        "error_mensaje": caso.error_mensaje,
        "emitido_at": caso.emitido_at.isoformat() if caso.emitido_at else None,
        "avance_declarado_at": (
            caso.avance_declarado_at.isoformat()
            if caso.avance_declarado_at else None
        ),
        "aprobado_at": (
            caso.aprobado_at.isoformat() if caso.aprobado_at else None
        ),
        "observaciones": caso.observaciones,
        "updated_at": caso.updated_at.isoformat() if caso.updated_at else None,
    }


def _serializar_libro(libro: CertificacionLibro) -> dict:
    return {
        "id": libro.id,
        "run_id": libro.run_id,
        "tipo_libro": libro.tipo_libro,
        "numero_atencion": libro.numero_atencion,
        "datos": libro.datos,
        "xml_libro": libro.xml_libro,
        "estado": libro.estado,
        "trackid": libro.trackid,
        "estado_sii": libro.estado_sii,
        "error_mensaje": libro.error_mensaje,
        "enviado_at": libro.enviado_at.isoformat() if libro.enviado_at else None,
        "avance_declarado_at": (
            libro.avance_declarado_at.isoformat()
            if libro.avance_declarado_at else None
        ),
        "aprobado_at": (
            libro.aprobado_at.isoformat() if libro.aprobado_at else None
        ),
        "observaciones": libro.observaciones,
        "updated_at": libro.updated_at.isoformat() if libro.updated_at else None,
    }


def _rut_from_filename(filename: str) -> str | None:
    """Extrae RUT desde 'SIISetDePruebas77829149-5.txt' → '77829149-5'."""
    import re
    m = re.search(r"SIISetDePruebas(\d+)", filename, re.I)
    if not m:
        return None
    digitos = m.group(1)
    if len(digitos) < 2:
        return None
    return f"{digitos[:-1]}-{digitos[-1]}"
