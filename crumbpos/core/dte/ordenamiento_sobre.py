"""Ordenamiento topológico de DTEs dentro de un sobre EnvioDTE.

Cuando un sobre contiene varios DTEs y algunos referencian a otros dentro
del mismo sobre, el SII exige que el referenciado aparezca antes que el
que lo referencia. Además, la validación CodRef=3 del SII (corrige monto)
rechaza el sobre si una Nota de Crédito (T61) va después de una Nota de
Débito (T56) cuando ambas apuntan al mismo documento original.

Este módulo centraliza la lógica en el core. Lo usan dos consumidores:

- `crumbpos/api/routers/facturacion.py::_enviar_set`: envío normal de sets
  de DTEs emitidos desde el panel de administración de la empresa.
- `crumbpos/api/services/envio_sobre_cert.py`: armado del sobre del set
  de pruebas SII durante la certificación.

Trabaja sobre cualquier objeto con la forma (tipo_dte, folio, xml_firmado)
— típicamente DteEmitido, pero no importa el tipo concreto.

Regla aprendida durante certificación de TRESTRES PUBLICIDAD SPA (set
EXENTA intento 13): ordenar por topología + prioridad por tipo fue una
de las dos condiciones necesarias para que el set quedara SOK. La otra
fue remover IndExe=1 de los ítems del T34 (ver R19 en AGENTS.md y
`project_certificacion_estado.md::20`).
"""
from __future__ import annotations

import base64
import logging
import re
from collections import defaultdict, deque
from typing import Protocol, TypeVar

logger = logging.getLogger(__name__)


class _DteCompat(Protocol):
    """Interface mínima que debe cumplir cada DTE pasado al ordenador."""
    tipo_dte: int
    folio: int
    xml_firmado: str | None


DTE = TypeVar("DTE", bound=_DteCompat)


# Prioridad por tipo cuando varios nodos quedan libres al mismo tiempo.
# Facturas primero (no dependen de nada), luego NC (T61) que modifica
# facturas, luego ND (T56) que modifica facturas, después guías y boletas.
DTE_PRIORITY = {33: 0, 34: 1, 61: 2, 56: 3, 52: 4, 39: 5, 41: 6}

_REF_PATTERN = re.compile(
    r"<TpoDocRef>(\d+)</TpoDocRef>.*?<FolioRef>(\d+)</FolioRef>",
    re.DOTALL,
)


def ordenar_por_dependencias(dtes: list[DTE]) -> list[DTE]:
    """Ordena DTEs por dependencias referenciales internas del sobre.

    Args:
        dtes: lista de DTEs a ordenar. Cada elemento debe tener
            ``tipo_dte`` (int), ``folio`` (int) y ``xml_firmado`` (str
            base64 del EnvioDTE individual tal como lo persiste
            ``ServicioEmisionDTE.emitir_factura``).

    Returns:
        Lista de los mismos objetos, pero ordenada con Kahn's algorithm
        sobre el grafo de dependencias. Si A referencia a B y ambos están
        en el sobre, B precede a A. En caso de empate, se ordena por
        ``DTE_PRIORITY`` (NC antes que ND).

    Behavior:
        - Si recibe 0 o 1 DTE, devuelve una copia de la lista sin tocar.
        - Si un DTE no tiene ``xml_firmado``, se trata como sin dependencias
          (no rompe el ordenamiento — a lo sumo queda primero).
        - Si el XML no parsea con el regex de referencia, se asume que
          no hay dependencias y se loguea un warning — el envío continúa.
        - En caso de ciclos (no debería ocurrir en un sobre real), los
          DTEs que queden sin procesar se agregan al final en orden
          determinista por (tipo, folio).
    """
    if len(dtes) <= 1:
        return list(dtes)

    dte_map: dict[tuple[int, int], DTE] = {}
    for d in dtes:
        dte_map[(d.tipo_dte, d.folio)] = d

    refs: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for d in dtes:
        key = (d.tipo_dte, d.folio)
        refs[key] = set()
        if not d.xml_firmado:
            continue
        try:
            xml_text = base64.b64decode(d.xml_firmado).decode(
                "ISO-8859-1", errors="replace",
            )
            for match in _REF_PATTERN.finditer(xml_text):
                ref_key = (int(match.group(1)), int(match.group(2)))
                if ref_key in dte_map:
                    refs[key].add(ref_key)
        except Exception as exc:
            logger.warning(
                "ordenar_por_dependencias: fallo parseando XML de "
                "(T%s F%s): %s", d.tipo_dte, d.folio, exc,
            )

    def _sort_key(k: tuple[int, int]) -> tuple[int, int]:
        return (DTE_PRIORITY.get(k[0], 99), k[1])

    in_degree: dict[tuple[int, int], int] = defaultdict(int)
    graph: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    all_keys = sorted(dte_map.keys(), key=_sort_key)

    for key in all_keys:
        in_degree.setdefault(key, 0)
        for dep in refs.get(key, set()):
            graph[dep].append(key)
            in_degree[key] += 1

    queue = deque(sorted(
        (k for k in all_keys if in_degree[k] == 0),
        key=_sort_key,
    ))
    ordered: list[tuple[int, int]] = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        released: list[tuple[int, int]] = []
        for dependent in graph[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                released.append(dependent)
        for r in sorted(released, key=_sort_key):
            queue.append(r)

    # Ciclos (no debería): agregar los restantes determinísticamente.
    vistos = set(ordered)
    remaining = sorted(
        (k for k in all_keys if k not in vistos),
        key=_sort_key,
    )
    ordered.extend(remaining)

    logger.info(
        "Orden DTEs en sobre: %s",
        [(dte_map[k].tipo_dte, dte_map[k].folio) for k in ordered],
    )
    return [dte_map[k] for k in ordered]
