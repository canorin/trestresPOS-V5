"""Parser y gestor de folios CAF (Código de Autorización de Folios).

Los folios consumidos se persisten en un archivo JSON para sobrevivir
reinicios del servidor. Nunca se reasigna un folio ya consumido.
"""
import json
from pathlib import Path
from lxml import etree
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.backends import default_backend


class CAF:
    """Representa un archivo CAF del SII."""

    def __init__(self, xml_path: str | Path):
        self.xml_path = Path(xml_path)
        self._tree = etree.parse(str(self.xml_path))
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
        import re as _re
        with open(str(self.xml_path), "rb") as _f:
            _raw = _f.read()
        _m = _re.search(rb'<CAF version.*?</CAF>', _raw, _re.DOTALL)
        self.caf_xml_raw = _m.group(0) if _m else b""
        # Versión string para compatibilidad (ISO-8859-1)
        self.caf_xml = self.caf_xml_raw.decode("ISO-8859-1") if self.caf_xml_raw else etree.tostring(caf, encoding="unicode")

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

    def __repr__(self):
        return (
            f"CAF(tipo={self.tipo_dte}, "
            f"folios={self.folio_desde}-{self.folio_hasta}, "
            f"rut={self.rut_emisor})"
        )


class CAFManager:
    """Gestiona múltiples archivos CAF."""

    def __init__(self, caf_dir: str | Path):
        self.caf_dir = Path(caf_dir)
        self._cafs: dict[int, list[CAF]] = {}
        self._folio_actual: dict[int, int] = {}
        self._folios_file = self.caf_dir / "folios_consumidos.json"
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

        Guarda el archivo en el directorio de CAFs y lo carga.
        Retorna info del CAF registrado.
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
