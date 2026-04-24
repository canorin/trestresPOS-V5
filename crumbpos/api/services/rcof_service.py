"""Servicio de generacion y envio del RCOF (Reporte de Consumo de Folios).

El RCOF (ahora Registro de Ventas Diario / RVD) reporta diariamente
al SII los folios de boletas consumidos.

Segun el instructivo SII: "No hay cambios en el envio de RCOF" — se envia
por el endpoint SOAP tradicional de DTE upload (maullin/palena), NO por
la REST API de boletas. Usa token SOAP y el mismo mecanismo que EnvioDTE.

La firma del XML si usa type="consu" (consumo de folios) via Firma library.
"""
import json
import logging
import time
import base64
import re
import requests
from datetime import datetime, date
from pathlib import Path

from cryptography.hazmat.primitives.serialization import (
    pkcs12, Encoding, PrivateFormat, NoEncryption,
)
from sqlalchemy.orm import Session

from crumbpos.db.models import Empresa, DteEmitido, RcofDiario
from crumbpos.core.rcof.generador_rcof import generar_rcof
from crumbpos.core.sii_client.autenticacion import obtener_token
from crumbpos.config.settings import get_sii_url

# Same Firma library used across the project
from facturacion_electronica.firma import Firma

logger = logging.getLogger(__name__)


class ServicioRCOF:
    """Genera, firma y envia el RCOF al SII."""

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
        self._token = None
        self._token_time = None

    def _cargar_firma(self):
        """Carga firma -- mismo metodo que emision_libros.py."""
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

    def _obtener_token_soap(self) -> str:
        """Obtiene token SOAP tradicional para envio de RCOF al SII.

        El RCOF se envia por el mismo endpoint que DTEs (maullin/palena),
        por lo que requiere el token SOAP, no el REST de boletas.
        """
        now = datetime.now()
        if self._token and self._token_time and (now - self._token_time).seconds < 1800:
            return self._token

        pfx_data = open(self.cert_path, "rb").read()
        password = self.cert_password.encode() if self.cert_password else None
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            pfx_data, password,
        )
        if not private_key or not certificate:
            raise RuntimeError("Certificado no contiene llave privada o certificado")

        pk_pem = private_key.private_bytes(
            Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption(),
        )
        cert_der = certificate.public_bytes(Encoding.DER)

        self._token = obtener_token(pk_pem, cert_der, self.empresa.ambiente_sii)
        self._token_time = now
        return self._token

    def generar_rcof_diario(
        self,
        db: Session,
        fecha: date,
        enviar: bool = True,
    ) -> dict:
        """Genera, firma y opcionalmente envia el RCOF para una fecha.

        Args:
            db: SQLAlchemy session
            fecha: Fecha del reporte (date object)
            enviar: Si True, envia al SII

        Returns:
            dict con keys: ok, rcof_id, track_id, estado_sii, error, resumen
        """
        try:
            fecha_str = fecha.isoformat()
            self._cargar_firma()
            rut_envia = self.empresa.cert_rut_firmante or self.empresa.rut

            # 1. Consultar boletas del dia
            boletas = (
                db.query(DteEmitido)
                .filter(
                    DteEmitido.empresa_id == self.empresa.id,
                    DteEmitido.tipo_dte.in_([39, 41]),
                    DteEmitido.fecha_emision == fecha,
                )
                .order_by(DteEmitido.tipo_dte, DteEmitido.folio)
                .all()
            )

            if not boletas:
                return {
                    "ok": True,
                    "rcof_id": None,
                    "mensaje": f"Sin boletas para {fecha_str}, RCOF no requerido",
                }

            # 2. Calcular secuencia de envio
            sec_envio = self._calcular_sec_envio(db, fecha)

            # 3. Generar XML
            xml_str, rcof_id = generar_rcof(
                rut_emisor=self.empresa.rut,
                rut_envia=rut_envia,
                fecha_resolucion=self.empresa.fecha_resolucion,
                numero_resolucion=self.empresa.numero_resolucion,
                fecha=fecha_str,
                boletas=boletas,
                sec_envio=sec_envio,
            )

            # 4. Firmar con type="consu"
            signed = self._firma.firmar(xml_str, rcof_id, type="consu")
            if not signed:
                # Fallback: intentar con type="libro_boleta"
                logger.warning(
                    "Firma con type='consu' fallo (%s), reintentando con 'libro_boleta'",
                    self._firma.errores,
                )
                signed = self._firma.firmar(xml_str, rcof_id, type="libro_boleta")
                if not signed:
                    return {
                        "ok": False,
                        "error": f"Error firmando RCOF: {self._firma.errores}",
                    }

            # 5. Preparar XML final
            xml_final = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed
            xml_bytes = xml_final.encode("ISO-8859-1")

            # Debug: guardar en /tmp
            Path("/tmp/ultimo_rcof.xml").write_bytes(xml_bytes)
            logger.info("RCOF XML guardado en /tmp/ultimo_rcof.xml")

            # 6. Construir resumen para DB
            resumen = self._build_resumen(boletas)

            track_id = None
            estado_sii = "generado"

            # 7. Enviar al SII
            if enviar:
                try:
                    resultado_sii = self._enviar_consumo(xml_bytes)
                    track_id = resultado_sii.get("track_id")
                    logger.info(
                        "SII RCOF: status=%s, track_id=%s",
                        resultado_sii.get("status"),
                        track_id,
                    )
                    if resultado_sii.get("status") == "OK" or track_id:
                        estado_sii = "enviado"
                    else:
                        estado_sii = "error_envio"
                        logger.warning(
                            "Error enviando RCOF: %s",
                            resultado_sii.get("glosa", resultado_sii.get("raw", "")),
                        )
                except Exception as exc:
                    logger.error("Error enviando RCOF al SII: %s", exc, exc_info=True)
                    estado_sii = "error_envio"

            # 8. Guardar en RcofDiario
            # Usar la primera sucursal de las boletas como sucursal_id
            sucursal_id = boletas[0].sucursal_id or ""
            xml_b64 = base64.b64encode(xml_bytes).decode("ascii")

            existing = (
                db.query(RcofDiario)
                .filter(
                    RcofDiario.empresa_id == self.empresa.id,
                    RcofDiario.sucursal_id == sucursal_id,
                    RcofDiario.fecha == fecha,
                )
                .first()
            )

            if existing:
                existing.xml_firmado = xml_b64
                existing.track_id = track_id
                existing.estado_sii = estado_sii
                existing.resumen = resumen
                existing.created_at = datetime.utcnow()
            else:
                rcof_record = RcofDiario(
                    empresa_id=self.empresa.id,
                    sucursal_id=sucursal_id,
                    fecha=fecha,
                    xml_firmado=xml_b64,
                    track_id=track_id,
                    estado_sii=estado_sii,
                    resumen=resumen,
                )
                db.add(rcof_record)

            db.commit()

            return {
                "ok": True,
                "rcof_id": rcof_id,
                "track_id": track_id,
                "estado_sii": estado_sii,
                "resumen": resumen,
                "total_boletas": len(boletas),
            }

        except Exception as e:
            logger.error("Error generando RCOF: %s", e, exc_info=True)
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {str(e)}",
            }

    def _enviar_consumo(self, xml_bytes: bytes) -> dict:
        """Envia RCOF al SII via upload SOAP tradicional (DTEUpload).

        El RCOF/RVD se envia por el mismo mecanismo que los DTEs:
        - Endpoint: maullin (cert) / palena (prod) via DTEUpload
        - Token: SOAP tradicional (no REST boleta)

        Segun instructivo SII: "No hay cambios en el envio de RCOF".

        Args:
            xml_bytes: XML del RCOF firmado como bytes

        Returns:
            dict con status, track_id, etc.
        """
        token = self._obtener_token_soap()
        rut_envia = self.empresa.cert_rut_firmante or self.empresa.rut
        sender_num, sender_dv = rut_envia.split("-")
        company_num, company_dv = self.empresa.rut.split("-")

        url = get_sii_url("upload", self.empresa.ambiente_sii)

        headers = {
            "Cookie": f"TOKEN={token}",
            "User-Agent": "Mozilla/4.0 (compatible; PROG 1.0; CrumbPOS)",
        }

        files = {
            "rutSender": (None, sender_num),
            "dvSender": (None, sender_dv),
            "rutCompany": (None, company_num),
            "dvCompany": (None, company_dv),
            "archivo": ("rcof.xml", xml_bytes, "text/xml"),
        }

        # Reintentos por conexiones inestables del SII
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = requests.post(url, files=files, headers=headers, timeout=90)
                response.raise_for_status()
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    requests.exceptions.HTTPError) as e:
                if attempt < max_retries - 1:
                    wait = 10 * (attempt + 1)
                    logger.warning(
                        "Reintento %d/%d en %ds... (%s)",
                        attempt + 2, max_retries, wait, e.__class__.__name__,
                    )
                    time.sleep(wait)
                else:
                    raise

        text = response.text

        # Respuesta es XML de RECEPCIONDTE
        track_id = None
        xml_track = re.search(r'<TRACKID>(\d+)</TRACKID>', text, re.IGNORECASE)
        if xml_track:
            track_id = xml_track.group(1)
        xml_status = re.search(r'<STATUS>(\d+)</STATUS>', text, re.IGNORECASE)
        status_code = xml_status.group(1) if xml_status else None

        glosa = ""
        glosa_match = re.search(r'<GLOSA>([^<]+)</GLOSA>', text, re.IGNORECASE)
        if glosa_match:
            glosa = glosa_match.group(1).strip()

        return {
            "status": "OK" if (status_code == "0" or track_id) else "ERROR",
            "track_id": track_id,
            "glosa": glosa,
            "raw": text,
        }

    def _calcular_sec_envio(self, db: Session, fecha: date) -> int:
        """Calcula el numero de secuencia de envio para el dia.

        Incrementa en 1 por cada reenvio del mismo dia.
        """
        count = (
            db.query(RcofDiario)
            .filter(
                RcofDiario.empresa_id == self.empresa.id,
                RcofDiario.fecha == fecha,
            )
            .count()
        )
        return count + 1

    @staticmethod
    def _build_resumen(boletas: list) -> dict:
        """Construye resumen JSON para almacenar en DB."""
        por_tipo = {}
        for b in boletas:
            tpo = b.tipo_dte
            if tpo not in por_tipo:
                por_tipo[tpo] = {
                    "tipo_dte": tpo,
                    "cantidad": 0,
                    "monto_neto": 0,
                    "monto_iva": 0,
                    "monto_exento": 0,
                    "monto_total": 0,
                    "folio_inicial": None,
                    "folio_final": None,
                }
            r = por_tipo[tpo]
            r["cantidad"] += 1
            monto_total = b.monto_total or 0
            monto_exento = b.monto_exento or 0

            if tpo == 41:
                r["monto_exento"] += monto_total
            else:
                monto_afecto = monto_total - monto_exento
                neto = round(monto_afecto / 1.19)
                iva = monto_afecto - neto
                r["monto_neto"] += neto
                r["monto_iva"] += iva
                r["monto_exento"] += monto_exento

            r["monto_total"] += monto_total

            if r["folio_inicial"] is None or b.folio < r["folio_inicial"]:
                r["folio_inicial"] = b.folio
            if r["folio_final"] is None or b.folio > r["folio_final"]:
                r["folio_final"] = b.folio

        return {
            "total_boletas": len(boletas),
            "por_tipo": list(por_tipo.values()),
        }
