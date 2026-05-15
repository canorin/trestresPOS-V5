"""Parseo de XML proveniente de fuentes no confiables.

`lxml.etree.fromstring/parse` por defecto NO resuelve entidades externas
(que sería el clásico XXE), pero SÍ expande entidades internas, lo que
habilita el ataque "billion laughs":

    <!DOCTYPE bomb [
      <!ENTITY a "lol">
      <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
      <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
      ...
    ]>
    <root>&c;</root>

10 niveles de expansión × 10 entidades = 10^10 nodos al parsear, OOM
en segundos.

Este módulo expone `fromstring_safe()` y `parse_safe()` que usan un
`XMLParser` configurado para rechazar:
  - DTDs (`no_network=True, resolve_entities=False`).
  - Expansión de entidades.
  - Documentos huge (`huge_tree=False`).

**Uso obligatorio en TODO endpoint que parsee XML enviado por el usuario:**
uploads de CAF, sets de prueba, DTEs recibidos de proveedores, etc.

NO usar para XML generado internamente (por ejemplo, el XML firmado de
DTEs que generamos nosotros) — eso es trusted.
"""
from __future__ import annotations

from lxml import etree

# Parser endurecido: rechaza DTDs, entidades, redes y árboles enormes.
# `resolve_entities=False` impide expansión incluso si por error pasa un DTD.
_SAFE_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    dtd_validation=False,
    load_dtd=False,
)


def fromstring_safe(xml: bytes | str):
    """Parsea XML desde bytes/str con parser endurecido contra XXE/billion laughs.

    Usar en TODO endpoint que reciba XML del usuario.
    """
    if isinstance(xml, str):
        xml = xml.encode("iso-8859-1", errors="replace")
    return etree.fromstring(xml, parser=_SAFE_PARSER)


def parse_safe(source):
    """Parsea XML desde archivo/stream con parser endurecido.

    `source` puede ser un path string, un file-like object o un Path.
    """
    return etree.parse(source, parser=_SAFE_PARSER)
