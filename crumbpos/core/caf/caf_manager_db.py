"""Gestor de folios CAF respaldado por base de datos.

Los CAFs viven en la tabla caf_folio, compartidos entre todas las
sucursales de una misma empresa. El folio_actual avanza de forma
atómica usando SELECT ... FOR UPDATE (PostgreSQL) o transacciones
serializadas (SQLite).
"""
import re
from lxml import etree
from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from crumbpos.db.models import CafFolio
from crumbpos.core.caf.caf_manager import CAF


class CAFManagerDB:
    """Gestiona folios CAF desde la base de datos."""

    def __init__(self, db: Session, empresa_id: str):
        self.db = db
        self.empresa_id = empresa_id

    # ── Consultas ──

    def estado_folios(self) -> list[dict]:
        """Estado detallado de todos los folios de la empresa."""
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

        rows = self.db.execute(
            select(CafFolio)
            .where(CafFolio.empresa_id == self.empresa_id)
            .order_by(CafFolio.tipo_dte, CafFolio.rango_desde)
        ).scalars().all()

        # Agrupar por tipo
        por_tipo: dict[int, list[CafFolio]] = {}
        for r in rows:
            por_tipo.setdefault(r.tipo_dte, []).append(r)

        resultado = []
        for tipo in sorted(por_tipo.keys()):
            cafs = por_tipo[tipo]
            folio_min = min(c.rango_desde for c in cafs)
            folio_max = max(c.rango_hasta for c in cafs)
            total_autorizados = sum(c.rango_hasta - c.rango_desde + 1 for c in cafs)

            # El folio_actual del CAF activo es el próximo a usar
            activos = [c for c in cafs if c.estado == "activo"]
            if activos:
                caf_activo = activos[0]  # El primero activo por rango
                folio_actual = caf_activo.folio_actual
            else:
                folio_actual = folio_max + 1  # Todo agotado

            # Disponibles reales
            disponibles = 0
            for c in cafs:
                if c.estado == "activo":
                    inicio = max(c.rango_desde, c.folio_actual)
                    if inicio <= c.rango_hasta:
                        disponibles += c.rango_hasta - inicio + 1

            consumidos = sum(
                max(0, min(c.folio_actual, c.rango_hasta + 1) - c.rango_desde)
                for c in cafs
            )
            pct_usado = round(consumidos / total_autorizados * 100, 1) if total_autorizados > 0 else 0

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

            rangos = []
            for c in cafs:
                usados = max(0, min(c.folio_actual, c.rango_hasta + 1) - c.rango_desde)
                rangos.append({
                    "id": c.id,
                    "desde": c.rango_desde,
                    "hasta": c.rango_hasta,
                    "total": c.rango_hasta - c.rango_desde + 1,
                    "usados": usados,
                    "folio_actual": c.folio_actual,
                    "estado": c.estado,
                    "fecha_autorizacion": c.fecha_autorizacion or "",
                })

            resultado.append({
                "tipo_dte": tipo,
                "nombre": NOMBRES_DTE.get(tipo, f"Tipo {tipo}"),
                "folio_actual": folio_actual,
                "folio_min": folio_min,
                "folio_max": folio_max,
                "disponibles": disponibles,
                "consumidos": consumidos,
                "total_autorizados": total_autorizados,
                "pct_usado": pct_usado,
                "alerta": alerta,
                "rangos": rangos,
            })

        return resultado

    def siguiente_folio(self, tipo_dte: int) -> tuple[int, "CAF"]:
        """Obtiene el siguiente folio disponible y el CAF asociado.

        Avanza folio_actual atómicamente en la DB.
        Retorna (folio, caf_object).
        """
        # Buscar CAF activo para este tipo
        caf_row = self.db.execute(
            select(CafFolio)
            .where(and_(
                CafFolio.empresa_id == self.empresa_id,
                CafFolio.tipo_dte == tipo_dte,
                CafFolio.estado == "activo",
            ))
            .order_by(CafFolio.rango_desde)
            .with_for_update()
        ).scalars().first()

        if not caf_row:
            raise ValueError(
                f"No hay folios disponibles para tipo DTE {tipo_dte}. "
                f"Suba nuevos CAFs desde el panel de gestión."
            )

        folio = caf_row.folio_actual

        if folio > caf_row.rango_hasta:
            # Este CAF está agotado, marcarlo y buscar siguiente
            caf_row.estado = "agotado"
            self.db.flush()
            # Intentar siguiente CAF
            return self.siguiente_folio(tipo_dte)

        # Avanzar folio
        caf_row.folio_actual = folio + 1

        # Si se agotó, marcar
        if caf_row.folio_actual > caf_row.rango_hasta:
            caf_row.estado = "agotado"

        self.db.flush()

        # Construir objeto CAF desde el XML almacenado
        caf_obj = self._caf_from_row(caf_row)

        return folio, caf_obj

    def obtener_caf(self, tipo_dte: int, folio: int) -> "CAF | None":
        """Obtiene el CAF que contiene un folio específico."""
        row = self.db.execute(
            select(CafFolio)
            .where(and_(
                CafFolio.empresa_id == self.empresa_id,
                CafFolio.tipo_dte == tipo_dte,
                CafFolio.rango_desde <= folio,
                CafFolio.rango_hasta >= folio,
            ))
        ).scalars().first()

        if not row:
            return None
        return self._caf_from_row(row)

    def set_folio(self, tipo_dte: int, folio: int):
        """Establece manualmente el próximo folio. Solo permite avanzar.

        Busca el CAF que contiene el folio solicitado, verifica que no
        retroceda DENTRO de ese rango, marca rangos anteriores como agotados
        y deja rangos posteriores intactos.
        """
        cafs = self.db.execute(
            select(CafFolio)
            .where(and_(
                CafFolio.empresa_id == self.empresa_id,
                CafFolio.tipo_dte == tipo_dte,
            ))
            .order_by(CafFolio.rango_desde)
            .with_for_update()
        ).scalars().all()

        if not cafs:
            raise ValueError(f"No hay CAFs para tipo DTE {tipo_dte}")

        # Encontrar el CAF correcto para este folio
        target_caf = None
        for c in cafs:
            if c.rango_desde <= folio <= c.rango_hasta:
                target_caf = c
                break

        if not target_caf:
            # Puede ser que el folio esté más allá de todos los rangos
            # Marcar todos como agotados
            for c in cafs:
                if folio > c.rango_hasta:
                    c.estado = "agotado"
                    c.folio_actual = c.rango_hasta + 1
            self.db.flush()
            raise ValueError(
                f"Folio {folio} está fuera de todos los rangos CAF disponibles para tipo {tipo_dte}"
            )

        # Solo verificar retroceso dentro del CAF target
        if target_caf.folio_actual > folio and target_caf.estado != "agotado":
            raise ValueError(
                f"No se puede retroceder: CAF rango {target_caf.rango_desde}-{target_caf.rango_hasta} "
                f"ya está en folio {target_caf.folio_actual}"
            )

        # Marcar CAFs con rangos anteriores al target como agotados
        for c in cafs:
            if c.rango_hasta < folio:
                c.estado = "agotado"
                c.folio_actual = c.rango_hasta + 1

        # CAFs con rangos posteriores: resetear a su folio_desde (no usados aún)
        for c in cafs:
            if c.rango_desde > target_caf.rango_hasta:
                c.folio_actual = c.rango_desde
                c.estado = "activo"

        # Establecer folio en el CAF target
        target_caf.folio_actual = folio
        target_caf.estado = "activo"
        self.db.flush()

    def registrar_caf(self, xml_bytes: bytes) -> dict:
        """Registra un nuevo CAF desde XML crudo."""
        # Parsear XML para extraer metadata
        try:
            tree = etree.fromstring(xml_bytes)
        except Exception as e:
            raise ValueError(f"XML inválido: {e}")

        # Buscar elemento CAF (puede ser raíz AUTORIZACION > CAF)
        caf_el = tree.find(".//CAF")
        if caf_el is None:
            raise ValueError("No se encontró elemento <CAF> en el XML")

        da = caf_el.find("DA")
        if da is None:
            raise ValueError("No se encontró elemento <DA> en el CAF")

        tipo_dte = int(da.findtext("TD", "0"))
        rut_emisor = da.findtext("RE", "")
        rng = da.find("RNG")
        if rng is None:
            raise ValueError("No se encontró elemento <RNG> en el CAF")

        folio_desde = int(rng.findtext("D", "0"))
        folio_hasta = int(rng.findtext("H", "0"))
        fecha_auth = da.findtext("FA", "")

        if not tipo_dte or not folio_desde:
            raise ValueError("CAF incompleto: faltan TD, D o H")

        # Verificar que no exista un CAF con el mismo rango
        existing = self.db.execute(
            select(CafFolio)
            .where(and_(
                CafFolio.empresa_id == self.empresa_id,
                CafFolio.tipo_dte == tipo_dte,
                CafFolio.rango_desde == folio_desde,
                CafFolio.rango_hasta == folio_hasta,
            ))
        ).scalars().first()

        if existing:
            raise ValueError(
                f"Ya existe un CAF para tipo {tipo_dte} con rango {folio_desde}-{folio_hasta}"
            )

        # Crear registro
        caf_row = CafFolio(
            empresa_id=self.empresa_id,
            tipo_dte=tipo_dte,
            rango_desde=folio_desde,
            rango_hasta=folio_hasta,
            folio_actual=folio_desde,
            caf_xml_raw=xml_bytes.decode("ISO-8859-1"),
            rut_emisor=rut_emisor,
            fecha_autorizacion=fecha_auth,
            estado="activo",
        )
        self.db.add(caf_row)
        self.db.flush()

        return {
            "id": caf_row.id,
            "tipo_dte": tipo_dte,
            "folio_desde": folio_desde,
            "folio_hasta": folio_hasta,
            "rut_emisor": rut_emisor,
            "fecha_autorizacion": fecha_auth,
        }

    def _caf_from_row(self, row: CafFolio) -> "CAF":
        """Construye un objeto CAF desde un registro de DB.

        Escribe temporalmente el XML a un archivo para reutilizar
        el parser existente (que necesita leer el archivo raw para
        preservar bytes exactos de la firma FRMA).
        """
        import tempfile
        import os

        xml_data = row.caf_xml_raw
        if isinstance(xml_data, str):
            xml_data = xml_data.encode("ISO-8859-1")

        # Escribir a archivo temporal
        fd, tmp_path = tempfile.mkstemp(suffix=".xml")
        try:
            os.write(fd, xml_data)
            os.close(fd)
            return CAF(tmp_path)
        finally:
            # No borrar inmediatamente — el CAF puede necesitar releer
            # Se limpia cuando el objeto CAF se deje de usar
            pass
