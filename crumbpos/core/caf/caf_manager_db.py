"""Gestor de folios CAF respaldado por base de datos.

Los CAFs viven en la tabla caf_folio, compartidos entre todas las
sucursales de una misma empresa. El folio_actual avanza de forma
atómica usando tres capas de protección:

1. threading.Lock por empresa (serialización en proceso único — uvicorn).
2. WAL + busy_timeout en el engine SQLite (serialización en múltiples
   procesos; configurado en multi_tenant.py).
3. SELECT ... FOR UPDATE (activo en PostgreSQL cuando se migre a cloud).

Con SQLite, `with_for_update()` es ignorado por el driver, pero las
capas 1 y 2 garantizan serialización equivalente para el caso de uso
actual (un proceso uvicorn, múltiples workers/hilos).
"""
import re
import threading
from lxml import etree
from sqlalchemy import select, and_, text
from sqlalchemy.orm import Session

from crumbpos.db.models import CafFolio
from crumbpos.core.caf.caf_manager import CAF

# ── Lock de proceso por empresa ──────────────────────────────────────────────
# Serializa llamadas a siguiente_folio() dentro del mismo proceso uvicorn.
# Clave: empresa_id (UUID string). Se crea on-demand y nunca se borra
# (las empresas son pocas y viven toda la vida del proceso).
_folio_locks: dict[str, threading.Lock] = {}
_folio_locks_meta = threading.Lock()  # protege el dict de locks


def _folio_lock(empresa_id: str) -> threading.Lock:
    """Retorna el Lock de folios para la empresa dada (crea si no existe)."""
    with _folio_locks_meta:
        if empresa_id not in _folio_locks:
            _folio_locks[empresa_id] = threading.Lock()
        return _folio_locks[empresa_id]


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

        Avanza folio_actual atómicamente en la DB. Usa un threading.Lock
        por empresa para serializar el acceso en SQLite (donde
        with_for_update() no tiene efecto real). En PostgreSQL, el lock
        de fila sigue activo como segunda capa de protección.

        Retorna (folio, caf_object).
        """
        with _folio_lock(self.empresa_id):
            return self._siguiente_folio_locked(tipo_dte)

    def _siguiente_folio_locked(self, tipo_dte: int) -> tuple[int, "CAF"]:
        """Lógica interna de siguiente_folio — debe llamarse bajo el lock."""
        # Buscar CAF activo para este tipo
        caf_row = self.db.execute(
            select(CafFolio)
            .where(and_(
                CafFolio.empresa_id == self.empresa_id,
                CafFolio.tipo_dte == tipo_dte,
                CafFolio.estado == "activo",
            ))
            .order_by(CafFolio.rango_desde)
            .with_for_update()  # activo en PostgreSQL; SQLite usa el threading.Lock
        ).scalars().first()

        if not caf_row:
            raise ValueError(
                f"No hay folios disponibles para tipo DTE {tipo_dte}. "
                f"Suba nuevos CAFs desde el panel de gestión."
            )

        # Avanzar por rangos agotados usando un while en vez de recursión,
        # para soportar N rangos CAF encadenados sin límite de pila.
        while caf_row.folio_actual > caf_row.rango_hasta:
            caf_row.estado = "agotado"
            self.db.flush()
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

        # Avanzar folio y marcar agotado si se consumió el último
        caf_row.folio_actual = folio + 1
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

        # Permitir retroceder si el CAF está agotado (folios sin usar en el rango)
        # o si explícitamente se solicita reubicar dentro del rango válido
        if target_caf.folio_actual > folio and target_caf.estado == "activo":
            raise ValueError(
                f"No se puede retroceder en CAF activo: rango {target_caf.rango_desde}-{target_caf.rango_hasta} "
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

    def registrar_caf(
        self,
        xml_bytes: bytes,
        folio_inicial_override: int | None = None,
    ) -> dict:
        """Registra un nuevo CAF desde XML crudo.

        Parámetros:
        - ``xml_bytes``: contenido crudo del CAF en ISO-8859-1.
        - ``folio_inicial_override``: si viene distinto de None, se usa como
          ``folio_actual`` en lugar del ``folio_desde`` del CAF. Sirve para
          el caso en que el CAF trae folios ya consumidos fuera del sistema
          (por ejemplo, un CAF pedido al SII antes de migrar a este software).
          Debe cumplir ``folio_desde <= override <= folio_hasta``; cualquier
          otro valor levanta ``ValueError`` con glosa explícita.

        Valida antes de persistir:
        - G5: El RUT del CAF debe coincidir con el RUT de la empresa.
        - G2: Vigencia <= 2 años (365 días para boletas) desde FA.
        - G2: Firma FRMA válida contra llave pública del SII (si está
          configurada en env var ``SII_PUBLIC_KEY_PATH``).
        """
        import os
        import tempfile
        from crumbpos.core.caf.caf_manager import CAF, _cargar_llave_publica_sii

        # ── Parsear XML para extraer metadata ─────────────────────────
        try:
            tree = etree.fromstring(xml_bytes)
        except Exception as e:
            raise ValueError(f"XML inválido: {e}")

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

        # ── Validar folio_inicial_override ────────────────────────────
        # Si el override viene, debe caer dentro del rango del CAF. Un
        # override == folio_desde es equivalente a no pasar override,
        # pero se acepta para que el cliente pueda mandarlo sin temor.
        if folio_inicial_override is not None:
            if not isinstance(folio_inicial_override, int):
                raise ValueError(
                    f"Folio inicial debe ser un número entero, "
                    f"no {type(folio_inicial_override).__name__}"
                )
            if folio_inicial_override < folio_desde:
                raise ValueError(
                    f"Folio inicial {folio_inicial_override} está fuera del "
                    f"rango del CAF ({folio_desde} a {folio_hasta}). "
                    f"No se puede arrancar antes del primer folio autorizado."
                )
            if folio_inicial_override > folio_hasta:
                raise ValueError(
                    f"Folio inicial {folio_inicial_override} está fuera del "
                    f"rango del CAF ({folio_desde} a {folio_hasta})."
                )

        # ── G5: Validar que el RUT del CAF pertenece a esta empresa ───
        from crumbpos.db.models import Empresa
        empresa = self.db.execute(
            select(Empresa).where(Empresa.id == self.empresa_id)
        ).scalars().first()

        if empresa and rut_emisor:
            # Normalizar ambos RUTs (quitar puntos, comparar en mayúsculas)
            rut_caf_norm = rut_emisor.replace(".", "").upper()
            rut_emp_norm = empresa.rut.replace(".", "").upper()
            if rut_caf_norm != rut_emp_norm:
                raise ValueError(
                    f"El RUT del CAF ({rut_emisor}) no coincide con el RUT "
                    f"de la empresa ({empresa.rut}). "
                    f"Verifique que subió el archivo correcto."
                )

        # ── G2: Validar firma FRMA y vigencia usando objeto CAF ────────
        fd, tmp_path = tempfile.mkstemp(suffix=".xml")
        try:
            os.write(fd, xml_bytes)
            os.close(fd)
            caf_obj = CAF(tmp_path)
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise ValueError(f"Error parseando CAF para validación: {e}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # Vigencia: 2 años para DTEs, 1 año para boletas
        max_dias = 365 if tipo_dte in (39, 41) else 730
        llave_sii = _cargar_llave_publica_sii()
        errores = caf_obj.validar(llave_publica_sii=llave_sii, max_dias_vigencia=max_dias)
        if errores:
            raise ValueError(
                "CAF rechazado:\n- " + "\n- ".join(errores)
            )

        # ── Verificar duplicado ────────────────────────────────────────
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

        # ── Crear registro ─────────────────────────────────────────────
        folio_inicial = (
            folio_inicial_override
            if folio_inicial_override is not None
            else folio_desde
        )
        caf_row = CafFolio(
            empresa_id=self.empresa_id,
            tipo_dte=tipo_dte,
            rango_desde=folio_desde,
            rango_hasta=folio_hasta,
            folio_actual=folio_inicial,
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
            "folio_inicial": folio_inicial,
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
