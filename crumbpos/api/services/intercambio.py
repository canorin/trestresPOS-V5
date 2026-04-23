"""Servicio de Intercambio de Información SII.

Coordina el flujo completo de la etapa de Intercambio:

1. Carga el certificado de la empresa (desde `cert_data` o `cert_path`).
2. Parsea el `ENVIO_DTE.xml` que mandó el SII.
3. Arma y firma los 3 XMLs de respuesta (RecepcionDTE, EnvioRecibos,
   ResultadoDTE) vía `crumbpos.core.intercambio`.
4. Los guarda en disco (`data/{rut}/intercambio/{timestamp}/`) para que
   el super admin tenga trazabilidad y los devuelve al caller.

No envía nada al SII automáticamente: la subida la hace el usuario
manualmente vía `https://www4.sii.cl/pfeInternet/#subirArchivos`, por
eso este servicio solo genera los archivos.

Es OPCIONAL: el SII no pide intercambio en todas las certificaciones
(ej: TRESTRES PUBLICIDAD no lo tuvo). El router solo la activa cuando
el usuario sube un XML.
"""
from __future__ import annotations

import base64
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
from crumbpos.core.intercambio.generador import (
    ContactoIntercambio,
    armar_envio_recibos,
    armar_recepcion_dte,
    armar_resultado_dte,
)
from crumbpos.core.intercambio.parser import (
    DteIntercambio,
    SobreIntercambio,
    parsear_envio_dte_sii,
)
from crumbpos.db.models import Empresa

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# DTOs del servicio
# ═══════════════════════════════════════════════════════════════════

@dataclass
class XmlGenerado:
    """Uno de los 3 XMLs de respuesta."""
    nombre_archivo: str         # "1_RecepcionDTE_<ts>.xml"
    ruta_local: str             # path absoluto donde se guardó
    contenido: bytes            # bytes del XML en ISO-8859-1


@dataclass
class IntercambioResultado:
    """Payload completo: sobre parseado + 3 XMLs generados."""
    sobre: SobreIntercambio
    recepcion: XmlGenerado
    recibos: XmlGenerado | None  # None si todos los DTEs fueron rechazados
    resultado: XmlGenerado
    directorio_salida: str       # data/{rut}/intercambio/{ts}/


# ═══════════════════════════════════════════════════════════════════
# Servicio
# ═══════════════════════════════════════════════════════════════════

class ServicioIntercambio:
    """Orquesta parse + genera + guarda los 3 XMLs."""

    def __init__(self, empresa: Empresa, data_root: str | Path):
        """
        Args:
            empresa: la Empresa de su BD de certificación (tiene cert).
            data_root: raíz de `data/` del proyecto. Los outputs se guardan
                en `<data_root>/<rut>/intercambio/<timestamp>/`.
        """
        self.empresa = empresa
        self.data_root = Path(data_root)

    # ── API pública ────────────────────────────────────────────────

    def parsear_preview(
        self,
        xml_bytes: bytes,
        nombre_archivo: str,
    ) -> dict:
        """Parsea el XML del SII y devuelve un resumen JSON-friendly.

        Útil para la vista previa antes de generar. No toca disco.
        """
        sobre = parsear_envio_dte_sii(xml_bytes, nombre_archivo=nombre_archivo)
        return self._resumen_sobre(sobre)

    def generar_respuestas(
        self,
        xml_bytes: bytes,
        nombre_archivo: str,
        nombre_contacto: str = "",
        email_contacto: str = "",
        rut_firma: str | None = None,
    ) -> IntercambioResultado:
        """Genera y firma los 3 XMLs de respuesta.

        Args:
            xml_bytes: bytes del `ENVIO_DTE.xml` del SII (ISO-8859-1).
            nombre_archivo: nombre original del archivo (va en <NmbEnvio>).
            nombre_contacto: `<NmbContacto>`. Si vacío, se omite del XML.
            email_contacto: `<MailContacto>`. Si vacío, se omite del XML.
            rut_firma: `<RutFirma>` en cada Recibo. Default: el
                `cert_rut_firmante` de la empresa.

        Raises:
            ValueError: XML inválido.
            RuntimeError: certificado no disponible.
        """
        sobre = parsear_envio_dte_sii(xml_bytes, nombre_archivo=nombre_archivo)

        contacto = ContactoIntercambio(
            nombre=nombre_contacto.strip(),
            email=email_contacto.strip(),
            rut_firma=(rut_firma or self.empresa.cert_rut_firmante or self.empresa.rut).strip(),
            rut_responde=self.empresa.rut.strip(),
        )

        private_key_pem, cert_der = self._cargar_cert()

        # Un único TmstFirma consistente para los 3 XMLs.
        tmst = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        timestamp_dir = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = self.data_root / self.empresa.rut / "intercambio" / timestamp_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        # ── 1. RecepcionDTE ─────────────────────────────────────────
        bytes_recepcion = armar_recepcion_dte(
            sobre, contacto, private_key_pem, cert_der, tmst=tmst
        )
        recepcion = self._guardar(out_dir, "1_RecepcionDTE.xml", bytes_recepcion)

        # ── 2. EnvioRecibos (solo si hay DTEs aceptados) ───────────
        try:
            bytes_recibos = armar_envio_recibos(
                sobre, contacto, private_key_pem, cert_der, tmst=tmst
            )
            recibos: XmlGenerado | None = self._guardar(
                out_dir, "2_EnvioRecibos.xml", bytes_recibos
            )
        except ValueError as e:
            # Caso raro: todos los DTEs son para otro RUT. RecepcionDTE
            # y ResultadoDTE se generan igual, pero no hay Recibos que
            # firmar.
            logger.warning(
                "Sin recibos para intercambio de %s: %s",
                self.empresa.rut, e,
            )
            recibos = None

        # ── 3. ResultadoDTE ─────────────────────────────────────────
        bytes_resultado = armar_resultado_dte(
            sobre, contacto, private_key_pem, cert_der, tmst=tmst
        )
        resultado = self._guardar(out_dir, "3_ResultadoDTE.xml", bytes_resultado)

        # Copia del sobre original para trazabilidad.
        sobre_original = out_dir / "0_SobreOriginal.xml"
        sobre_original.write_bytes(sobre.xml_bytes)

        return IntercambioResultado(
            sobre=sobre,
            recepcion=recepcion,
            recibos=recibos,
            resultado=resultado,
            directorio_salida=str(out_dir),
        )

    # ── Helpers ────────────────────────────────────────────────────

    def _cargar_cert(self) -> tuple[bytes, bytes]:
        """Retorna (private_key_pem, cert_der) de la empresa.

        Prioridad: `cert_data` (base64 en BD) → `cert_path` (archivo).
        """
        cert_path, limpiar_tmp = self._resolver_cert_path()
        try:
            private_key, _, cert_der = cargar_certificado_pfx(
                cert_path, self.empresa.cert_password,
            )
            return private_key, cert_der
        finally:
            if limpiar_tmp and os.path.exists(cert_path):
                os.unlink(cert_path)

    def _resolver_cert_path(self) -> tuple[str, bool]:
        """Resuelve ruta al .pfx; si viene de cert_data lo escribe en tmp.

        Retorna (cert_path, hay_que_limpiar_tmp).
        """
        if self.empresa.cert_data:
            pfx_bytes = base64.b64decode(self.empresa.cert_data)
            fd, tmp_pfx = tempfile.mkstemp(suffix=".pfx")
            os.write(fd, pfx_bytes)
            os.close(fd)
            return tmp_pfx, True

        if self.empresa.cert_path and Path(self.empresa.cert_path).exists():
            return self.empresa.cert_path, False

        raise RuntimeError(
            f"Empresa {self.empresa.rut} sin certificado .pfx cargado. "
            "Subir desde el wizard antes de generar intercambio."
        )

    def _guardar(self, out_dir: Path, nombre: str, contenido: bytes) -> XmlGenerado:
        ruta = out_dir / nombre
        ruta.write_bytes(contenido)
        return XmlGenerado(
            nombre_archivo=nombre,
            ruta_local=str(ruta),
            contenido=contenido,
        )

    def _resumen_sobre(self, sobre: SobreIntercambio) -> dict:
        """Estructura JSON-friendly para la vista previa del wizard."""
        rut_mio = self.empresa.rut.replace(".", "").strip().upper()
        return {
            "set_id": sobre.set_id,
            "nombre_archivo": sobre.nombre_archivo,
            "rut_emisor": sobre.rut_emisor,
            "rut_envia": sobre.rut_envia,
            "rut_receptor": sobre.rut_receptor,
            "tmst_firma_env": sobre.tmst_firma_env,
            "digest_sha1_b64": sobre.digest_sha1_b64,
            "dtes": [_dte_dict(d, rut_mio) for d in sobre.dtes],
        }


def _dte_dict(dte: DteIntercambio, rut_mio: str) -> dict:
    aceptado = dte.rut_recep.replace(".", "").strip().upper() == rut_mio
    return {
        "tipo_dte": dte.tipo_dte,
        "folio": dte.folio,
        "fch_emis": dte.fch_emis,
        "rut_emisor": dte.rut_emisor,
        "rut_recep": dte.rut_recep,
        "mnt_total": dte.mnt_total,
        "aceptado": aceptado,
        "motivo": "Recep OK" if aceptado else "RUT Receptor no corresponde",
    }
