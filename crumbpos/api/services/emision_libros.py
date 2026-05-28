"""Servicio de generación y envío de Libros de Compras y Ventas (IECV).

Sigue el mismo patrón de firma que emision_dte.py:
- Carga certificado con facturacion_electronica.firma.Firma
- Firma con type="libro"
- Envía al SII con enviar_dte (mismo endpoint DTEUpload SOAP).

**Endpoints SII para libros:**
Los libros IECV y LibroGuia se envían al MISMO endpoint que los DTEs normales:
- Cert:  https://maullin.sii.cl/cgi_dte/UPL/DTEUpload
- Prod:  https://palena.sii.cl/cgi_dte/UPL/DTEUpload

`enviar_dte()` con `es_boleta=False` (default) resuelve `settings.get_sii_url("upload", ...)`,
que apunta a esos hosts. No hay endpoint separado para libros — la diferenciación
la hace el SII inspeccionando el tag root del XML enviado (LibroCompraVenta vs LibroGuia
vs EnvioDTE).
"""
import json
import logging
import base64
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from crumbpos.db.models import Empresa, DteEmitido, LibroGenerado
from crumbpos.core.libros.generador_iecv import generar_libro_ventas, generar_libro_compras, generar_libro_guias
from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
from crumbpos.core.sii_client.autenticacion import obtener_token
from crumbpos.core.sii_client.envio import enviar_dte

# Same Firma library used in emision_dte.py
from facturacion_electronica.firma import Firma

logger = logging.getLogger(__name__)


class ServicioLibros:
    """Genera, firma y envía Libros de Compras y Ventas al SII."""

    def __init__(
        self,
        empresa: Empresa,
        cert_path: str,
        cert_password: str | None = None,
    ):
        self.empresa = empresa
        self.cert_path = cert_path
        self.cert_password = cert_password
        self._firma = None
        self._private_key = None
        self._cert_der = None
        self._token = None
        self._token_time = None

    def _cargar_firma(self):
        """Carga firma -- mismo método de emision_dte.py."""
        if self._firma is not None:
            return
        pfx_data = open(self.cert_path, "rb").read()
        rut_firmante = self.empresa.cert_rut_firmante or self.empresa.rut
        self._firma = Firma({
            "string_firma": base64.b64encode(pfx_data).decode(),
            "string_password": self.cert_password or "",
            "init_signature": True,
            "rut_firmante": rut_firmante,
        })
        if not self._firma.firma_electronica:
            raise RuntimeError(f"Error cargando certificado: {self._firma.errores}")
        self._firma.verify = False

        # Also load with our method for SII token
        self._private_key, _, self._cert_der = cargar_certificado_pfx(
            self.cert_path,
            self.cert_password,
        )

    def _obtener_token(self) -> str:
        self._cargar_firma()
        now = datetime.now()
        if self._token and self._token_time and (now - self._token_time).total_seconds() < 1800:
            return self._token
        self._token = obtener_token(
            self._private_key, self._cert_der, self.empresa.ambiente_sii
        )
        self._token_time = now
        return self._token

    def generar_libro_ventas(
        self,
        db: Session,
        periodo: str,
        folio_notificacion: int = 0,
        enviar: bool = True,
        folios_filter: dict[str, list[int]] | None = None,
    ) -> dict:
        """Genera, firma y opcionalmente envía un Libro de Ventas.

        Args:
            db: SQLAlchemy session
            periodo: "YYYY-MM"
            folio_notificacion: 0 for production, >0 for certification
            enviar: whether to send to SII

        Returns:
            dict with keys: ok, libro_id, track_id, error, resumen
        """
        try:
            self._cargar_firma()
            rut_envia = self.empresa.cert_rut_firmante or self.empresa.rut

            # Query DTEs for this empresa and periodo
            # Libro de Ventas: solo T33, T34, T56, T61 (NO boletas T39/T41, NO guías T52)
            año, mes = periodo.split("-")
            tipos_venta = [33, 34, 56, 61]
            dtes = (
                db.query(DteEmitido)
                .filter(
                    DteEmitido.empresa_id == self.empresa.id,
                    DteEmitido.tipo_dte.in_(tipos_venta),
                    DteEmitido.fecha_emision >= f"{año}-{mes}-01",
                    DteEmitido.fecha_emision < _next_month(año, mes),
                )
                .order_by(DteEmitido.tipo_dte, DteEmitido.folio)
                .all()
            )

            # Filtro opcional por folios específicos (certificación)
            if folios_filter:
                allowed = set()
                for tipo_str, folio_list in folios_filter.items():
                    for f in folio_list:
                        allowed.add((int(tipo_str), f))
                dtes = [d for d in dtes if (d.tipo_dte, d.folio) in allowed]

            if not dtes:
                return {
                    "ok": False,
                    "error": f"No hay DTEs emitidos para el periodo {periodo}",
                }

            # Generate XML
            xml_str, libro_id = generar_libro_ventas(
                dtes=dtes,
                empresa=self.empresa,
                periodo=periodo,
                rut_envia=rut_envia,
                folio_notificacion=folio_notificacion,
            )

            # Sign with type="libro"
            signed = self._firma.firmar(xml_str, libro_id, type="libro")
            if not signed:
                return {
                    "ok": False,
                    "error": f"Error firmando libro de ventas: {self._firma.errores}",
                }

            # Add XML declaration
            xml_final = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed
            xml_bytes = xml_final.encode("ISO-8859-1")
            # FIX 2026-05-28 — eliminado debug write a /tmp (libro ventas)
            # Historial: este bloque escribía el XML a /tmp/ultimo_libro_ventas_{rut}_{periodo}.xml
            #            en cada generación. En producción escribe sin límite en disco del servidor.
            # Solución: eliminado. El logging centralizado cubre el diagnóstico.

            # Build resumen for DB storage
            resumen = _build_resumen(dtes)

            track_id = None
            estado_sii = "generado"

            # Send to SII
            if enviar:
                try:
                    token = self._obtener_token()
                    resultado_sii = enviar_dte(
                        xml_bytes=xml_bytes,
                        token=token,
                        rut_emisor=self.empresa.rut,
                        ambiente=self.empresa.ambiente_sii,
                        rut_envia=rut_envia,
                    )
                    track_id = resultado_sii.get("track_id")
                    logger.info(
                        "SII libro ventas: status=%s, track_id=%s",
                        resultado_sii.get("status"),
                        track_id,
                    )
                    if resultado_sii.get("status") == "OK" or track_id:
                        estado_sii = "enviado"
                    else:
                        estado_sii = "error_envio"
                        logger.warning(
                            "Error enviando libro ventas: %s",
                            resultado_sii.get("glosa", ""),
                        )
                except Exception as exc:
                    logger.error("Error enviando libro ventas al SII: %s", exc, exc_info=True)
                    estado_sii = "error_envio"

            # Check for existing record to update or create new
            existing = (
                db.query(LibroGenerado)
                .filter(
                    LibroGenerado.empresa_id == self.empresa.id,
                    LibroGenerado.tipo_libro == "VENTA",
                    LibroGenerado.periodo == periodo,
                )
                .first()
            )

            xml_b64 = base64.b64encode(xml_bytes).decode("ascii")

            if existing:
                existing.xml_firmado = xml_b64
                existing.track_id = track_id
                existing.estado_sii = estado_sii
                existing.resumen_json = json.dumps(resumen, ensure_ascii=False)
                existing.created_at = datetime.now(timezone.utc)
                libro_record = existing
            else:
                libro_record = LibroGenerado(
                    empresa_id=self.empresa.id,
                    tipo_libro="VENTA",
                    periodo=periodo,
                    xml_firmado=xml_b64,
                    track_id=track_id,
                    estado_sii=estado_sii,
                    resumen_json=json.dumps(resumen, ensure_ascii=False),
                )
                db.add(libro_record)

            db.commit()

            return {
                "ok": True,
                "libro_id": libro_id,
                "track_id": track_id,
                "estado_sii": estado_sii,
                "resumen": resumen,
                "total_dtes": len(dtes),
            }

        except Exception as e:
            import traceback
            logger.error("Error generando libro ventas: %s", e, exc_info=True)
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {str(e)}",
            }


    def generar_libro_guias(
        self,
        db: Session,
        periodo: str,
        folio_notificacion: int,
        enviar: bool = True,
        guias_anuladas: list[int] | None = None,
        folios_filter: list[int] | None = None,
    ) -> dict:
        """Genera, firma y opcionalmente envía un Libro de Guías de Despacho.

        Args:
            db: SQLAlchemy session
            periodo: "YYYY-MM"
            folio_notificacion: Número de atención del SII (obligatorio, > 0).
                LibroGuia_v10.xsd solo acepta TipoLibro='ESPECIAL'.
                Obtenerlo en https://zeus.sii.cl/AUT2/AS/accAut.html.
            enviar: whether to send to SII

        Returns:
            dict with keys: ok, libro_id, track_id, error, resumen
        """
        try:
            self._cargar_firma()
            rut_envia = self.empresa.cert_rut_firmante or self.empresa.rut

            # Query Guías de Despacho (tipo_dte=52) for this empresa and periodo
            año, mes = periodo.split("-")
            dtes = (
                db.query(DteEmitido)
                .filter(
                    DteEmitido.empresa_id == self.empresa.id,
                    DteEmitido.tipo_dte == 52,
                    DteEmitido.fecha_emision >= f"{año}-{mes}-01",
                    DteEmitido.fecha_emision < _next_month(año, mes),
                )
                .order_by(DteEmitido.folio)
                .all()
            )

            # Filtro opcional por folios específicos (certificación)
            if folios_filter:
                allowed = set(folios_filter)
                dtes = [d for d in dtes if d.folio in allowed]

            if not dtes:
                return {
                    "ok": False,
                    "error": f"No hay Guías de Despacho para el periodo {periodo}",
                }

            # Pasar guias_anuladas como parámetro al generador, NO mutar la BD.
            # Mutar `dte.anulado=True` aquí contaminaba la base productiva si
            # el caller pasaba `guias_anuladas` por error (ej. caso típico del
            # SET de pruebas SII donde una guía aparece marcada como anulada
            # solo para ese envío específico).
            folios_anulados_set = set(guias_anuladas) if guias_anuladas else None

            # Generate XML (LibroGuia, NOT LibroCompraVenta)
            xml_str, libro_id = generar_libro_guias(
                dtes=dtes,
                empresa=self.empresa,
                periodo=periodo,
                rut_envia=rut_envia,
                folio_notificacion=folio_notificacion,
                folios_anulados=folios_anulados_set,
            )

            # Sign with type="libro"
            signed = self._firma.firmar(xml_str, libro_id, type="libro")
            if not signed:
                return {
                    "ok": False,
                    "error": f"Error firmando libro de guías: {self._firma.errores}",
                }

            # Add XML declaration
            xml_final = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed
            xml_bytes = xml_final.encode("ISO-8859-1")
            # FIX 2026-05-28 — eliminado debug write a /tmp (libro guías)
            # Ver comentario en libro ventas (mismo fix aplicado a los 3 tipos de libro).

            # Build resumen for DB storage
            resumen = _build_resumen(dtes)

            track_id = None
            estado_sii = "generado"

            # Send to SII
            if enviar:
                try:
                    token = self._obtener_token()
                    resultado_sii = enviar_dte(
                        xml_bytes=xml_bytes,
                        token=token,
                        rut_emisor=self.empresa.rut,
                        ambiente=self.empresa.ambiente_sii,
                        rut_envia=rut_envia,
                    )
                    track_id = resultado_sii.get("track_id")
                    logger.info(
                        "SII libro guías: status=%s, track_id=%s",
                        resultado_sii.get("status"),
                        track_id,
                    )
                    if resultado_sii.get("status") == "OK" or track_id:
                        estado_sii = "enviado"
                    else:
                        estado_sii = "error_envio"
                        logger.warning(
                            "Error enviando libro guías: %s",
                            resultado_sii.get("glosa", ""),
                        )
                except Exception as exc:
                    logger.error("Error enviando libro guías al SII: %s", exc, exc_info=True)
                    estado_sii = "error_envio"

            # Check for existing record to update or create new
            existing = (
                db.query(LibroGenerado)
                .filter(
                    LibroGenerado.empresa_id == self.empresa.id,
                    LibroGenerado.tipo_libro == "GUIA",
                    LibroGenerado.periodo == periodo,
                )
                .first()
            )

            xml_b64 = base64.b64encode(xml_bytes).decode("ascii")

            if existing:
                existing.xml_firmado = xml_b64
                existing.track_id = track_id
                existing.estado_sii = estado_sii
                existing.resumen_json = json.dumps(resumen, ensure_ascii=False)
                existing.created_at = datetime.now(timezone.utc)
                libro_record = existing
            else:
                libro_record = LibroGenerado(
                    empresa_id=self.empresa.id,
                    tipo_libro="GUIA",
                    periodo=periodo,
                    xml_firmado=xml_b64,
                    track_id=track_id,
                    estado_sii=estado_sii,
                    resumen_json=json.dumps(resumen, ensure_ascii=False),
                )
                db.add(libro_record)

            db.commit()

            return {
                "ok": True,
                "libro_id": libro_id,
                "track_id": track_id,
                "estado_sii": estado_sii,
                "resumen": resumen,
                "total_dtes": len(dtes),
            }

        except Exception as e:
            logger.error("Error generando libro guías: %s", e, exc_info=True)
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {str(e)}",
            }


    def generar_libro_compras(
        self,
        db: Session,
        periodo: str,
        entradas: list[dict],
        folio_notificacion: int = 0,
        enviar: bool = True,
    ) -> dict:
        """Genera, firma y opcionalmente envía un Libro de Compras.

        Args:
            db: SQLAlchemy session
            periodo: "YYYY-MM"
            entradas: list of dicts with purchase data (TpoDoc, NroDoc, FchDoc, etc.)
            folio_notificacion: 0 for production, >0 for certification (ESPECIAL)
            enviar: whether to send to SII

        Returns:
            dict with keys: ok, libro_id, track_id, error, resumen
        """
        try:
            self._cargar_firma()
            rut_envia = self.empresa.cert_rut_firmante or self.empresa.rut

            xml_str, libro_id = generar_libro_compras(
                dtes=entradas,
                empresa=self.empresa,
                periodo=periodo,
                rut_envia=rut_envia,
                folio_notificacion=folio_notificacion,
            )

            signed = self._firma.firmar(xml_str, libro_id, type="libro")
            if not signed:
                return {"ok": False, "error": f"Error firmando libro de compras: {self._firma.errores}"}

            xml_final = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed
            xml_bytes = xml_final.encode("ISO-8859-1")
            # FIX 2026-05-28 — eliminado debug write a /tmp (libro compras)
            # Ver comentario en libro ventas (mismo fix aplicado a los 3 tipos de libro).

            track_id = None
            estado_sii = "generado"

            if enviar:
                try:
                    token = self._obtener_token()
                    resultado_sii = enviar_dte(
                        xml_bytes=xml_bytes,
                        token=token,
                        rut_emisor=self.empresa.rut,
                        ambiente=self.empresa.ambiente_sii,
                        rut_envia=rut_envia,
                    )
                    track_id = resultado_sii.get("track_id")
                    if resultado_sii.get("status") == "OK" or track_id:
                        estado_sii = "enviado"
                    else:
                        estado_sii = "error_envio"
                except Exception as exc:
                    logger.error("Error enviando libro compras al SII: %s", exc, exc_info=True)
                    estado_sii = "error_envio"

            existing = (
                db.query(LibroGenerado)
                .filter(
                    LibroGenerado.empresa_id == self.empresa.id,
                    LibroGenerado.tipo_libro == "COMPRA",
                    LibroGenerado.periodo == periodo,
                )
                .first()
            )

            xml_b64 = base64.b64encode(xml_bytes).decode("ascii")
            resumen = {"total_documentos": len(entradas), "entradas": entradas}

            if existing:
                existing.xml_firmado = xml_b64
                existing.track_id = track_id
                existing.estado_sii = estado_sii
                existing.resumen_json = json.dumps(resumen, ensure_ascii=False)
                existing.created_at = datetime.now(timezone.utc)
            else:
                db.add(LibroGenerado(
                    empresa_id=self.empresa.id,
                    tipo_libro="COMPRA",
                    periodo=periodo,
                    xml_firmado=xml_b64,
                    track_id=track_id,
                    estado_sii=estado_sii,
                    resumen_json=json.dumps(resumen, ensure_ascii=False),
                ))

            db.commit()

            return {
                "ok": True,
                "libro_id": libro_id,
                "track_id": track_id,
                "estado_sii": estado_sii,
                "resumen": resumen,
                "total_dtes": len(entradas),
            }

        except Exception as e:
            logger.error("Error generando libro compras: %s", e, exc_info=True)
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)}"}


def _next_month(año: str, mes: str) -> str:
    """Returns the first day of the next month as YYYY-MM-DD string."""
    y = int(año)
    m = int(mes)
    if m == 12:
        return f"{y + 1}-01-01"
    return f"{y}-{m + 1:02d}-01"


def _build_resumen(dtes: list) -> dict:
    """Build a JSON-serializable summary of DTEs for storage."""
    por_tipo = {}
    for dte in dtes:
        tpo = dte.tipo_dte
        if tpo not in por_tipo:
            por_tipo[tpo] = {
                "tipo_dte": tpo,
                "cantidad": 0,
                "monto_exento": 0,
                "monto_neto": 0,
                "iva": 0,
                "monto_total": 0,
            }
        r = por_tipo[tpo]
        r["cantidad"] += 1
        r["monto_exento"] += dte.monto_exento or 0
        r["monto_neto"] += dte.monto_neto or 0
        r["iva"] += dte.iva or 0
        r["monto_total"] += dte.monto_total or 0

    return {
        "total_documentos": len(dtes),
        "por_tipo": list(por_tipo.values()),
    }
