"""Gestor de folios CAF respaldado por base de datos.

Modelo de subdivisión por sucursal
==================================

Un CAF (``caf_folio``) representa un rango de folios autorizado por el SII
y nunca se subdivide a nivel SII. Internamente sí: cada CAF se descompone
en uno o más **tramos** (``caf_asignacion``) que el master cliente asigna
a sucursales o al **pool del server** (``sucursal_id IS NULL``).

Reglas:

  - **Pool-first.** Toda subida nueva crea automáticamente un tramo único
    cubriendo todo el rango con ``sucursal_id=NULL``. Desde ahí el master
    puede subdividir y reasignar.
  - **Cobertura total.** La unión de los tramos de un CAF cubre todo su
    rango sin solapes. Cualquier folio sin asignar explícitamente cae al
    pool.
  - **Inmutabilidad de folios consumidos.** Un folio ya emitido no se
    puede mover de tramo. La reasignación solo afecta a folios frescos
    (``rango_desde >= folio_actual`` del tramo previo).
  - **Sin fallback automático.** ``siguiente_folio(tipo, sucursal_id=X)``
    busca solo en los tramos de la sucursal X. Si no hay folios, levanta
    ``ValueError`` con glosa explícita; el caller decide si pedir
    confirmación al master para consumir de otra sucursal.

Concurrencia
============

El folio_actual de cada tramo avanza atómicamente con tres capas:

  1. ``threading.Lock`` por empresa (serialización en proceso uvicorn).
  2. WAL + busy_timeout en el engine SQLite (multi-proceso).
  3. ``SELECT ... FOR UPDATE`` (activo en PostgreSQL).
"""
import re
import threading
import uuid as _uuid
from lxml import etree
from sqlalchemy import select, and_, text, or_, func
from sqlalchemy.orm import Session

from crumbpos.core.security.xml_safe import fromstring_safe

from crumbpos.db.models import (
    CafFolio,
    CafAsignacion,
    CafEventoSync,
    Sucursal,
)
from crumbpos.core.caf.caf_manager import CAF

# ── Lock de proceso por empresa ──────────────────────────────────────────────
_folio_locks: dict[str, threading.Lock] = {}
_folio_locks_meta = threading.Lock()


def _folio_lock(empresa_id: str) -> threading.Lock:
    """Retorna el Lock de folios para la empresa dada (crea si no existe)."""
    with _folio_locks_meta:
        if empresa_id not in _folio_locks:
            _folio_locks[empresa_id] = threading.Lock()
        return _folio_locks[empresa_id]


# ── Excepción específica para "pool/sucursal sin folios" ────────────────────
class FoliosAgotadosError(ValueError):
    """No hay folios disponibles en el slice solicitado.

    El caller (router de emisión) puede capturarla para devolver un 409
    estructurado con la lista de sucursales que sí tienen stock, dejando
    al UI ofrecer la confirmación explícita de consumo desde otra sucursal.
    """

    def __init__(
        self,
        tipo_dte: int,
        sucursal_id: str | None,
        sucursales_con_stock: list[dict] | None = None,
    ):
        self.tipo_dte = tipo_dte
        self.sucursal_id = sucursal_id
        self.sucursales_con_stock = sucursales_con_stock or []
        scope = (
            "pool del server" if sucursal_id is None
            else f"sucursal {sucursal_id}"
        )
        super().__init__(
            f"No hay folios disponibles para tipo DTE {tipo_dte} en el "
            f"{scope}. Suba un nuevo CAF o reasigne tramos desde el módulo CAF."
        )


class CAFManagerDB:
    """Gestiona folios CAF desde la base de datos."""

    def __init__(self, db: Session, empresa_id: str):
        self.db = db
        self.empresa_id = empresa_id

    # ══════════════════════════════════════════════════════════════════
    # CONSULTAS — estado y listados
    # ══════════════════════════════════════════════════════════════════

    def estado_folios(self) -> list[dict]:
        """Estado detallado de todos los folios de la empresa.

        Devuelve un dict por tipo DTE con métricas agregadas + la lista
        completa de CAFs (con sus asignaciones) para que la UI pinte
        cards y resúmenes de tramos.

        El "próximo folio" reportado es el menor ``folio_actual`` entre
        los tramos activos del pool del server (si los hay). Si el pool
        está vacío, se reporta el menor de cualquier sucursal — es solo
        un indicador para el master, no afecta al consumo real.
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

        cafs = self.db.execute(
            select(CafFolio)
            .where(CafFolio.empresa_id == self.empresa_id)
            .order_by(CafFolio.tipo_dte, CafFolio.rango_desde)
        ).scalars().all()

        # Map de sucursales para rotularlas en el output
        sucursales_map = {
            s.id: s.nombre
            for s in self.db.execute(
                select(Sucursal).where(Sucursal.empresa_id == self.empresa_id)
            ).scalars().all()
        }

        por_tipo: dict[int, list[CafFolio]] = {}
        for c in cafs:
            por_tipo.setdefault(c.tipo_dte, []).append(c)

        resultado = []
        for tipo in sorted(por_tipo.keys()):
            cafs_tipo = por_tipo[tipo]
            folio_min = min(c.rango_desde for c in cafs_tipo)
            folio_max = max(c.rango_hasta for c in cafs_tipo)
            total_autorizados = sum(
                c.rango_hasta - c.rango_desde + 1 for c in cafs_tipo
            )

            disponibles = 0
            consumidos = 0
            # Próximo folio "global" = menor folio_actual del pool con
            # tramos activos. Si no hay pool con stock, el menor de
            # cualquier sucursal.
            siguiente_pool: int | None = None
            siguiente_cualquiera: int | None = None

            rangos_out = []
            for c in cafs_tipo:
                tramos_out = []
                for a in sorted(c.asignaciones, key=lambda x: x.rango_desde):
                    folios_total = a.rango_hasta - a.rango_desde + 1
                    folios_consumidos = max(
                        0, min(a.folio_actual, a.rango_hasta + 1) - a.rango_desde
                    )
                    folios_disponibles = max(
                        0, a.rango_hasta - max(a.folio_actual, a.rango_desde) + 1
                    )
                    if a.estado == "agotado":
                        folios_disponibles = 0
                    consumidos += folios_consumidos
                    disponibles += folios_disponibles

                    if a.estado == "activo" and folios_disponibles > 0:
                        if a.sucursal_id is None and (
                            siguiente_pool is None
                            or a.folio_actual < siguiente_pool
                        ):
                            siguiente_pool = a.folio_actual
                        if (
                            siguiente_cualquiera is None
                            or a.folio_actual < siguiente_cualquiera
                        ):
                            siguiente_cualquiera = a.folio_actual

                    tramos_out.append({
                        "id": a.id,
                        "sucursal_id": a.sucursal_id,
                        "sucursal_nombre": (
                            sucursales_map.get(a.sucursal_id)
                            if a.sucursal_id else None
                        ),
                        "rango_desde": a.rango_desde,
                        "rango_hasta": a.rango_hasta,
                        "folio_actual": a.folio_actual,
                        "estado": a.estado,
                        "consumidos": folios_consumidos,
                        "disponibles": folios_disponibles,
                        "total": folios_total,
                    })

                rangos_out.append({
                    "id": c.id,
                    "desde": c.rango_desde,
                    "hasta": c.rango_hasta,
                    "total": c.rango_hasta - c.rango_desde + 1,
                    "estado": c.estado,
                    "fecha_autorizacion": c.fecha_autorizacion or "",
                    "tramos": tramos_out,
                    # Compatibilidad con UI legacy: campos planos del primer
                    # tramo. Se irán retirando cuando la UI nueva esté lista.
                    "usados": sum(t["consumidos"] for t in tramos_out),
                    "folio_actual": (
                        min((t["folio_actual"] for t in tramos_out), default=c.folio_actual)
                    ),
                    "sucursal_id": (
                        tramos_out[0]["sucursal_id"]
                        if len(tramos_out) == 1 else None
                    ),
                    "subdividido": len(tramos_out) > 1,
                })

            folio_actual = (
                siguiente_pool
                if siguiente_pool is not None
                else (
                    siguiente_cualquiera
                    if siguiente_cualquiera is not None
                    else folio_max + 1
                )
            )

            pct_usado = round(
                consumidos / total_autorizados * 100, 1
            ) if total_autorizados > 0 else 0

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
                "folio_min": folio_min,
                "folio_max": folio_max,
                "disponibles": disponibles,
                "consumidos": consumidos,
                "total_autorizados": total_autorizados,
                "pct_usado": pct_usado,
                "alerta": alerta,
                "rangos": rangos_out,
            })

        return resultado

    def listar_asignaciones(self, caf_id: str) -> list[dict]:
        """Lista los tramos de un CAF para la UI de Configurar destinos."""
        caf = self.db.execute(
            select(CafFolio).where(and_(
                CafFolio.id == caf_id,
                CafFolio.empresa_id == self.empresa_id,
            ))
        ).scalars().first()
        if not caf:
            raise ValueError(f"CAF {caf_id} no encontrado")
        return [
            {
                "id": a.id,
                "sucursal_id": a.sucursal_id,
                "rango_desde": a.rango_desde,
                "rango_hasta": a.rango_hasta,
                "folio_actual": a.folio_actual,
                "estado": a.estado,
            }
            for a in sorted(caf.asignaciones, key=lambda x: x.rango_desde)
        ]

    # ══════════════════════════════════════════════════════════════════
    # CONSUMO — siguiente_folio
    # ══════════════════════════════════════════════════════════════════

    def siguiente_folio(
        self,
        tipo_dte: int,
        sucursal_id: str | None = None,
    ) -> tuple[int, "CAF"]:
        """Obtiene el siguiente folio del slice solicitado.

        ``sucursal_id=None`` → consume del pool del server (tramos con
        ``sucursal_id IS NULL``). Es el flujo por defecto cuando el master
        emite desde la consola.

        ``sucursal_id=X`` → consume del slice exclusivo de la sucursal X.
        Lo usa el POS de esa sucursal.

        **No hay fallback automático**: si el slice solicitado no tiene
        folios disponibles, se levanta ``FoliosAgotadosError``. La emisión
        desde el server con pool vacío debe pedir confirmación al master
        antes de consumir de un slice de sucursal (el router se encarga
        de esa coreografía y, si decide consumir, llama de nuevo con el
        ``sucursal_id`` explícito).
        """
        # C1: filtrar CAFs por ambiente de la empresa para prevenir uso
        # de CAFs de certificación en producción y viceversa.
        from crumbpos.db.models import Empresa
        empresa_row = self.db.execute(
            select(Empresa).where(Empresa.id == self.empresa_id)
        ).scalars().first()
        ambiente = empresa_row.ambiente_sii if empresa_row else "certificacion"

        with _folio_lock(self.empresa_id):
            return self._siguiente_folio_locked(tipo_dte, sucursal_id, ambiente)

    def _siguiente_folio_locked(
        self,
        tipo_dte: int,
        sucursal_id: str | None,
        ambiente: str = "certificacion",
    ) -> tuple[int, "CAF"]:
        """Lógica interna de siguiente_folio — debe llamarse bajo el lock."""
        sucursal_filter = (
            CafAsignacion.sucursal_id.is_(None)
            if sucursal_id is None
            else CafAsignacion.sucursal_id == sucursal_id
        )

        def _buscar_tramo() -> CafAsignacion | None:
            return self.db.execute(
                select(CafAsignacion)
                .join(CafFolio, CafAsignacion.caf_id == CafFolio.id)
                .where(and_(
                    CafFolio.empresa_id == self.empresa_id,
                    CafFolio.tipo_dte == tipo_dte,
                    CafFolio.estado == "activo",
                    CafFolio.ambiente == ambiente,  # C1: aísla cert vs producción
                    CafAsignacion.estado == "activo",
                    sucursal_filter,
                ))
                .order_by(CafFolio.created_at, CafAsignacion.rango_desde)
                .with_for_update()
            ).scalars().first()

        asig = _buscar_tramo()
        if asig is None:
            raise FoliosAgotadosError(tipo_dte, sucursal_id)

        # Avanzar por tramos consumidos hasta encontrar uno con folios.
        while asig.folio_actual > asig.rango_hasta:
            asig.estado = "agotado"
            self._marcar_caf_si_agotado(asig.caf_id)
            self.db.flush()
            asig = _buscar_tramo()
            if asig is None:
                raise FoliosAgotadosError(tipo_dte, sucursal_id)

        folio = asig.folio_actual
        asig.folio_actual = folio + 1
        if asig.folio_actual > asig.rango_hasta:
            asig.estado = "agotado"
            self._marcar_caf_si_agotado(asig.caf_id)

        # Mantener caf_folio.folio_actual (legacy) en sincronía con la
        # asignación más avanzada del CAF, para que reportes históricos
        # y migraciones futuras sigan teniendo un valor coherente.
        caf_row = asig.caf
        if folio + 1 > caf_row.folio_actual:
            caf_row.folio_actual = folio + 1

        self.db.flush()

        return folio, self._caf_from_row(caf_row)

    def _marcar_caf_si_agotado(self, caf_id: str) -> None:
        """Marca el CAF padre como 'agotado' si todos sus tramos lo están."""
        sigue_activa = self.db.execute(
            select(func.count(CafAsignacion.id))
            .where(and_(
                CafAsignacion.caf_id == caf_id,
                CafAsignacion.estado == "activo",
            ))
        ).scalar()
        if not sigue_activa:
            caf_row = self.db.execute(
                select(CafFolio).where(CafFolio.id == caf_id)
            ).scalars().first()
            if caf_row and caf_row.estado != "agotado":
                caf_row.estado = "agotado"

    # ══════════════════════════════════════════════════════════════════
    # CONFIGURAR ASIGNACIONES — el master subdivide un CAF
    # ══════════════════════════════════════════════════════════════════

    def configurar_asignaciones(
        self,
        caf_id: str,
        tramos: list[dict],
        actor_user_id: str | None = None,
    ) -> list[dict]:
        """Reescribe los tramos de un CAF.

        Cada tramo es un dict con ``sucursal_id`` (None | str), ``rango_desde``
        (int) y ``rango_hasta`` (int). El método valida y aplica
        atómicamente:

          1. Todos los tramos están dentro del rango del CAF.
          2. No hay solapes ni rangos invertidos.
          3. Folios ya consumidos (``< folio_actual_previo`` de su tramo
             original) no cambian de sucursal — solo se aceptan tramos
             nuevos que cubran esos folios manteniendo el mismo dueño.
          4. Los huecos no cubiertos se rellenan automáticamente con
             tramos pool (``sucursal_id=NULL``).

        Side effects:
          - Inserta ``CafEventoSync`` por cada sucursal afectada (la que
            pierde folios y la que los gana), para que el POS sincronice
            su cache al volver online.

        Devuelve la lista de tramos resultantes (incluidos los pool de
        relleno) tal cual quedaron en DB.
        """
        with _folio_lock(self.empresa_id):
            return self._configurar_asignaciones_locked(
                caf_id, tramos, actor_user_id,
            )

    def _configurar_asignaciones_locked(
        self,
        caf_id: str,
        tramos: list[dict],
        actor_user_id: str | None,
    ) -> list[dict]:
        # ── 1. Cargar CAF + tramos viejos ──
        caf = self.db.execute(
            select(CafFolio)
            .where(and_(
                CafFolio.id == caf_id,
                CafFolio.empresa_id == self.empresa_id,
            ))
        ).scalars().first()
        if not caf:
            raise ValueError(f"CAF {caf_id} no encontrado")

        viejos = sorted(
            list(caf.asignaciones),
            key=lambda a: a.rango_desde,
        )

        # ── 2. Validar y normalizar tramos de entrada ──
        if not isinstance(tramos, list):
            raise ValueError("'tramos' debe ser una lista")

        normalizados: list[dict] = []
        for t in tramos:
            if not isinstance(t, dict):
                raise ValueError("Cada tramo debe ser un objeto")
            try:
                rd = int(t["rango_desde"])
                rh = int(t["rango_hasta"])
            except (KeyError, TypeError, ValueError):
                raise ValueError(
                    "Cada tramo requiere 'rango_desde' y 'rango_hasta' enteros"
                )
            suc_id = t.get("sucursal_id")
            if suc_id == "":
                suc_id = None
            if rd > rh:
                raise ValueError(
                    f"Tramo inválido: rango_desde ({rd}) > rango_hasta ({rh})"
                )
            if rd < caf.rango_desde or rh > caf.rango_hasta:
                raise ValueError(
                    f"Tramo {rd}-{rh} fuera del rango del CAF "
                    f"({caf.rango_desde}-{caf.rango_hasta})"
                )
            normalizados.append({
                "sucursal_id": suc_id,
                "rango_desde": rd,
                "rango_hasta": rh,
            })

        # ── 3. Validar sucursales ──
        sucursales_validas = {
            s.id: s
            for s in self.db.execute(
                select(Sucursal).where(Sucursal.empresa_id == self.empresa_id)
            ).scalars().all()
        }
        for t in normalizados:
            if t["sucursal_id"] is not None:
                suc = sucursales_validas.get(t["sucursal_id"])
                if not suc:
                    raise ValueError(
                        f"Sucursal {t['sucursal_id']} no pertenece a esta empresa"
                    )
                if not suc.activa:
                    raise ValueError(
                        f"Sucursal '{suc.nombre}' está inactiva — "
                        f"reactívala antes de asignarle folios"
                    )

        # ── 4. Detectar solapes entre tramos ingresados ──
        ordenados = sorted(normalizados, key=lambda x: x["rango_desde"])
        for i in range(1, len(ordenados)):
            if ordenados[i]["rango_desde"] <= ordenados[i - 1]["rango_hasta"]:
                raise ValueError(
                    f"Tramos solapados: "
                    f"{ordenados[i - 1]['rango_desde']}-"
                    f"{ordenados[i - 1]['rango_hasta']} y "
                    f"{ordenados[i]['rango_desde']}-{ordenados[i]['rango_hasta']}"
                )

        # ── 5. Rellenar huecos con tramos pool ──
        completos = self._rellenar_huecos(
            ordenados, caf.rango_desde, caf.rango_hasta,
        )

        # ── 6. Validar inmutabilidad de folios consumidos ──
        # Cada folio consumido (folio < folio_actual_viejo en su tramo
        # original) debe quedar en un tramo nuevo con el MISMO sucursal_id
        # que tenía antes. Tramos pool consumidos pueden moverse a otro
        # pool sin restricción (porque el dueño es el server).
        consumidos_por_sucursal: dict[int, str | None] = {}
        for v in viejos:
            for f in range(v.rango_desde, min(v.folio_actual, v.rango_hasta + 1)):
                consumidos_por_sucursal[f] = v.sucursal_id

        for t in completos:
            for f in range(t["rango_desde"], t["rango_hasta"] + 1):
                if f in consumidos_por_sucursal:
                    dueno_viejo = consumidos_por_sucursal[f]
                    if dueno_viejo != t["sucursal_id"]:
                        nombre_viejo = (
                            sucursales_validas[dueno_viejo].nombre
                            if dueno_viejo else "Pool del server"
                        )
                        nombre_nuevo = (
                            sucursales_validas[t["sucursal_id"]].nombre
                            if t["sucursal_id"] else "Pool del server"
                        )
                        raise ValueError(
                            f"Folio {f} ya fue emitido por '{nombre_viejo}' "
                            f"y no puede reasignarse a '{nombre_nuevo}'. "
                            f"Solo se pueden mover folios sin consumir."
                        )

        # ── 7. Calcular folio_actual de cada tramo nuevo ──
        # Si el tramo cubre folios consumidos, su folio_actual es el del
        # tramo viejo (preserva el avance). Si es completamente fresco,
        # folio_actual = rango_desde. Si todos los folios del tramo ya
        # estaban consumidos (caso raro: tramo completamente en zona
        # consumida), folio_actual = rango_hasta + 1 → estado='agotado'.
        for t in completos:
            consumidos_en_tramo = [
                f for f in range(t["rango_desde"], t["rango_hasta"] + 1)
                if f in consumidos_por_sucursal
            ]
            if not consumidos_en_tramo:
                t["folio_actual"] = t["rango_desde"]
                t["estado"] = "activo"
            else:
                # Posición del primer folio NO consumido en el tramo.
                # Equivale a max(folio_consumido_más_alto+1, rango_desde).
                ultimo_consumido = max(consumidos_en_tramo)
                folio_actual = ultimo_consumido + 1
                if folio_actual > t["rango_hasta"]:
                    t["folio_actual"] = t["rango_hasta"] + 1
                    t["estado"] = "agotado"
                else:
                    t["folio_actual"] = folio_actual
                    t["estado"] = "activo"

        # ── 8. Aplicar: borrar viejos, insertar nuevos, emitir eventos ──
        # Set de sucursales que quedaron afectadas (perdieron o ganaron
        # folios) para emitir eventos de sync.
        sucursales_afectadas: set[str] = set()
        for v in viejos:
            if v.sucursal_id:
                sucursales_afectadas.add(v.sucursal_id)
            self.db.delete(v)
        self.db.flush()

        nuevos_rows: list[CafAsignacion] = []
        for t in completos:
            row = CafAsignacion(
                id=str(_uuid.uuid4()),
                caf_id=caf.id,
                sucursal_id=t["sucursal_id"],
                rango_desde=t["rango_desde"],
                rango_hasta=t["rango_hasta"],
                folio_actual=t["folio_actual"],
                estado=t["estado"],
            )
            self.db.add(row)
            nuevos_rows.append(row)
            if t["sucursal_id"]:
                sucursales_afectadas.add(t["sucursal_id"])
        self.db.flush()

        # Recalcular estado del CAF padre (puede haber pasado a agotado o
        # vuelto a activo si la reasignación abrió tramos disponibles).
        algun_activo = any(r.estado == "activo" for r in nuevos_rows)
        caf.estado = "activo" if algun_activo else "agotado"

        # Emitir un evento por sucursal afectada. El payload lleva el
        # rango total de tramos que la sucursal tiene ahora, para que el
        # POS pueda reescribir su cache al recibirlo.
        for suc_id in sucursales_afectadas:
            tramos_de_la_sucursal = [
                {
                    "rango_desde": r.rango_desde,
                    "rango_hasta": r.rango_hasta,
                    "folio_actual": r.folio_actual,
                    "estado": r.estado,
                }
                for r in nuevos_rows if r.sucursal_id == suc_id
            ]
            self.db.add(CafEventoSync(
                sucursal_id=suc_id,
                caf_id=caf.id,
                asignacion_id=None,
                tipo_evento="asignacion_modificada",
                payload={
                    "tipo_dte": caf.tipo_dte,
                    "tramos": tramos_de_la_sucursal,
                    "actor_user_id": actor_user_id,
                },
            ))
        self.db.flush()

        return [
            {
                "id": r.id,
                "sucursal_id": r.sucursal_id,
                "rango_desde": r.rango_desde,
                "rango_hasta": r.rango_hasta,
                "folio_actual": r.folio_actual,
                "estado": r.estado,
            }
            for r in sorted(nuevos_rows, key=lambda x: x.rango_desde)
        ]

    @staticmethod
    def _rellenar_huecos(
        tramos: list[dict],
        rango_desde: int,
        rango_hasta: int,
    ) -> list[dict]:
        """Rellena huecos entre tramos con tramos pool (sucursal_id=None).

        Asume que ``tramos`` viene ordenado por ``rango_desde`` y sin solapes
        (ya validado). Garantiza cobertura total ``[rango_desde, rango_hasta]``.
        """
        completos: list[dict] = []
        cursor = rango_desde
        for t in tramos:
            if t["rango_desde"] > cursor:
                completos.append({
                    "sucursal_id": None,
                    "rango_desde": cursor,
                    "rango_hasta": t["rango_desde"] - 1,
                })
            completos.append(t)
            cursor = t["rango_hasta"] + 1
        if cursor <= rango_hasta:
            completos.append({
                "sucursal_id": None,
                "rango_desde": cursor,
                "rango_hasta": rango_hasta,
            })
        return completos

    # ══════════════════════════════════════════════════════════════════
    # OBTENER CAF de un folio específico (para firma TED en re-emisiones)
    # ══════════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════════
    # SET FOLIO MANUAL — solo permitido si el CAF no está subdividido
    # ══════════════════════════════════════════════════════════════════

    def set_folio(self, tipo_dte: int, folio: int):
        """Establece manualmente el próximo folio para un tipo DTE.

        Solo opera sobre CAFs cuya única asignación es el pool del server
        (caso típico: CAF recién subido sin subdividir, o CAF migrado
        donde el master quiere ajustar el folio inicial). Si el master
        ya subdividió el CAF en tramos por sucursal, este método rechaza
        la operación y le pide reasignar primero los tramos al pool —
        mover folios entre sucursales con un cambio masivo de
        ``folio_actual`` puede romper la inmutabilidad de folios consumidos.

        Solo permite avanzar (no retroceder) dentro del CAF activo.
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

        # Encontrar el CAF que contiene el folio
        target_caf = None
        for c in cafs:
            if c.rango_desde <= folio <= c.rango_hasta:
                target_caf = c
                break
        if not target_caf:
            for c in cafs:
                if folio > c.rango_hasta:
                    c.estado = "agotado"
                    c.folio_actual = c.rango_hasta + 1
                    for a in c.asignaciones:
                        a.estado = "agotado"
                        a.folio_actual = a.rango_hasta + 1
            self.db.flush()
            raise ValueError(
                f"Folio {folio} fuera de todos los rangos CAF disponibles "
                f"para tipo {tipo_dte}"
            )

        # Validar: el CAF target no debe estar subdividido a sucursales
        no_pool = [a for a in target_caf.asignaciones if a.sucursal_id]
        if no_pool:
            nombres = ", ".join(
                a.sucursal.nombre for a in no_pool if a.sucursal
            ) or "una sucursal"
            raise ValueError(
                f"El CAF tipo {tipo_dte} ({target_caf.rango_desde}-"
                f"{target_caf.rango_hasta}) tiene tramos asignados a "
                f"{nombres}. Reasigna los tramos al pool antes de ajustar "
                f"el folio manualmente."
            )

        # Encontrar la asignación pool que contiene el folio
        target_asig = None
        for a in sorted(target_caf.asignaciones, key=lambda x: x.rango_desde):
            if a.rango_desde <= folio <= a.rango_hasta:
                target_asig = a
                break
        if not target_asig:
            raise ValueError(
                f"Folio {folio} no cae en ninguna asignación pool del CAF"
            )

        if target_asig.folio_actual > folio and target_asig.estado == "activo":
            raise ValueError(
                f"No se puede retroceder en CAF activo: "
                f"rango {target_asig.rango_desde}-{target_asig.rango_hasta} "
                f"ya está en folio {target_asig.folio_actual}"
            )

        # CAFs/asignaciones con rangos anteriores → marcar agotados
        for c in cafs:
            if c.rango_hasta < folio:
                c.estado = "agotado"
                c.folio_actual = c.rango_hasta + 1
                for a in c.asignaciones:
                    a.estado = "agotado"
                    a.folio_actual = a.rango_hasta + 1
            elif c.rango_desde > target_caf.rango_hasta:
                # Rangos posteriores: resetear a su rango_desde
                c.folio_actual = c.rango_desde
                c.estado = "activo"
                for a in c.asignaciones:
                    a.folio_actual = a.rango_desde
                    a.estado = "activo"

        # Asignaciones del CAF target con rango anterior al folio: agotadas
        for a in target_caf.asignaciones:
            if a.rango_hasta < folio:
                a.estado = "agotado"
                a.folio_actual = a.rango_hasta + 1
            elif a.rango_desde > folio:
                # Posteriores al target_asig: resetear
                a.folio_actual = a.rango_desde
                a.estado = "activo"

        # Aplicar al target
        target_asig.folio_actual = folio
        target_asig.estado = "activo"
        target_caf.folio_actual = folio
        target_caf.estado = "activo"
        self.db.flush()

    # ══════════════════════════════════════════════════════════════════
    # REGISTRAR CAF — sube y crea la asignación pool inicial
    # ══════════════════════════════════════════════════════════════════

    def registrar_caf(
        self,
        xml_bytes: bytes,
        folio_inicial_override: int | None = None,
    ) -> dict:
        """Registra un nuevo CAF desde XML crudo.

        Tras validar firma, vigencia y duplicados, persiste:

          1. Una fila ``caf_folio`` con el rango global y XML completo.
          2. Una fila ``caf_asignacion`` cubriendo todo el rango con
             ``sucursal_id=NULL`` (pool del server). El master cliente
             puede subdividir después desde el módulo CAF.

        Parámetros:
        - ``xml_bytes``: contenido crudo del CAF en ISO-8859-1.
        - ``folio_inicial_override``: si viene distinto de None, se usa
          como ``folio_actual`` inicial (caso migración).
        """
        import os
        import tempfile
        from crumbpos.core.caf.caf_manager import CAF, _cargar_llave_publica_sii

        # ── Parsear XML para extraer metadata ─────────────────────────
        # CAF viene del usuario (upload): parser endurecido contra XXE.
        try:
            tree = fromstring_safe(xml_bytes)
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

        # ── G5: RUT del CAF debe coincidir con la empresa ─────────────
        from crumbpos.db.models import Empresa
        empresa = self.db.execute(
            select(Empresa).where(Empresa.id == self.empresa_id)
        ).scalars().first()
        if empresa and rut_emisor:
            rut_caf_norm = rut_emisor.replace(".", "").upper()
            rut_emp_norm = empresa.rut.replace(".", "").upper()
            if rut_caf_norm != rut_emp_norm:
                raise ValueError(
                    f"El RUT del CAF ({rut_emisor}) no coincide con el RUT "
                    f"de la empresa ({empresa.rut}). "
                    f"Verifique que subió el archivo correcto."
                )

        # ── G2: firma + vigencia ──────────────────────────────────────
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

        max_dias = 365 if tipo_dte in (39, 41) else 730
        llave_sii = _cargar_llave_publica_sii()
        errores = caf_obj.validar(
            llave_publica_sii=llave_sii, max_dias_vigencia=max_dias,
        )
        if errores:
            raise ValueError("CAF rechazado:\n- " + "\n- ".join(errores))

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
                f"Ya existe un CAF para tipo {tipo_dte} con rango "
                f"{folio_desde}-{folio_hasta}"
            )

        # ── Crear CafFolio ─────────────────────────────────────────────
        folio_inicial = (
            folio_inicial_override
            if folio_inicial_override is not None
            else folio_desde
        )
        # C1: tomar el ambiente del CAF desde la empresa para que el marcador
        # quede incrustado en la fila y nunca se pueda consumir en el otro ambiente.
        ambiente_caf = empresa.ambiente_sii if empresa else "certificacion"
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
            ambiente=ambiente_caf,
        )
        self.db.add(caf_row)
        self.db.flush()

        # ── Crear asignación pool inicial ──────────────────────────────
        asig = CafAsignacion(
            caf_id=caf_row.id,
            sucursal_id=None,
            rango_desde=folio_desde,
            rango_hasta=folio_hasta,
            folio_actual=folio_inicial,
            estado="activo",
        )
        self.db.add(asig)
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

    # ══════════════════════════════════════════════════════════════════
    # DEVOLVER FOLIOS DE UNA SUCURSAL DESACTIVADA AL POOL
    # ══════════════════════════════════════════════════════════════════

    def devolver_folios_de_sucursal_al_pool(
        self,
        sucursal_id: str,
        actor_user_id: str | None = None,
    ) -> int:
        """Devuelve al pool todos los tramos de una sucursal.

        Pensado para el flujo "desactivar sucursal": al cerrar la sucursal,
        sus tramos sin consumir vuelven al pool del server. Folios ya
        consumidos quedan inmutables (siguen "perteneciendo" históricamente
        a la sucursal en términos de auditoría, pero ya no aparecen como
        rango asignable porque están consumidos).

        Devuelve la cantidad de tramos modificados.
        """
        with _folio_lock(self.empresa_id):
            tramos = self.db.execute(
                select(CafAsignacion)
                .join(CafFolio, CafAsignacion.caf_id == CafFolio.id)
                .where(and_(
                    CafFolio.empresa_id == self.empresa_id,
                    CafAsignacion.sucursal_id == sucursal_id,
                ))
                .with_for_update()
            ).scalars().all()
            for a in tramos:
                a.sucursal_id = None
                self.db.add(CafEventoSync(
                    sucursal_id=sucursal_id,
                    caf_id=a.caf_id,
                    asignacion_id=a.id,
                    tipo_evento="asignacion_eliminada",
                    payload={
                        "rango_desde": a.rango_desde,
                        "rango_hasta": a.rango_hasta,
                        "folio_actual": a.folio_actual,
                        "actor_user_id": actor_user_id,
                        "motivo": "sucursal_desactivada",
                    },
                ))
            self.db.flush()
            return len(tramos)

    # ══════════════════════════════════════════════════════════════════
    # HELPER — construir objeto CAF desde fila de DB
    # ══════════════════════════════════════════════════════════════════

    def _caf_from_row(self, row: CafFolio) -> "CAF":
        """Construye un objeto CAF (firma TED) desde un registro de DB."""
        import tempfile
        import os

        xml_data = row.caf_xml_raw
        if isinstance(xml_data, str):
            xml_data = xml_data.encode("ISO-8859-1")

        fd, tmp_path = tempfile.mkstemp(suffix=".xml")
        try:
            os.write(fd, xml_data)
            os.close(fd)
            return CAF(tmp_path)
        finally:
            # No borrar inmediatamente — el CAF puede necesitar releer
            pass
