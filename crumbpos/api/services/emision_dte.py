"""Servicio de emisión DTE — usa el MISMO flujo de firma aprobado por el SII.

Usa facturacion_electronica.firma.Firma (misma librería de firmar_set.py).
"""
import logging
import re
import base64
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from lxml import etree

from crumbpos.db.models import DteEmitido
from crumbpos.models.dte_models import DTE, ItemDetalle, Referencia, DescuentoGlobal
from crumbpos.core.caf.caf_manager import CAFManager
from crumbpos.core.dte.generador_xml import (
    generar_documento_xml,
    generar_dte_xml,
    generar_envio_dte,
    xml_to_string,
)
from crumbpos.core.firma.timbre import generar_ted
from crumbpos.core.sii_client.autenticacion import obtener_token, obtener_token_boleta
from crumbpos.core.sii_client.envio import enviar_dte, enviar_boleta
from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
from crumbpos.core.impresion import generar_pdf
from crumbpos.core.impresion.base import DTEPrintData

# Misma librería usada en firmar_set.py (aprobada por SII)
from facturacion_electronica.firma import Firma

logger = logging.getLogger(__name__)

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


@dataclass
class EmisorConfig:
    rut: str
    razon_social: str
    giro: str
    acteco: int
    direccion: str
    comuna: str
    ciudad: str
    fecha_resolucion: str
    numero_resolucion: int
    cert_path: str
    cert_password: str | None = None
    rut_firmante: str | None = None
    caf_dir: str | None = None
    # Dirección sucursal (override de la empresa si se especifica)
    sucursal_direccion: str | None = None
    sucursal_comuna: str | None = None
    sucursal_ciudad: str | None = None
    sucursal_sii: str | None = None  # Código sucursal SII


@dataclass
class FacturaRequest:
    tipo_dte: int
    receptor_rut: str
    receptor_razon: str
    receptor_giro: str
    receptor_dir: str
    receptor_comuna: str
    receptor_ciudad: str | None = None
    items: list[dict] = None
    referencias: list[dict] | None = None
    fma_pago: int | None = None  # 1=Contado, 2=Crédito
    fecha_vencimiento: str | None = None  # YYYY-MM-DD
    # OC reference (tipo 801)
    oc_numero: str | None = None
    oc_fecha: str | None = None
    # Descuentos globales
    descuentos_globales: list[dict] | None = None
    # Guía de despacho
    ind_traslado: int | None = None  # 1=op.const.venta, 2=ventas, 5=traslado interno, 6=otros
    tipo_despacho: int | None = None  # 1=por cuenta receptor, 2=emisor a instalaciones del cliente, 3=emisor a otras instalaciones
    # Certificación: caso del set de pruebas (ej: "CASO 4768464-1")
    caso_set: str | None = None


@dataclass
class EmisionResult:
    ok: bool
    folio: int | None = None
    track_id: str | None = None
    xml_firmado: bytes | None = None
    ted_xml: str | None = None
    pdf_bytes: bytes | None = None
    error: str | None = None
    monto_neto: int | None = None
    monto_exento: int | None = None
    iva: int | None = None
    monto_total: int | None = None


def _limpiar_namespaces(xml_str: str) -> str:
    """Elimina xmlns heredados — MISMO código de firmar_set.py."""
    xml_str = re.sub(r'\s+xmlns:xsi="[^"]*"', '', xml_str)
    xml_str = re.sub(r'\s+xsi:schemaLocation="[^"]*"', '', xml_str)
    xml_str = re.sub(r'\s+xmlns=""', '', xml_str)
    xml_str = re.sub(r'\s+xmlns="http://www\.sii\.cl/SiiDte"', '', xml_str)
    return xml_str


class ServicioEmisionDTE:
    """Emite DTEs usando el MISMO flujo de firma aprobado por el SII."""

    def __init__(self, config: EmisorConfig, caf_manager_db=None):
        self.config = config
        self._firma = None
        self._caf_manager = None
        self._caf_manager_db = caf_manager_db  # CAFManagerDB (prioridad si existe)
        self._token = None
        self._token_time = None
        self._private_key = None
        self._cert_der = None

    def _cargar_firma(self):
        """Carga firma con validación completa del certificado.

        Valida ANTES de firmar:
        - Archivo del certificado existe y es legible
        - Password correcta (descifra el PFX)
        - Certificado NO está vencido
        - Alerta si vence en menos de 30 días
        - RUT del certificado coincide con RUT firmante configurado
        """
        if self._firma is not None:
            # Re-validar vigencia cada vez (el cert puede vencer mientras el server corre)
            fe = getattr(self._firma, '_firma_electronica', None)
            if fe and hasattr(fe, 'not_after') and datetime.now() > fe.not_after:
                fecha_venc = fe.not_after.strftime("%Y-%m-%d")
                raise RuntimeError(
                    f"Certificado VENCIDO (expiró el {fecha_venc}). "
                    f"No se puede firmar — el SII rechazará el envío. Renovar certificado."
                )
            return

        # 1. Verificar que el archivo existe
        cert_path = Path(self.config.cert_path)
        if not cert_path.exists():
            raise RuntimeError(f"Certificado no encontrado: {self.config.cert_path}")
        if not cert_path.is_file():
            raise RuntimeError(f"Ruta del certificado no es un archivo: {self.config.cert_path}")

        pfx_data = open(self.config.cert_path, "rb").read()
        self._firma = Firma({
            "string_firma": base64.b64encode(pfx_data).decode(),
            "string_password": self.config.cert_password or "",
            "init_signature": True,
            "rut_firmante": self.config.rut_firmante or self.config.rut,
        })
        if not self._firma.firma_electronica:
            raise RuntimeError(f"Error cargando certificado: {self._firma.errores}")

        # 2. Validar vigencia del certificado
        fe = self._firma._firma_electronica  # SignatureCert
        if hasattr(fe, 'status') and fe.status == 'expired':
            fecha_venc = fe.not_after.strftime("%Y-%m-%d") if hasattr(fe, 'not_after') else "desconocida"
            raise RuntimeError(
                f"Certificado VENCIDO (expiró el {fecha_venc}). "
                f"No se puede firmar con un certificado vencido — el SII rechazará el envío."
            )
        if hasattr(fe, 'not_after'):
            dias_restantes = (fe.not_after - datetime.now()).days
            if dias_restantes < 30:
                logger.warning(
                    "⚠️ Certificado vence en %d días (%s). Renovar pronto.",
                    dias_restantes, fe.not_after.strftime("%Y-%m-%d"),
                )

        # 3. Validar que el RUT del certificado coincide con el firmante configurado
        rut_firmante_config = self.config.rut_firmante or self.config.rut
        if hasattr(fe, 'subject_serial_number') and fe.subject_serial_number:
            rut_cert = fe.subject_serial_number.replace(".", "").upper()
            rut_config = rut_firmante_config.replace(".", "").upper()
            if rut_cert != rut_config:
                logger.warning(
                    "RUT del certificado (%s) no coincide con rut_firmante configurado (%s). "
                    "Verificar configuración.",
                    rut_cert, rut_config,
                )

        self._firma.verify = False

        # También cargar con nuestro método para obtener token SII
        self._private_key, _, self._cert_der = cargar_certificado_pfx(
            self.config.cert_path,
            self.config.cert_password,
        )

    def _cargar_cafs(self):
        if self._caf_manager is None and self.config.caf_dir:
            self._caf_manager = CAFManager(self.config.caf_dir)

    def _obtener_token(self) -> str:
        """Token SOAP para DTEs (facturas, NC, ND, guías)."""
        self._cargar_firma()
        now = datetime.now()
        if self._token and self._token_time and (now - self._token_time).seconds < 1800:
            return self._token
        self._token = obtener_token(self._private_key, self._cert_der)
        self._token_time = now
        return self._token

    def _obtener_token_boleta(self) -> str:
        """Token REST para boletas (T39, T41) — endpoint separado del SII."""
        self._cargar_firma()
        now = datetime.now()
        if not hasattr(self, '_token_boleta'):
            self._token_boleta = None
            self._token_boleta_time = None
        if self._token_boleta and self._token_boleta_time and (now - self._token_boleta_time).seconds < 1800:
            return self._token_boleta
        self._token_boleta = obtener_token_boleta(self._firma)
        self._token_boleta_time = now
        return self._token_boleta

    # ═══════════════════════════════════════════════════════════════
    # VALIDACIONES PRE-EMISIÓN — Se ejecutan ANTES de consumir folio
    # ═══════════════════════════════════════════════════════════════

    TIPOS_DTE_VALIDOS = (33, 34, 39, 41, 52, 56, 61)
    TIPOS_NOMBRES = {
        33: "Factura Electrónica",
        34: "Factura Exenta Electrónica",
        39: "Boleta Electrónica",
        41: "Boleta Exenta Electrónica",
        52: "Guía de Despacho Electrónica",
        56: "Nota de Débito Electrónica",
        61: "Nota de Crédito Electrónica",
    }

    # ══════════════════════════════════════════════════════════════
    # Enriquecimiento CodRef=3 (MODIFICA MONTO)
    # ══════════════════════════════════════════════════════════════
    #
    # Una NC/ND con CodRef=3 "modifica monto" del DTE referenciado. Si el
    # caller construye el request con items {nombre, cantidad} pero sin
    # precio (flujo típico: POS copiando los items del DTE original pero
    # aún no llenó los ajustes, o parser del SET SII que declara items así),
    # el XML sale con MontoItem=0 y el SII rechaza con REF-2-768.
    #
    # Este método enriquece in-place los items leyendo el ``DteEmitido``
    # referenciado desde la misma BD. Vive en el CORE — no en la capa cert
    # o producción — porque es procesamiento de documentos, y hay UN solo
    # lugar que procesa documentos. Cambia el destino del envío SII según
    # ambiente, no la lógica de construcción del DTE.
    #
    # Fuente canonical: tabla ``dte_emitido`` (misma en cert y prod,
    # distinta DB por tenant). El XML firmado persistido como base64 tiene
    # todos los items con precios.

    def _enriquecer_items_codref3(
        self,
        req: FacturaRequest,
        session,
        empresa_id: str,
    ) -> None:
        """Si ``req`` es NC/ND CodRef=3 con items incompletos, los enriquece
        desde el ``DteEmitido`` referenciado. Modifica ``req.items`` in-place.

        El SET SII tiene dos patrones duales para MODIFICA MONTO:

        - **Patrón A**: SET declara CANTIDAD → NC hereda PRECIO del original.
        - **Patrón B**: SET declara VALOR UNITARIO → NC hereda CANTIDAD del
          original.

        En ambos casos, lo que no viene en el request se hereda del DTE
        referenciado matcheando por ``nombre``. Aplica también al
        ``descuento_pct`` cuando el NC no lo trae.

        No hace nada si:
          - ``req.tipo_dte`` no es 56 ni 61.
          - No hay referencia con ``codigo=3``.
          - Todos los items ya tienen ``precio_unitario > 0`` y
            ``cantidad`` definida (no hay nada que enriquecer).

        Levanta ``ValueError`` si:
          - El DTE referenciado no existe en la BD.
          - El DTE referenciado no tiene ``xml_firmado``.
          - Algún item del NC/ND no matchea por nombre con el original.
        """
        # Gate 1: solo NC (61) y ND (56) pueden tener CodRef=3
        if req.tipo_dte not in (56, 61):
            return
        # Gate 2: requiere al menos una referencia con codigo=3
        if not req.referencias:
            return
        refs_codref3 = [
            r for r in req.referencias
            if r.get("codigo") == 3
            and r.get("folio")
            and r.get("tipo_doc") is not None
        ]
        if not refs_codref3:
            return
        # Gate 3: requiere al menos un item con precio faltante O con cantidad
        # no declarada (None). Si los items traen precio>0 Y cantidad definida,
        # el caller es autoridad — no sobrescribimos, y evitamos trip a BD.
        items = req.items or []

        def _necesita_enrich(it: dict) -> bool:
            precio_falta = (it.get("precio_unitario") or 0) <= 0
            cantidad_falta = it.get("cantidad") is None
            return precio_falta or cantidad_falta

        if not any(_necesita_enrich(it) for it in items):
            return

        # En NC/ND la referencia CodRef=3 apunta a UN solo DTE original.
        ref = refs_codref3[0]
        try:
            tipo_ref = int(str(ref["tipo_doc"]))
            folio_ref = int(str(ref["folio"]))
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"NC/ND CodRef=3: referencia con tipo_doc/folio no numérico "
                f"(tipo_doc={ref.get('tipo_doc')}, folio={ref.get('folio')}): {e}"
            )

        # Buscar DteEmitido del tenant (empresa_id + tipo + folio es único)
        dte_ref = session.query(DteEmitido).filter(
            DteEmitido.empresa_id == empresa_id,
            DteEmitido.tipo_dte == tipo_ref,
            DteEmitido.folio == folio_ref,
        ).first()
        if dte_ref is None:
            raise ValueError(
                f"NC/ND CodRef=3: no existe DteEmitido con tipo {tipo_ref} "
                f"folio {folio_ref} para esta empresa. El DTE referenciado "
                f"debe estar emitido antes de emitir la NC/ND que modifica "
                f"su monto."
            )
        if not dte_ref.xml_firmado:
            raise ValueError(
                f"NC/ND CodRef=3: DteEmitido tipo {tipo_ref} folio "
                f"{folio_ref} no tiene xml_firmado guardado; imposible "
                f"extraer items originales para enriquecer."
            )

        # Decodificar XML firmado (persistido como base64)
        try:
            xml_bytes = base64.b64decode(dte_ref.xml_firmado)
        except Exception as e:
            raise ValueError(
                f"NC/ND CodRef=3: xml_firmado de tipo {tipo_ref} folio "
                f"{folio_ref} no es base64 válido: {e}"
            )
        items_ref = self._extraer_items_del_xml_firmado(xml_bytes)

        # Matcheo por nombre (trim). Case-sensitive: los nombres del SII
        # deben venir idénticos al original — si no, es un bug del caller.
        ref_por_nombre = {
            (it.get("nombre") or "").strip(): it for it in items_ref
        }
        for it in items:
            if not _necesita_enrich(it):
                continue  # caller ya provisionó precio y cantidad, no pisar
            nombre = (it.get("nombre") or "").strip()
            it_ref = ref_por_nombre.get(nombre)
            if it_ref is None:
                raise ValueError(
                    f"NC/ND CodRef=3: item '{nombre}' no existe en el DTE "
                    f"referenciado (tipo {tipo_ref} folio {folio_ref}). "
                    f"Los items deben matchear por nombre con los del "
                    f"documento original."
                )
            # Heredar precio solo si el caller no lo trajo.
            if (it.get("precio_unitario") or 0) <= 0:
                it["precio_unitario"] = it_ref.get("precio_unitario") or 0
            # Heredar cantidad solo si el caller no la trajo.
            if it.get("cantidad") is None:
                it["cantidad"] = it_ref.get("cantidad") or 1
            # Si el NC no trajo descuento_pct y el original sí lo tenía,
            # lo heredamos — mantiene la relación proporcional del original.
            if (
                it.get("descuento_pct") is None
                and it_ref.get("descuento_pct") is not None
            ):
                it["descuento_pct"] = it_ref["descuento_pct"]

    def _enriquecer_items_referencia_a_exenta(
        self,
        req: FacturaRequest,
    ) -> None:
        """Si ``req`` es NC/ND que referencia un T34 (Factura Exenta),
        marca todos los items como ``exento=True`` in-place.

        Contexto: un DTE que corrige/modifica una Factura Exenta debe tener
        todos sus ítems exentos (``IndExe=1``). El validador del core ya lo
        exige (``_validar_request``), pero el SET del SII declara los ítems
        sin la marca ``exento`` y el mapper del wizard tampoco la propaga.

        No hace nada si:
          - ``req.tipo_dte`` no es 56 ni 61.
          - No hay referencia con ``tipo_doc=34``.
          - La referencia es CodRef=2 (CORRIGE TEXTO) — usa placeholder con
            ``cantidad=0, precio=0`` que no necesita marca exento.

        Es idempotente: si el item ya viene con ``exento=True``, queda igual.
        Patrón gemelo de ``_enriquecer_items_codref3`` — único core procesa
        documentos tanto en certificación como en producción.
        """
        # Gate 1: solo NC (61) y ND (56) referencian otros DTEs
        if req.tipo_dte not in (56, 61):
            return
        # Gate 2: requiere al menos una referencia a T34
        if not req.referencias:
            return
        refs_a_exenta = [
            r for r in req.referencias
            if str(r.get("tipo_doc", "")).isdigit()
            and int(str(r.get("tipo_doc"))) == 34
        ]
        if not refs_a_exenta:
            return
        # Gate 3: excluir CodRef=2 (CORRIGE TEXTO) — usa placeholder.
        # El validador también lo excluye; mantener simetría.
        if all(r.get("codigo") == 2 for r in refs_a_exenta):
            return

        # Enriquecer in-place: marcar todos los items como exento.
        for it in req.items or []:
            it["exento"] = True

    @staticmethod
    def _extraer_items_del_xml_firmado(xml_bytes: bytes) -> list[dict]:
        """Parsea un sobre EnvioDTE firmado y devuelve la lista de items
        del primer (y único, por convención) Documento.

        Estructura esperada:
            EnvioDTE > SetDTE > DTE > Documento > Detalle×N
            Detalle: NroLinDet, NmbItem, QtyItem, PrcItem, DescuentoPct,
                     MontoItem, ...

        Namespace SII: ``http://www.sii.cl/SiiDte``.

        Retorna: ``[{nombre, cantidad, precio_unitario, descuento_pct,
        monto_item}]``. Los valores numéricos se preservan como ``int`` si
        son enteros exactos (típico del CLP sin decimales), si no ``float``.
        """
        ns = {"sii": "http://www.sii.cl/SiiDte"}
        root = etree.fromstring(xml_bytes)
        detalles = root.findall(".//sii:Documento/sii:Detalle", namespaces=ns)

        def _txt(detalle, tag: str) -> str | None:
            el = detalle.find(f"sii:{tag}", namespaces=ns)
            return el.text if el is not None else None

        def _num(detalle, tag: str):
            v = _txt(detalle, tag)
            if v is None:
                return None
            try:
                f = float(v)
            except (ValueError, TypeError):
                return None
            # CLP son enteros — preservar como int si no hay decimales reales
            if f == int(f):
                return int(f)
            return f

        items = []
        for d in detalles:
            items.append({
                "nombre": _txt(d, "NmbItem"),
                "cantidad": _num(d, "QtyItem"),
                "precio_unitario": _num(d, "PrcItem"),
                "descuento_pct": _num(d, "DescuentoPct"),
                "monto_item": _num(d, "MontoItem"),
            })
        return items

    def _validar_request(self, req: FacturaRequest) -> str | None:
        """Validación completa de reglas de negocio SII antes de emitir.

        Retorna mensaje de error si hay problema, None si todo OK.
        Se ejecuta ANTES de consumir folio para no desperdiciarlos.
        """
        # --- Tipo DTE válido ---
        if req.tipo_dte not in self.TIPOS_DTE_VALIDOS:
            return f"Tipo DTE {req.tipo_dte} no es válido. Tipos soportados: {', '.join(f'{k}={v}' for k, v in self.TIPOS_NOMBRES.items())}"

        # --- Items obligatorios ---
        if not req.items:
            return f"El DTE debe tener al menos 1 ítem de detalle"

        # --- Validar caracteres compatibles con ISO-8859-1 ---
        # El XML SII usa encoding ISO-8859-1. Caracteres fuera de Latin-1
        # (emojis, caracteres CJK, etc.) rompen el XML silenciosamente.
        error_charset = self._validar_charset_iso8859(req)
        if error_charset:
            return error_charset

        # --- Validar RUT formato básico (NN.NNN.NNN-X o NNNNNNNN-X) ---
        error_rut = self._validar_rut(req.receptor_rut, "receptor")
        if error_rut:
            return error_rut

        es_boleta = req.tipo_dte in (39, 41)

        # --- Receptor: datos obligatorios según tipo ---
        if not es_boleta:
            campos_receptor = {
                "receptor_rut": "RUT Receptor",
                "receptor_razon": "Razón Social Receptor",
                "receptor_giro": "Giro Receptor",
                "receptor_dir": "Dirección Receptor",
                "receptor_comuna": "Comuna Receptor",
            }
            for campo, nombre in campos_receptor.items():
                valor = getattr(req, campo, None)
                if not valor or not str(valor).strip():
                    return f"{nombre} es obligatorio para {self.TIPOS_NOMBRES[req.tipo_dte]}"

        # --- NC/ND: deben tener al menos 1 referencia al documento original ---
        if req.tipo_dte in (56, 61):
            nombre_tipo = "Nota de Crédito" if req.tipo_dte == 61 else "Nota de Débito"
            if not req.referencias:
                return f"{nombre_tipo} requiere al menos 1 referencia al documento original"
            # Verificar que al menos una referencia apunta a un DTE válido
            tiene_ref_dte = False
            for ref in req.referencias:
                tipo_ref = str(ref.get("tipo_doc", ""))
                if tipo_ref.isdigit() and int(tipo_ref) in (33, 34, 52, 56, 61):
                    tiene_ref_dte = True
                    # Verificar que tiene folio
                    if not ref.get("folio"):
                        return f"{nombre_tipo}: referencia a tipo {tipo_ref} requiere folio"
                    # Verificar CodRef para NC
                    if req.tipo_dte == 61 and ref.get("codigo") is None:
                        return f"Nota de Crédito requiere CodRef en la referencia (1=anula, 2=corrige texto, 3=corrige monto)"
                    break
            if not tiene_ref_dte:
                return f"{nombre_tipo} requiere al menos 1 referencia a un DTE (tipo 33, 34, 52, 56 o 61)"

            # CodRef=2 (corrige texto) — SII error REF-2-781 si lleva montos
            # Spec SII: el documento debe llevar exactamente 1 ítem placeholder
            # con cantidad=0 y precio_unitario=0 (MontoItem=0). MntTotal debe ser 0.
            refs_dte = [r for r in req.referencias
                        if str(r.get("tipo_doc", "")).isdigit() and int(str(r.get("tipo_doc"))) in (33, 34, 52, 56, 61)]
            es_corrige_texto = refs_dte and all(r.get("codigo") == 2 for r in refs_dte)
            if es_corrige_texto:
                items = req.items or []
                if len(items) != 1:
                    return (
                        f"{nombre_tipo} con CodRef=2 (corrige texto) debe tener exactamente 1 ítem "
                        f"placeholder. Recibidos: {len(items)}. SII rechaza con REF-2-781."
                    )
                item = items[0]
                cant = item.get("cantidad")
                precio = item.get("precio_unitario")
                monto_item = item.get("monto_item")
                if (cant not in (None, 0)) or (precio not in (None, 0)) or (monto_item not in (None, 0)):
                    return (
                        f"{nombre_tipo} con CodRef=2 (corrige texto) no debe tener cantidad ni montos. "
                        f"Item recibido: cantidad={cant}, precio_unitario={precio}, monto_item={monto_item}. "
                        f"SII rechaza con REF-2-781."
                    )

            # CodRef=1 (anula doc completo): debe replicar los ítems del original
            es_anula = refs_dte and all(r.get("codigo") == 1 for r in refs_dte)
            if es_anula and not req.items:
                return (
                    f"{nombre_tipo} con CodRef=1 (anula doc) debe incluir los ítems del documento "
                    f"original — el SII compara montos. Enviar la misma lista de ítems del DTE referenciado."
                )

            # CodRef=3 (modifica monto) — SII error REF-2-768 si MontoTotal=0.
            # Una NC/ND con CodRef=3 expresa un ajuste de monto; si todos los
            # ítems traen precio_unitario en 0/None, el MontoTotal sale en 0 y
            # el SII rebota con "Modificacion de montos debe tener monto mayor
            # a cero". Se valida pre-envío para no quemar folio.
            #
            # NOTA: si al menos un ítem tiene precio > 0, el MontoTotal será > 0
            # (los precios son no-negativos por definición), así que basta con
            # chequear que exista al menos uno con precio > 0. No se valida
            # que cada ítem individual tenga precio > 0: un caso legítimo es
            # un ajuste parcial donde algunos ítems quedan con precio=0 y otros
            # con precio > 0, el total es > 0 y el SII lo acepta.
            #
            # Este guard es universal — aplica por igual a producción y
            # certificación. En cert, además, ``_caso_a_factura_request``
            # enriquece precios desde el caso referenciado porque el SET del
            # SII declara ítems sin precio; este guard es la red final.
            es_modifica_monto = refs_dte and all(r.get("codigo") == 3 for r in refs_dte)
            if es_modifica_monto:
                items = req.items or []
                tiene_precio_positivo = any(
                    (it.get("precio_unitario") or 0) > 0 for it in items
                )
                if not tiene_precio_positivo:
                    nombres = ", ".join(
                        str(it.get("nombre", "?")) for it in items
                    ) or "(sin items)"
                    return (
                        f"{nombre_tipo} con CodRef=3 (modifica monto) debe tener "
                        f"al menos un ítem con precio_unitario > 0. Items "
                        f"recibidos: [{nombres}] todos con precio en 0/None. "
                        f"El SII rechaza con REF-2-768 'Modificacion de montos "
                        f"debe tener monto mayor a cero'."
                    )

            # Si la referencia es a un T34 (factura exenta), todos los ítems deben ser exentos.
            ref_a_t34 = any(
                int(str(r.get("tipo_doc", "0"))) == 34
                for r in refs_dte
                if str(r.get("tipo_doc", "")).isdigit()
            )
            if ref_a_t34 and not es_corrige_texto:
                for item in (req.items or []):
                    if not item.get("exento"):
                        return (
                            f"{nombre_tipo} referencia a Factura Exenta (T34) — todos los ítems deben "
                            f"marcarse como exentos. Item '{item.get('nombre', '?')}' no tiene exento=True."
                        )

        # --- Forma de pago ---
        if req.fma_pago is not None:
            if req.fma_pago not in (1, 2, 3):
                return f"FmaPago={req.fma_pago} no es válido. Valores: 1=Contado, 2=Crédito, 3=Sin costo"
            if req.fma_pago == 2 and not req.fecha_vencimiento:
                logger.warning("FmaPago=2 (Crédito) sin fecha_vencimiento — se recomienda incluirla")

        # --- Guía de despacho: indicadores ---
        error_guia = self._validar_guia_despacho(req)
        if error_guia:
            return error_guia

        # --- No-guías NO deben tener indicadores de traslado ---
        if req.tipo_dte != 52:
            if req.ind_traslado is not None:
                return f"IndTraslado solo aplica para Guía de Despacho (T52), no para {self.TIPOS_NOMBRES[req.tipo_dte]}"
            if req.tipo_despacho is not None:
                return f"TipoDespacho solo aplica para Guía de Despacho (T52), no para {self.TIPOS_NOMBRES[req.tipo_dte]}"

        return None

    def _validar_charset_iso8859(self, req: FacturaRequest) -> str | None:
        """Valida que todos los textos sean compatibles con ISO-8859-1.

        El XML SII usa encoding ISO-8859-1 (Latin-1). Caracteres fuera de
        este charset (emojis, CJK, etc.) causan error al serializar el XML,
        pero DESPUÉS de consumir folio. Esta validación lo detecta ANTES.
        """
        campos_texto = [
            ("receptor_razon", req.receptor_razon),
            ("receptor_giro", req.receptor_giro),
            ("receptor_dir", req.receptor_dir),
            ("receptor_comuna", req.receptor_comuna),
            ("receptor_ciudad", req.receptor_ciudad),
        ]
        for item in (req.items or []):
            campos_texto.append((f"item '{item.get('nombre', '?')}'", item.get("nombre", "")))
            if item.get("unidad_medida"):
                campos_texto.append((f"unidad_medida item", item["unidad_medida"]))
        for ref in (req.referencias or []):
            if ref.get("razon"):
                campos_texto.append(("razon_ref", ref["razon"]))

        for campo, valor in campos_texto:
            if not valor:
                continue
            try:
                valor.encode("iso-8859-1")
            except (UnicodeEncodeError, AttributeError):
                return (
                    f"El campo {campo} contiene caracteres no compatibles con ISO-8859-1: "
                    f"'{valor}'. El XML SII requiere encoding Latin-1. "
                    f"Caracteres válidos: letras acentuadas (á,é,ñ,ü), símbolos comunes."
                )
        return None

    def _validar_rut(self, rut: str, contexto: str) -> str | None:
        """Valida formato básico de RUT chileno."""
        if not rut or not rut.strip():
            return f"RUT {contexto} es obligatorio"
        rut_limpio = rut.replace(".", "").replace(" ", "").upper()
        if "-" not in rut_limpio:
            return f"RUT {contexto} '{rut}' debe tener formato XXXXXXXX-X"
        partes = rut_limpio.split("-")
        if len(partes) != 2:
            return f"RUT {contexto} '{rut}' formato inválido"
        cuerpo, dv = partes
        if not cuerpo.isdigit():
            return f"RUT {contexto} '{rut}': cuerpo debe ser numérico"
        if dv not in "0123456789K":
            return f"RUT {contexto} '{rut}': dígito verificador inválido"
        return None

    def _validar_guia_despacho(self, req: FacturaRequest) -> str | None:
        """Valida reglas de negocio para Guía de Despacho (T52).

        Reglas SII:
        - IndTraslado es obligatorio para T52
        - TipoDespacho solo aplica cuando hay venta/consignación (IndTraslado 1 o 3)
        - Traslado interno (5), otros traslados (6), devolución (7) NO llevan TipoDespacho
        - Venta por efectuar (2) NO lleva TipoDespacho (la venta aún no se concreta)
        """
        if req.tipo_dte != 52:
            return None

        if req.ind_traslado is None:
            return "Guía de Despacho (T52) requiere IndTraslado (tipo de traslado)"

        if req.ind_traslado not in (1, 2, 3, 4, 5, 6, 7):
            return f"IndTraslado={req.ind_traslado} no es válido. Valores: 1=venta, 2=venta por efectuar, 3=consignación, 4=promoción, 5=traslado interno, 6=otros, 7=devolución"

        if req.tipo_despacho is not None and req.tipo_despacho not in (1, 2, 3):
            return f"TipoDespacho={req.tipo_despacho} no es válido. Valores: 1=por cuenta receptor, 2=emisor a instalaciones cliente, 3=emisor a otras instalaciones"

        # TipoDespacho solo es compatible con venta (1) o consignación (3)
        if req.tipo_despacho is not None and req.ind_traslado not in (1, 3):
            nombres = {2: "Venta por Efectuar", 4: "Promoción/Donación", 5: "Traslado Interno", 6: "Otros Traslados", 7: "Devolución"}
            nombre = nombres.get(req.ind_traslado, str(req.ind_traslado))
            return (
                f"TipoDespacho={req.tipo_despacho} no corresponde con IndTraslado={req.ind_traslado} ({nombre}). "
                f"TipoDespacho solo aplica para IndTraslado=1 (Venta) o IndTraslado=3 (Consignación)"
            )

        # Si es venta (1) o consignación (3), TipoDespacho debería estar presente
        if req.ind_traslado in (1, 3) and req.tipo_despacho is None:
            tipo = "Venta" if req.ind_traslado == 1 else "Consignación"
            return (
                f"IndTraslado={req.ind_traslado} ({tipo}) requiere TipoDespacho: "
                f"1=por cuenta receptor, 2=emisor a instalaciones cliente, 3=emisor a otras instalaciones"
            )

        return None

    def emitir_factura(
        self,
        req: FacturaRequest,
        enviar_sii: bool = True,
        *,
        folio_override: int | None = None,
        session=None,
        empresa_id: str | None = None,
    ) -> EmisionResult:
        """Emite un DTE completo usando el flujo aprobado por el SII.

        Args:
            req: FacturaRequest con los datos del documento.
            enviar_sii: si False, sólo se firma y persiste — no se envía
                al SII. Permite revisar XML antes de enviar.
            folio_override: si se indica, reutiliza ese folio sin avanzar
                el contador del CAF. Pensado para **regeneración** de un
                DTE cuyo sobre fue rechazado por el SII (STATUS=7 esquema
                inválido, firma rechazada, etc.): el SII nunca recepcionó
                el DTE viejo, el folio formalmente no está quemado, y
                reasignarlo sería desperdiciar un folio útil.
                Falla si el folio no pertenece a ningún CAF cargado.
            session: sesión de BD del tenant para habilitar enriquecimiento
                de items CodRef=3 (NC/ND MODIFICA MONTO) leyendo el
                ``DteEmitido`` referenciado. Si no se pasa, se asume que
                el caller ya enriqueció los items. Callers modernos
                (routers cert/producción) deberían pasar siempre ``session``
                + ``empresa_id``; queda opcional por compatibilidad con
                scripts legacy.
            empresa_id: id del tenant (requerido para filtrar el
                ``DteEmitido`` por empresa al enriquecer). Debe pasarse
                junto con ``session``.
        """
        try:
            # Enriquecer items CodRef=3 (MODIFICA MONTO) ANTES de validar.
            # Es el único "preprocesamiento" que hace el core: si el caller
            # manda items con precio=0/None referenciando un DTE existente,
            # el core hereda los precios del original. El guard de
            # ``_validar_request`` sigue siendo la red final por si el
            # enriquecimiento no pudo completar.
            if session is not None and empresa_id is not None:
                try:
                    self._enriquecer_items_codref3(req, session, empresa_id)
                except ValueError as e:
                    return EmisionResult(ok=False, error=str(e))

            # Enriquecer items cuando la NC/ND referencia una Factura Exenta
            # (T34): marcar todos los items como ``exento=True``. El SET del
            # SII y el mapper del wizard no lo propagan, y el validador del
            # core lo exige más abajo. Independiente de session/empresa_id —
            # sólo lee req.referencias.
            self._enriquecer_items_referencia_a_exenta(req)

            # Validar TODAS las reglas de negocio antes de consumir folio
            error_validacion = self._validar_request(req)
            if error_validacion:
                return EmisionResult(ok=False, error=error_validacion)

            self._cargar_firma()

            # 1. Obtener folio — DB tiene prioridad sobre archivos
            if folio_override is not None:
                # Regeneración: NO avanzar el contador, sólo obtener el CAF
                folio = folio_override
                if self._caf_manager_db:
                    caf = self._caf_manager_db.obtener_caf(req.tipo_dte, folio)
                else:
                    self._cargar_cafs()
                    caf = self._caf_manager.obtener_caf(req.tipo_dte, folio)
            elif self._caf_manager_db:
                folio, caf = self._caf_manager_db.siguiente_folio(req.tipo_dte)
            else:
                self._cargar_cafs()
                folio = self._caf_manager.siguiente_folio(req.tipo_dte)
                caf = self._caf_manager.obtener_caf(req.tipo_dte, folio)

            if not caf:
                return EmisionResult(ok=False, error=f"No hay CAF disponible para tipo {req.tipo_dte} folio {folio}")

            fecha_hoy = datetime.now().strftime("%Y-%m-%d")

            # 2. Construir DTE model
            # Usa dirección de sucursal si está configurada, sino usa la de empresa
            dir_origen = self.config.sucursal_direccion or self.config.direccion
            cmna_origen = self.config.sucursal_comuna or self.config.comuna
            ciudad_origen = self.config.sucursal_ciudad or self.config.ciudad

            emisor = {
                "RUTEmisor": self.config.rut,
                "RznSoc": self.config.razon_social,
                "GiroEmis": self.config.giro,
                "Acteco": self.config.acteco,
                "DirOrigen": dir_origen,
                "CmnaOrigen": cmna_origen,
                "CiudadOrigen": ciudad_origen,
            }
            # Código sucursal SII (si tiene)
            if self.config.sucursal_sii:
                emisor["SucDeSII"] = self.config.sucursal_sii
            receptor = {
                "RUTRecep": req.receptor_rut,
                "RznSocRecep": req.receptor_razon,
                "GiroRecep": req.receptor_giro,
                "DirRecep": req.receptor_dir,
                "CmnaRecep": req.receptor_comuna,
            }
            if req.receptor_ciudad:
                receptor["CiudadRecep"] = req.receptor_ciudad

            items_dte = []
            for i, item in enumerate(req.items or [], start=1):
                cantidad = item.get("cantidad")
                precio = item.get("precio_unitario", 0)
                descuento_pct = item.get("descuento_pct")

                # Si hay precio pero no cantidad, defaultear a 1
                # (SII requiere QtyItem*PrcItem=MontoItem en el Detalle)
                if cantidad is None and precio:
                    cantidad = 1

                if cantidad is not None and precio:
                    monto = round(cantidad * precio)
                else:
                    monto = item.get("monto_item") or precio or 0

                desc_monto = None
                if descuento_pct and monto:
                    desc_monto = round(monto * descuento_pct / 100)
                    monto -= desc_monto

                items_dte.append(ItemDetalle(
                    nro_linea=i,
                    nombre=item["nombre"],
                    cantidad=cantidad,
                    unidad_medida=item.get("unidad_medida"),
                    precio_unitario=precio if precio else None,
                    descuento_pct=descuento_pct,
                    descuento_monto=desc_monto,
                    monto_item=monto,
                    exento=item.get("exento", False),
                ))

            referencias_dte = []
            ref_counter = 1
            # Referencia SET para certificación (caso del set de pruebas)
            if req.caso_set:
                referencias_dte.append(Referencia(
                    nro_linea=ref_counter,
                    tipo_doc_ref="SET",
                    folio_ref="0",
                    fecha_ref=fecha_hoy,
                    razon_ref=req.caso_set,
                    codigo_ref=None,
                ))
                ref_counter += 1
            # OC como referencia tipo 801 (si se proporcionó)
            if req.oc_numero:
                referencias_dte.append(Referencia(
                    nro_linea=ref_counter,
                    tipo_doc_ref="801",
                    folio_ref=str(req.oc_numero),
                    fecha_ref=req.oc_fecha,
                    razon_ref=None,
                    codigo_ref=None,
                ))
                ref_counter += 1
            # Referencias adicionales (NC/ND)
            for ref in (req.referencias or []):
                referencias_dte.append(Referencia(
                    nro_linea=ref_counter,
                    tipo_doc_ref=str(ref["tipo_doc"]),
                    folio_ref=str(ref["folio"]),
                    fecha_ref=ref.get("fecha"),
                    razon_ref=ref.get("razon"),
                    codigo_ref=ref.get("codigo"),
                ))
                ref_counter += 1

            # Calcular datos de pago para crédito
            fecha_vencimiento = req.fecha_vencimiento
            fecha_pago = None
            monto_pago = None

            # Descuentos globales
            descuentos_globales_dte = []
            for i, dg in enumerate(req.descuentos_globales or [], start=1):
                descuentos_globales_dte.append(DescuentoGlobal(
                    nro_linea=i,
                    tipo=dg.get("tipo", "D"),
                    descripcion=dg.get("descripcion", "Descuento global"),
                    tipo_valor=dg.get("tipo_valor", "%"),
                    valor=dg.get("valor", 0),
                    indicador_exento=dg.get("indicador_exento"),
                ))

            dte = DTE(
                tipo_dte=req.tipo_dte,
                folio=folio,
                fecha_emision=fecha_hoy,
                emisor=emisor,
                receptor=receptor,
                items=items_dte,
                referencias=referencias_dte,
                descuentos_globales=descuentos_globales_dte,
                fma_pago=req.fma_pago,
                fecha_vencimiento=fecha_vencimiento,
                tipo_traslado=req.ind_traslado,
                tipo_despacho=req.tipo_despacho,
            )
            dte.calcular_totales()

            # MntPagos: si es crédito, agregar fecha y monto de pago
            if req.fma_pago == 2 and fecha_vencimiento:
                dte.fecha_pago = fecha_vencimiento
                dte.monto_pago = dte.monto_total

            # 3. Generar XML del documento (incluye TED)
            documento = generar_documento_xml(dte, caf)
            dte_element = generar_dte_xml(documento)
            doc_id = documento.get("ID", f"F{folio}T{req.tipo_dte}")

            # Obtener TED como string para PDF
            ted_el = documento.find("TED")
            ted_xml_str = etree.tostring(ted_el, encoding="unicode") if ted_el is not None else ""

            # ═══════════════════════════════════════════════════════
            # FIRMA — MISMO FLUJO EXACTO DE firmar_set.py
            # ═══════════════════════════════════════════════════════

            # Paso A: Serializar Documento limpiando namespaces
            doc_str = etree.tostring(
                documento, encoding="ISO-8859-1", xml_declaration=False
            ).decode("ISO-8859-1")
            doc_str = _limpiar_namespaces(doc_str)

            # Paso B: Envolver en DTE con namespace
            dte_str = f'<DTE xmlns="{SII_NS}" version="1.0">{doc_str}</DTE>'

            # Paso C: Firmar DTE con la librería (type='doc')
            signed_dte = self._firma.firmar(dte_str, doc_id, type="doc")
            if not signed_dte:
                return EmisionResult(ok=False, folio=folio, error="Error firmando DTE")

            # Paso C.1: VERIFICAR firma del DTE antes de continuar
            # Reproduce lo que hará el SII: re-computa digest y verifica RSA.
            # Si falla aquí, el SII rechazaría con DTE-3-505.
            try:
                codigo_verif, msg_verif = self._firma.verificar_firma_xml(signed_dte)
                if codigo_verif != 0:
                    logger.error("Verificación de firma DTE FALLÓ: %s (folio=%s)", msg_verif, folio)
                    return EmisionResult(
                        ok=False, folio=folio,
                        error=f"Firma DTE inválida (pre-verificación): {msg_verif}. "
                              f"Esto habría sido rechazado por el SII con DTE-3-505.",
                    )
                logger.debug("Firma DTE verificada OK (folio=%s)", folio)
            except Exception as e:
                logger.warning("No se pudo verificar firma DTE (folio=%s): %s", folio, e)
                # No bloquear si la verificación falla por error interno,
                # pero sí logear para diagnóstico

            # Paso D: Construir Caratula
            rut_envia = self.config.rut_firmante or self.config.rut
            timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            caratula = (
                f'<Caratula version="1.0">'
                f'<RutEmisor>{self.config.rut}</RutEmisor>'
                f'<RutEnvia>{rut_envia}</RutEnvia>'
                f'<RutReceptor>60803000-K</RutReceptor>'
                f'<FchResol>{self.config.fecha_resolucion}</FchResol>'
                f'<NroResol>{self.config.numero_resolucion}</NroResol>'
                f'<TmstFirmaEnv>{timestamp}</TmstFirmaEnv>'
                f'<SubTotDTE><TpoDTE>{req.tipo_dte}</TpoDTE><NroDTE>1</NroDTE></SubTotDTE>'
                f'</Caratula>'
            )

            # Paso E: Armar sobre (EnvioBOLETA para boletas, EnvioDTE para el resto)
            es_boleta = req.tipo_dte in (39, 41)
            if es_boleta:
                schema_loc = f"{SII_NS} EnvioBOLETA_v11.xsd"
                env_tag = "EnvioBOLETA"
            else:
                schema_loc = f"{SII_NS} EnvioDTE_v10.xsd"
                env_tag = "EnvioDTE"
            env_str = (
                f'<{env_tag} xmlns="{SII_NS}" '
                f'xmlns:xsi="{XSI_NS}" '
                f'xsi:schemaLocation="{schema_loc}" '
                f'version="1.0">'
                f'<SetDTE ID="SetDoc">'
                f'{caratula}'
                f'{signed_dte}'
                f'</SetDTE>'
                f'</{env_tag}>'
            )

            # Paso F: Firmar sobre (type='env' para DTE, 'env_boleta' para boletas)
            firma_type = "env_boleta" if es_boleta else "env"
            signed_env = self._firma.firmar(env_str, "SetDoc", type=firma_type)
            if not signed_env:
                return EmisionResult(ok=False, folio=folio, error=f"Error firmando sobre {env_tag}")

            # Paso F.1: VERIFICAR firma del sobre
            try:
                codigo_env, msg_env = self._firma.verificar_firma_xml(signed_env)
                if codigo_env != 0:
                    logger.error("Verificación de firma sobre FALLÓ: %s", msg_env)
                    return EmisionResult(
                        ok=False, folio=folio,
                        error=f"Firma sobre {env_tag} inválida (pre-verificación): {msg_env}",
                    )
                logger.debug("Firma sobre verificada OK")
            except Exception as e:
                logger.warning("No se pudo verificar firma sobre: %s", e)

            # Paso G: Agregar declaración XML
            xml_final = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed_env
            xml_bytes = xml_final.encode("ISO-8859-1")

            # 4. Enviar al SII (si está habilitado)
            track_id = None
            if enviar_sii:
                es_boleta = req.tipo_dte in (39, 41)
                if es_boleta:
                    # Boletas: REST API con token separado
                    token = self._obtener_token_boleta()
                    resultado_sii = enviar_boleta(
                        xml_bytes=xml_bytes,
                        token=token,
                        rut_emisor=self.config.rut,
                        rut_envia=rut_envia,
                    )
                else:
                    # DTEs: SOAP con token estándar
                    token = self._obtener_token()
                    resultado_sii = enviar_dte(
                        xml_bytes=xml_bytes,
                        token=token,
                        rut_emisor=self.config.rut,
                        rut_envia=rut_envia,
                    )

                track_id = resultado_sii.get("track_id")
                logger.info("SII: status=%s, track_id=%s", resultado_sii.get('status'), track_id)
                if resultado_sii.get("status") != "OK" and not track_id:
                    raw_preview = resultado_sii.get('raw', '')[:300]
                    return EmisionResult(
                        ok=False,
                        folio=folio,
                        error=f"Error SII: {resultado_sii.get('glosa', '')} | {raw_preview}",
                        xml_firmado=xml_bytes,
                        ted_xml=ted_xml_str,
                    )
            else:
                logger.info("VALIDACIÓN: XML generado y firmado sin enviar al SII (folio=%s)", folio)

            # 5. Generar PDF
            tipo_nombres = {
                33: "FACTURA ELECTRONICA",
                34: "FACTURA NO AFECTA O EXENTA ELECTRONICA",
                56: "NOTA DE DEBITO ELECTRONICA",
                61: "NOTA DE CREDITO ELECTRONICA",
                52: "GUIA DE DESPACHO ELECTRONICA",
            }
            print_data = DTEPrintData(
                tipo_dte=req.tipo_dte,
                folio=folio,
                fecha_emision=fecha_hoy,
                emisor_rut=self.config.rut,
                emisor_razon=self.config.razon_social,
                emisor_giro=self.config.giro,
                emisor_dir=self.config.direccion,
                emisor_comuna=self.config.comuna,
                emisor_ciudad=self.config.ciudad,
                receptor_rut=req.receptor_rut,
                receptor_razon=req.receptor_razon,
                receptor_giro=req.receptor_giro,
                receptor_dir=req.receptor_dir,
                receptor_comuna=req.receptor_comuna,
                items=[{
                    "nombre": it.nombre,
                    "cantidad": it.cantidad,
                    "precio_unitario": it.precio_unitario,
                    "monto": it.monto_item,
                    "exento": it.exento,
                    "descuento_pct": it.descuento_pct,
                    "descuento_monto": it.descuento_monto,
                } for it in items_dte],
                descuentos_globales=[{
                    "tipo": dg.tipo,
                    "descripcion": dg.descripcion,
                    "tipo_valor": dg.tipo_valor,
                    "valor": dg.valor,
                } for dg in descuentos_globales_dte],
                monto_neto=dte.monto_neto,
                monto_exento=dte.monto_exento,
                iva=dte.iva,
                monto_total=dte.monto_total,
                ted_xml=ted_xml_str,
                referencias=[{
                    "tipo": str(r.tipo_doc_ref),
                    "folio": str(r.folio_ref),
                    "fecha": r.fecha_ref or "",
                    "razon": r.razon_ref or "",
                } for r in referencias_dte],
                fma_pago=req.fma_pago,
                fecha_vencimiento=req.fecha_vencimiento,
                numero_resolucion=self.config.numero_resolucion,
                fecha_resolucion=self.config.fecha_resolucion,
            )
            try:
                pdf_bytes = generar_pdf(print_data, formato="carta")
            except Exception:
                pdf_bytes = None

            return EmisionResult(
                ok=True,
                folio=folio,
                track_id=track_id,
                xml_firmado=xml_bytes,
                ted_xml=ted_xml_str,
                pdf_bytes=pdf_bytes,
                monto_neto=dte.monto_neto,
                monto_exento=dte.monto_exento,
                iva=dte.iva,
                monto_total=dte.monto_total,
            )

        except Exception as e:
            import traceback
            return EmisionResult(ok=False, error=f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}")
