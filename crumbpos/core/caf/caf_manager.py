"""Parser y gestor de folios CAF (Código de Autorización de Folios).

Los folios consumidos se persisten en un archivo JSON para sobrevivir
reinicios del servidor. Nunca se reasigna un folio ya consumido.

Validaciones SII:
- Firma FRMA: el CAF está firmado por el SII. Si se dispone de la llave
  pública del SII (env var ``SII_PUBLIC_KEY_PATH``) se verifica la firma
  antes de aceptar el CAF. Referencia: instructivo_emision.pdf §1.4.
- Vigencia: un CAF tiene vigencia máxima de 2 años desde su fecha de
  autorización (timbraje_electronico.pdf pág 10).
"""
import base64
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

from lxml import etree
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes

from crumbpos.core.security.xml_safe import parse_safe
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_pem_public_key,
)

# Vigencia máxima de un CAF en días (2 años, SII timbraje_electronico.pdf)
CAF_VIGENCIA_MAX_DIAS = 730


class CAF:
    """Representa un archivo CAF del SII."""

    def __init__(self, xml_path: str | Path):
        self.xml_path = Path(xml_path)
        # CAF viene del SII pero pasa por el usuario (upload): parser endurecido
        # contra XXE / billion laughs.
        self._tree = parse_safe(str(self.xml_path))
        self._root = self._tree.getroot()
        self._parse()

    def _parse(self):
        caf = self._root.find("CAF")
        da = caf.find("DA")

        self.rut_emisor = da.findtext("RE")
        self.razon_social = da.findtext("RS")
        self.tipo_dte = int(da.findtext("TD"))
        rng = da.find("RNG")
        self.folio_desde = int(rng.findtext("D"))
        self.folio_hasta = int(rng.findtext("H"))
        self.fecha_autorizacion = da.findtext("FA")
        self.idk = da.findtext("IDK")

        # Llave pública RSA del CAF (componentes)
        rsapk = da.find("RSAPK")
        self.modulus = rsapk.findtext("M")
        self.exponent = rsapk.findtext("E")

        # Firma del CAF
        frma = caf.find("FRMA")
        self.firma_caf = frma.text
        self.algoritmo_firma = frma.get("algoritmo")

        # Llave privada RSA (para firmar el TED)
        self.private_key_pem = self._root.findtext("RSASK").strip()

        # Llave pública RSA
        self.public_key_pem = self._root.findtext("RSAPUBK").strip()

        # Elemento DA completo como XML string (se usa en el TED)
        self.da_xml = etree.tostring(da, encoding="unicode")

        # Elemento CAF completo como BYTES RAW del archivo original
        # CRÍTICO: No re-serializar con lxml — preservar bytes exactos
        # para que la firma FRMA del SII siga siendo válida
        with open(str(self.xml_path), "rb") as _f:
            _raw = _f.read()
        _m = re.search(rb'<CAF version.*?</CAF>', _raw, re.DOTALL)
        self.caf_xml_raw = _m.group(0) if _m else b""
        # Versión string para compatibilidad (ISO-8859-1)
        self.caf_xml = self.caf_xml_raw.decode("ISO-8859-1") if self.caf_xml_raw else etree.tostring(caf, encoding="unicode")

        # Bytes RAW del elemento DA del archivo original — se usan para
        # verificar la firma FRMA del SII sin re-serializar (cualquier
        # cambio en whitespace o encoding invalida la firma).
        _da_match = re.search(rb'<DA>.*?</DA>', _raw, re.DOTALL)
        self.da_xml_raw = _da_match.group(0) if _da_match else b""

    def get_private_key(self):
        """Retorna la llave privada RSA como objeto cryptography."""
        return load_pem_private_key(
            self.private_key_pem.encode("ascii"),
            password=None,
            backend=default_backend(),
        )

    def folio_valido(self, folio: int) -> bool:
        """Verifica si un folio está dentro del rango autorizado."""
        return self.folio_desde <= folio <= self.folio_hasta

    def dias_desde_autorizacion(self, hoy: date | None = None) -> int:
        """Días transcurridos desde la fecha de autorización del CAF.

        ``fecha_autorizacion`` viene del SII en formato ``YYYY-MM-DD``.
        """
        if not self.fecha_autorizacion:
            raise ValueError("CAF sin fecha de autorización")
        fecha = datetime.strptime(self.fecha_autorizacion, "%Y-%m-%d").date()
        hoy = hoy or date.today()
        return (hoy - fecha).days

    def esta_vigente(
        self,
        max_dias: int = CAF_VIGENCIA_MAX_DIAS,
        hoy: date | None = None,
    ) -> bool:
        """Indica si el CAF aún está dentro de su vigencia.

        Por defecto usa 2 años (730 días) según SII timbraje_electronico.pdf.
        Tipos boleta (39/41) tienen vigencia diferente (1 año) — pasar
        ``max_dias=365`` al llamar si corresponde.
        """
        try:
            return self.dias_desde_autorizacion(hoy) <= max_dias
        except ValueError:
            return False

    def validar_firma_sii(self, llave_publica_pem: str | bytes) -> bool:
        """Verifica la firma FRMA del CAF contra la llave pública del SII.

        La firma FRMA es una firma RSA-SHA1 sobre los bytes canónicos del
        elemento ``<DA>``. Se usan los bytes raw del archivo para preservar
        la canonicalización original — cualquier re-serialización
        invalidaría la firma.

        Args:
            llave_publica_pem: Llave pública del SII en formato PEM
                (bytes o string).

        Returns:
            ``True`` si la firma es válida.

        Raises:
            ValueError: si faltan datos para validar (DA raw o FRMA).
            cryptography.exceptions.InvalidSignature: si la firma no
                verifica contra la llave pública provista.
        """
        if not self.da_xml_raw:
            raise ValueError(
                "No se puede validar firma: no se extrajeron bytes raw del DA"
            )
        if not self.firma_caf:
            raise ValueError("CAF sin elemento FRMA")

        if isinstance(llave_publica_pem, str):
            llave_publica_pem = llave_publica_pem.encode("ascii")
        public_key = load_pem_public_key(llave_publica_pem, backend=default_backend())

        firma_bytes = base64.b64decode(self.firma_caf)

        # SII firma FRMA con RSA-SHA1 sobre los bytes del elemento DA.
        # Si la firma no verifica, cryptography levanta InvalidSignature.
        public_key.verify(
            firma_bytes,
            self.da_xml_raw,
            padding.PKCS1v15(),
            hashes.SHA1(),
        )
        return True

    def validar(
        self,
        llave_publica_sii: str | bytes | None = None,
        max_dias_vigencia: int = CAF_VIGENCIA_MAX_DIAS,
    ) -> list[str]:
        """Ejecuta todas las validaciones del CAF y retorna lista de errores.

        - Vigencia <= ``max_dias_vigencia`` días desde autorización.
        - Firma FRMA válida contra llave pública del SII (si se provee).

        No levanta excepciones: retorna lista de mensajes. Lista vacía =
        CAF válido.
        """
        errores: list[str] = []

        # Vigencia
        try:
            dias = self.dias_desde_autorizacion()
            if dias > max_dias_vigencia:
                errores.append(
                    f"CAF vencido: {dias} días desde autorización "
                    f"(máximo {max_dias_vigencia}). "
                    f"Fecha autorización: {self.fecha_autorizacion}"
                )
        except ValueError as e:
            errores.append(f"Error leyendo fecha de autorización: {e}")

        # Firma FRMA
        if llave_publica_sii is not None:
            try:
                self.validar_firma_sii(llave_publica_sii)
            except InvalidSignature:
                errores.append(
                    "Firma FRMA del CAF no verifica contra la llave "
                    "pública del SII — CAF inválido o adulterado"
                )
            except ValueError as e:
                errores.append(f"Error validando firma FRMA: {e}")

        return errores

    def __repr__(self):
        return (
            f"CAF(tipo={self.tipo_dte}, "
            f"folios={self.folio_desde}-{self.folio_hasta}, "
            f"rut={self.rut_emisor})"
        )


def _cargar_llave_publica_sii() -> bytes | None:
    """Carga la llave pública del SII desde env var ``SII_PUBLIC_KEY_PATH``.

    Retorna ``None`` si la variable no está seteada o el archivo no existe.
    El llamador debe manejar ese caso (generalmente log warning y continuar).
    """
    ruta = os.environ.get("SII_PUBLIC_KEY_PATH")
    if not ruta:
        return None
    p = Path(ruta)
    if not p.exists():
        print(
            f"  WARN: SII_PUBLIC_KEY_PATH={ruta} no existe — "
            f"no se validará firma FRMA de CAFs"
        )
        return None
    return p.read_bytes()


class CAFManager:
    """Gestiona múltiples archivos CAF."""

    def __init__(self, caf_dir: str | Path):
        self.caf_dir = Path(caf_dir)
        self._cafs: dict[int, list[CAF]] = {}
        self._folio_actual: dict[int, int] = {}
        self._folios_file = self.caf_dir / "folios_consumidos.json"
        self._llave_publica_sii = _cargar_llave_publica_sii()
        self._cargar_cafs()
        self._cargar_folios_persistidos()

    def _cargar_cafs(self):
        """Carga todos los archivos CAF del directorio y subdirectorios.

        Busca recursivamente en subdirectorios (ej: /CAF/33/, /CAF/56/).
        Cuando hay CAFs duplicados para el mismo rango de folios,
        el último cargado (subdirectorio) reemplaza al anterior.
        """
        for xml_file in sorted(self.caf_dir.rglob("*.xml")):
            try:
                caf = CAF(xml_file)
                if caf.tipo_dte not in self._cafs:
                    self._cafs[caf.tipo_dte] = []

                # Reemplazar CAF existente si cubre el mismo rango de folios
                existing = self._cafs[caf.tipo_dte]
                replaced = False
                for i, old_caf in enumerate(existing):
                    if old_caf.folio_desde == caf.folio_desde and old_caf.folio_hasta == caf.folio_hasta:
                        existing[i] = caf
                        replaced = True
                        break
                if not replaced:
                    existing.append(caf)

                # Inicializar folio actual con el primer folio disponible
                if caf.tipo_dte not in self._folio_actual:
                    self._folio_actual[caf.tipo_dte] = caf.folio_desde
            except Exception as e:
                print(f"Error cargando CAF {xml_file}: {e}")

    def obtener_caf(self, tipo_dte: int, folio: int) -> CAF | None:
        """Obtiene el CAF que contiene el folio indicado."""
        for caf in self._cafs.get(tipo_dte, []):
            if caf.folio_valido(folio):
                return caf
        return None

    def siguiente_folio(self, tipo_dte: int) -> int:
        """Obtiene el siguiente folio disponible para un tipo de DTE.

        El folio se persiste ANTES de retornarlo para garantizar que
        nunca se reasigne, incluso si el servidor se cae después.
        """
        folio = self._folio_actual.get(tipo_dte)
        if folio is None:
            raise ValueError(f"No hay CAF para tipo DTE {tipo_dte}")

        # Verificar que hay un CAF válido para este folio
        caf = self.obtener_caf(tipo_dte, folio)
        if not caf:
            raise ValueError(
                f"Folio {folio} para tipo {tipo_dte} fuera de rango de CAFs disponibles. "
                f"Solicite nuevos folios al SII."
            )

        # Avanzar y persistir ANTES de retornar
        self._folio_actual[tipo_dte] = folio + 1
        self._persistir_folios()
        return folio

    def _cargar_folios_persistidos(self):
        """Carga folios consumidos desde disco. Si el archivo indica un
        folio mayor al que tenemos en memoria, usamos el del archivo."""
        if not self._folios_file.exists():
            return
        try:
            data = json.loads(self._folios_file.read_text())
            for tipo_str, folio in data.items():
                tipo = int(tipo_str)
                # Usar el MAYOR entre lo que dice el archivo y lo que
                # calculamos de los CAFs (nunca retroceder)
                if tipo in self._folio_actual:
                    self._folio_actual[tipo] = max(self._folio_actual[tipo], folio)
                else:
                    self._folio_actual[tipo] = folio
            print(f"  Folios cargados desde {self._folios_file}: {self._folio_actual}")
        except Exception as e:
            print(f"  WARN: Error leyendo {self._folios_file}: {e}")

    def _persistir_folios(self):
        """Guarda el estado actual de folios a disco."""
        try:
            data = {str(k): v for k, v in self._folio_actual.items()}
            self._folios_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"  ERROR: No se pudo persistir folios: {e}")

    def tipos_disponibles(self) -> list[int]:
        """Lista los tipos de DTE con CAF cargados."""
        return list(self._cafs.keys())

    def estado_folios(self) -> list[dict]:
        """Retorna estado detallado de folios por tipo DTE.

        Para cada tipo incluye: rangos CAF, folio actual, disponibles,
        total autorizado, porcentaje usado y nivel de alerta.
        """
        NOMBRES_DTE = {
            33: "Factura Electrónica",
            34: "Factura Exenta Electrónica",
            39: "Boleta Electrónica",
            41: "Boleta Exenta Electrónica",
            46: "Factura de Compra Electrónica",
            52: "Guía de Despacho Electrónica",
            56: "Nota de Débito Electrónica",
            61: "Nota de Crédito Electrónica",
        }
        resultado = []
        for tipo in sorted(self._cafs.keys()):
            cafs = self._cafs[tipo]
            folio_actual = self._folio_actual.get(tipo, 0)

            # Construir rangos
            rangos = []
            total_autorizados = 0
            folio_min_global = None
            folio_max_global = None
            for caf in sorted(cafs, key=lambda c: c.folio_desde):
                rango_total = caf.folio_hasta - caf.folio_desde + 1
                total_autorizados += rango_total
                if folio_min_global is None or caf.folio_desde < folio_min_global:
                    folio_min_global = caf.folio_desde
                if folio_max_global is None or caf.folio_hasta > folio_max_global:
                    folio_max_global = caf.folio_hasta

                # Folios usados en este rango
                usado_desde = max(caf.folio_desde, folio_min_global)
                usado_hasta = min(caf.folio_hasta, folio_actual - 1)
                usados_rango = max(0, usado_hasta - caf.folio_desde + 1) if folio_actual > caf.folio_desde else 0

                rangos.append({
                    "desde": caf.folio_desde,
                    "hasta": caf.folio_hasta,
                    "total": rango_total,
                    "usados": usados_rango,
                    "archivo": caf.xml_path.name,
                    "fecha_autorizacion": caf.fecha_autorizacion,
                })

            # Calcular disponibles reales (folios >= folio_actual dentro de rangos CAF)
            disponibles = 0
            for caf in cafs:
                inicio = max(caf.folio_desde, folio_actual)
                if inicio <= caf.folio_hasta:
                    disponibles += caf.folio_hasta - inicio + 1

            # Consumidos = todos los que ya pasaron
            consumidos = folio_actual - folio_min_global if folio_min_global else 0
            consumidos = max(0, consumidos)

            # Porcentaje usado
            pct_usado = round(consumidos / total_autorizados * 100, 1) if total_autorizados > 0 else 0

            # Alerta
            if disponibles == 0:
                alerta = "agotado"
            elif disponibles <= 3:
                alerta = "critico"
            elif disponibles <= 10:
                alerta = "bajo"
            elif pct_usado >= 80:
                alerta = "advertencia"
            else:
                alerta = "ok"

            resultado.append({
                "tipo_dte": tipo,
                "nombre": NOMBRES_DTE.get(tipo, f"Tipo {tipo}"),
                "folio_actual": folio_actual,
                "folio_min": folio_min_global,
                "folio_max": folio_max_global,
                "disponibles": disponibles,
                "consumidos": consumidos,
                "total_autorizados": total_autorizados,
                "pct_usado": pct_usado,
                "alerta": alerta,
                "rangos": rangos,
            })

        return resultado

    def set_folio(self, tipo_dte: int, folio: int):
        """Establece manualmente el próximo folio para un tipo DTE.

        Solo permite avanzar (nunca retroceder) para evitar reutilización.
        """
        actual = self._folio_actual.get(tipo_dte)
        if actual is not None and folio < actual:
            raise ValueError(
                f"No se puede retroceder: folio actual={actual}, solicitado={folio}. "
                f"Retroceder causaría reutilización de folios ya consumidos."
            )
        self._folio_actual[tipo_dte] = folio
        self._persistir_folios()

    def registrar_caf(self, xml_bytes: bytes, filename: str) -> dict:
        """Registra un nuevo archivo CAF desde bytes.

        Guarda el archivo en el directorio de CAFs y lo carga. Valida
        vigencia (<=2 años) y firma FRMA (si hay llave pública SII
        configurada vía env ``SII_PUBLIC_KEY_PATH``).

        Retorna info del CAF registrado con lista de advertencias.
        """
        # Guardar archivo
        dest = self.caf_dir / filename
        if dest.exists():
            raise ValueError(f"Ya existe un archivo con ese nombre: {filename}")
        dest.write_bytes(xml_bytes)

        # Cargar
        try:
            caf = CAF(dest)
        except Exception as e:
            dest.unlink()  # Borrar si no se pudo parsear
            raise ValueError(f"Error parseando CAF: {e}")

        # Validar vigencia y firma FRMA
        # Tipos boleta (39/41) tienen vigencia de 1 año, el resto 2 años
        max_dias = 365 if caf.tipo_dte in (39, 41) else CAF_VIGENCIA_MAX_DIAS
        errores = caf.validar(
            llave_publica_sii=self._llave_publica_sii,
            max_dias_vigencia=max_dias,
        )
        # Cualquier error bloquea el registro (firma inválida, CAF vencido)
        if errores:
            dest.unlink()
            raise ValueError(
                "CAF rechazado por validación SII:\n- " + "\n- ".join(errores)
            )

        # Agregar al manager
        if caf.tipo_dte not in self._cafs:
            self._cafs[caf.tipo_dte] = []

        existing = self._cafs[caf.tipo_dte]
        replaced = False
        for i, old in enumerate(existing):
            if old.folio_desde == caf.folio_desde and old.folio_hasta == caf.folio_hasta:
                existing[i] = caf
                replaced = True
                break
        if not replaced:
            existing.append(caf)

        # Inicializar folio si no existe para este tipo
        if caf.tipo_dte not in self._folio_actual:
            self._folio_actual[caf.tipo_dte] = caf.folio_desde
            self._persistir_folios()

        return {
            "tipo_dte": caf.tipo_dte,
            "folio_desde": caf.folio_desde,
            "folio_hasta": caf.folio_hasta,
            "rut_emisor": caf.rut_emisor,
            "fecha_autorizacion": caf.fecha_autorizacion,
            "archivo": filename,
            "reemplazo": replaced,
        }

    def info(self):
        """Muestra información de los CAFs cargados."""
        for tipo, cafs in sorted(self._cafs.items()):
            for caf in cafs:
                print(f"  Tipo {tipo}: folios {caf.folio_desde}-{caf.folio_hasta}")
