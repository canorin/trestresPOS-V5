"""Parser del Set de Pruebas oficial del SII.

Lee el archivo `SIISetDePruebas{RUT}.txt` que el SII envía a cada contribuyente
postulante y devuelve una estructura tipada que el resto del software
(emisor DTE, generador de libros, generador de muestras impresas) puede
consumir directamente.

El parser NO contiene datos hardcoded de ninguna empresa: todo sale del .txt.
Esto es lo que permite que el módulo de certificación sirva para cualquier
cliente nuevo sin tocar código.

Encoding: el archivo del SII viene en ISO-8859-1 (Latin-1).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ── Tipos DTE soportados por el set de pruebas ────────────────────────
TIPO_FACTURA = 33
TIPO_FACTURA_EXENTA = 34
TIPO_BOLETA = 39
TIPO_BOLETA_EXENTA = 41
TIPO_GUIA = 52
TIPO_NOTA_DEBITO = 56
TIPO_NOTA_CREDITO = 61

# Mapeo del texto "DOCUMENTO" del set → tipo SII
# Orden: más específico primero para evitar match parcial.
_DOC_TIPO_MAP: list[tuple[str, int]] = [
    ("FACTURA NO AFECTA O EXENTA ELECTRONICA", TIPO_FACTURA_EXENTA),
    ("FACTURA EXENTA ELECTRONICA", TIPO_FACTURA_EXENTA),
    ("FACTURA ELECTRONICA", TIPO_FACTURA),
    ("BOLETA NO AFECTA O EXENTA ELECTRONICA", TIPO_BOLETA_EXENTA),
    ("BOLETA EXENTA ELECTRONICA", TIPO_BOLETA_EXENTA),
    ("BOLETA ELECTRONICA", TIPO_BOLETA),
    ("GUIA DE DESPACHO ELECTRONICA", TIPO_GUIA),
    ("GUIA DE DESPACHO", TIPO_GUIA),
    ("NOTA DE DEBITO ELECTRONICA", TIPO_NOTA_DEBITO),
    ("NOTA DE CREDITO ELECTRONICA", TIPO_NOTA_CREDITO),
]

# Tipos SII de los documentos del libro de compras (no todos electrónicos)
_TIPO_DOC_LIBRO_COMPRAS: dict[str, int] = {
    "FACTURA": 30,
    "FACTURA ELECTRONICA": 33,
    "FACTURA NO AFECTA O EXENTA": 32,
    "FACTURA NO AFECTA O EXENTA ELECTRONICA": 34,
    "FACTURA EXENTA ELECTRONICA": 34,
    "FACTURA EXENTA": 32,
    "BOLETA": 35,
    "BOLETA EXENTA": 38,
    "LIQUIDACION FACTURA": 43,
    "LIQUIDACION FACTURA ELECTRONICA": 43,
    "FACTURA DE COMPRA": 45,
    "FACTURA DE COMPRA ELECTRONICA": 46,
    "GUIA DE DESPACHO": 50,
    "GUIA DE DESPACHO ELECTRONICA": 52,
    "NOTA DE DEBITO": 55,
    "NOTA DE DEBITO ELECTRONICA": 56,
    "NOTA DE CREDITO": 60,
    "NOTA DE CREDITO ELECTRONICA": 61,
}

# Nombres canónicos de los sets que el SII envía
SET_BASICO = "BASICO"
SET_GUIAS = "GUIAS"
SET_EXENTA = "EXENTA"
SET_BOLETAS = "BOLETAS"
SET_LIBRO_VENTAS = "LIBRO_VENTAS"
SET_LIBRO_COMPRAS = "LIBRO_COMPRAS"
SET_LIBRO_GUIAS = "LIBRO_GUIAS"

# Identificación de cada set por la línea de header.
# El set de boletas se entrega en un archivo .txt separado del set regular.
# El SII usa encabezados como "SET BOLETA ELECTRONICA - NUMERO DE ATENCION: N"
# o variantes. El patrón es flexible: cualquier "SET BOLETA*".
_SET_HEADER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^SET\s+BASICO\b", re.I), SET_BASICO),
    (re.compile(r"^SET\s+GUIA\s+DE\s+DESPACHO\b", re.I), SET_GUIAS),
    (re.compile(r"^SET\s+FACTURA\s+EXENTA\b", re.I), SET_EXENTA),
    (re.compile(r"^SET\s+BOLETA", re.I), SET_BOLETAS),
    (re.compile(r"^SET\s+LIBRO\s+DE\s+VENTAS\b", re.I), SET_LIBRO_VENTAS),
    (re.compile(r"^SET\s+LIBRO\s+DE\s+COMPRAS\b", re.I), SET_LIBRO_COMPRAS),
    (re.compile(r"^SET\s+LIBRO\s+DE\s+GUIAS\b", re.I), SET_LIBRO_GUIAS),
]


# ════════════════════════════════════════════════════════════════════
# Modelos
# ════════════════════════════════════════════════════════════════════

@dataclass
class ItemCaso:
    """Una línea de detalle dentro de un caso del set."""
    nombre: str
    cantidad: int | None = None
    precio_unitario: int | None = None
    descuento_pct: int | None = None
    unidad_medida: str | None = None
    exento: bool = False


@dataclass
class ReferenciaCaso:
    """Referencia a otro caso del mismo set (para NC/ND)."""
    caso_referido: str           # ej: "4768464-1"
    tipo_doc_referido: int       # 33, 34, 61
    razon: str
    cod_ref: int                 # 1=anula, 2=corrige texto, 3=corrige montos


@dataclass
class CasoSet:
    """Un caso individual del set de pruebas."""
    numero_caso: str             # ej: "4768464-1"
    set_nombre: str              # BASICO, GUIAS, EXENTA
    numero_atencion: int         # ej: 4768464
    tipo_dte: int                # 33, 34, 52, 56, 61
    items: list[ItemCaso] = field(default_factory=list)
    referencia: ReferenciaCaso | None = None
    descuento_global_pct: int | None = None
    motivo_guia: str | None = None       # texto crudo del MOTIVO
    traslado_por: str | None = None      # texto crudo del TRASLADO POR
    ind_traslado: int | None = None      # 1=venta, 5=traslado interno, etc.
    tipo_despacho: int | None = None     # 1=cliente, 2=emisor a cliente, 3=emisor a otras


@dataclass
class CompraLibro:
    """Una compra del libro de compras (set libro de compras)."""
    tipo_doc_texto: str          # ej: "FACTURA ELECTRONICA"
    tipo_doc: int                # tipo SII (30, 33, 45, 46, 60, 61, ...)
    folio: int
    observaciones: str
    monto_exento: int = 0
    monto_afecto: int = 0


@dataclass
class SetParseado:
    """Resultado completo del parseo de un SIISetDePruebas{RUT}.txt"""
    rut_emisor: str | None = None
    sets: dict[str, list[CasoSet]] = field(default_factory=dict)
    libro_compras: list[CompraLibro] = field(default_factory=list)
    libro_compras_observaciones: list[str] = field(default_factory=list)
    libro_ventas_instrucciones: str | None = None
    libro_guias_instrucciones: str | None = None

    def todos_los_casos(self) -> list[CasoSet]:
        """Devuelve todos los casos de todos los sets, en orden."""
        out: list[CasoSet] = []
        for nombre in (SET_BASICO, SET_EXENTA, SET_GUIAS, SET_BOLETAS):
            out.extend(self.sets.get(nombre, []))
        return out

    def buscar_caso(self, numero_caso: str) -> CasoSet | None:
        """Busca un caso por su número (ej: '4768464-1')."""
        for caso in self.todos_los_casos():
            if caso.numero_caso == numero_caso:
                return caso
        return None


# ════════════════════════════════════════════════════════════════════
# API pública
# ════════════════════════════════════════════════════════════════════

def parse_set_sii(filepath: str | Path) -> SetParseado:
    """Lee el archivo del set de pruebas y devuelve la estructura parseada."""
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Set de pruebas no encontrado: {filepath}")
    content = path.read_text(encoding="iso-8859-1")
    return parse_set_sii_content(content, rut_hint=_rut_from_filename(path.name))


def parse_set_sii_content(content: str, rut_hint: str | None = None) -> SetParseado:
    """Parsea el contenido textual del set (sin tocar disco)."""
    resultado = SetParseado(rut_emisor=rut_hint)

    for bloque in _split_into_sets(content):
        nombre_set, numero_atencion, cuerpo = _identificar_set(bloque)
        if nombre_set is None:
            continue

        if nombre_set == SET_LIBRO_COMPRAS:
            compras, obs = _parse_libro_compras(cuerpo)
            resultado.libro_compras = compras
            resultado.libro_compras_observaciones = obs
        elif nombre_set == SET_LIBRO_VENTAS:
            resultado.libro_ventas_instrucciones = cuerpo.strip()
        elif nombre_set == SET_LIBRO_GUIAS:
            resultado.libro_guias_instrucciones = cuerpo.strip()
        else:
            casos = _parse_casos_de_set(cuerpo, nombre_set, numero_atencion)
            resultado.sets[nombre_set] = casos

    return resultado


# ════════════════════════════════════════════════════════════════════
# Splitting / identificación
# ════════════════════════════════════════════════════════════════════

_SEPARATOR_RE = re.compile(r"^-{40,}\s*$", re.M)
# El SII envía indistintamente "ATENCION" o "ATENCIÓN" (con tilde).
# El "." matches both 'O' y 'Ó' sin depender del encoding.
_NUM_ATENCION_RE = re.compile(r"NUMERO\s+DE\s+ATENCI.N\s*:?\s*(\d+)", re.I)


def _split_into_sets(content: str) -> list[str]:
    return _SEPARATOR_RE.split(content)


def _identificar_set(bloque: str) -> tuple[str | None, int | None, str]:
    lineas = bloque.strip().splitlines()
    if not lineas:
        return None, None, bloque
    for i, linea in enumerate(lineas):
        linea_norm = linea.strip()
        for regex, nombre in _SET_HEADER_PATTERNS:
            if regex.search(linea_norm):
                num = _extract_numero_atencion(linea_norm)
                cuerpo = "\n".join(lineas[i + 1:])
                return nombre, num, cuerpo
    return None, None, bloque


def _extract_numero_atencion(linea: str) -> int | None:
    m = _NUM_ATENCION_RE.search(linea)
    return int(m.group(1)) if m else None


# ════════════════════════════════════════════════════════════════════
# Parser de casos
# ════════════════════════════════════════════════════════════════════

_CASO_RE = re.compile(r"^CASO\s+(\d+-\d+)\s*$", re.M)
_DOCUMENTO_RE = re.compile(r"^DOCUMENTO\s*[\t ]+(.+?)\s*$", re.M)
_REFERENCIA_RE = re.compile(r"^REFERENCIA\s*[\t ]+(.+?)\s*$", re.M)
_RAZON_REF_RE = re.compile(r"^RAZON\s+REFERENCIA\s*[\t ]+(.+?)\s*$", re.M)
_MOTIVO_RE = re.compile(r"^MOTIVO\s*:?\s*[\t ]+(.+?)\s*$", re.M)
_TRASLADO_POR_RE = re.compile(r"^TRASLADO\s+POR\s*:?\s*[\t ]+(.+?)\s*$", re.M)
_DESC_GLOBAL_RE = re.compile(
    r"DESCUENTO\s+GLOBAL\s+ITEMES?\s+AFECTOS\s*[\t ]+(\d+)\s*%", re.I
)
_CASO_REF_NUM_RE = re.compile(r"CASO\s+(\d+-\d+)", re.I)


def _parse_casos_de_set(cuerpo: str, nombre_set: str, numero_atencion: int | None) -> list[CasoSet]:
    if numero_atencion is None:
        return []
    matches = list(_CASO_RE.finditer(cuerpo))
    if not matches:
        return []

    casos: list[CasoSet] = []
    for idx, m in enumerate(matches):
        numero_caso = m.group(1)
        inicio = m.end()
        fin = matches[idx + 1].start() if idx + 1 < len(matches) else len(cuerpo)
        bloque_caso = cuerpo[inicio:fin]
        caso = _parse_caso_individual(numero_caso, bloque_caso, nombre_set, numero_atencion)
        if caso is not None:
            casos.append(caso)
    return casos


def _parse_caso_individual(
    numero: str, texto: str, nombre_set: str, numero_atencion: int,
) -> CasoSet | None:
    m_doc = _DOCUMENTO_RE.search(texto)
    if not m_doc:
        return None
    tipo_dte = _doc_to_tipo(m_doc.group(1))
    if tipo_dte is None:
        return None

    caso = CasoSet(
        numero_caso=numero,
        set_nombre=nombre_set,
        numero_atencion=numero_atencion,
        tipo_dte=tipo_dte,
    )

    # Referencia (NC/ND)
    m_ref = _REFERENCIA_RE.search(texto)
    m_razon = _RAZON_REF_RE.search(texto)
    if m_ref and m_razon:
        ref_texto = _normalizar_inline(m_ref.group(1))
        razon = _normalizar_inline(m_razon.group(1))
        caso_ref_match = _CASO_REF_NUM_RE.search(ref_texto)
        if caso_ref_match:
            tipo_doc_ref = _extract_tipo_doc_ref(ref_texto)
            caso.referencia = ReferenciaCaso(
                caso_referido=caso_ref_match.group(1),
                tipo_doc_referido=tipo_doc_ref,
                razon=razon,
                cod_ref=_inferir_cod_ref(razon),
            )

    # Descuento global
    m_dg = _DESC_GLOBAL_RE.search(texto)
    if m_dg:
        caso.descuento_global_pct = int(m_dg.group(1))

    # Motivo / traslado por (guías)
    m_motivo = _MOTIVO_RE.search(texto)
    if m_motivo:
        caso.motivo_guia = _normalizar_inline(m_motivo.group(1)).upper()
    m_tp = _TRASLADO_POR_RE.search(texto)
    if m_tp:
        caso.traslado_por = _normalizar_inline(m_tp.group(1)).upper()

    if caso.tipo_dte == TIPO_GUIA:
        ind, td = _inferir_indicadores_guia(caso.motivo_guia, caso.traslado_por)
        caso.ind_traslado = ind
        caso.tipo_despacho = td

    # Items
    caso.items = _parse_items_de_caso(texto, caso.tipo_dte)

    return caso


# ════════════════════════════════════════════════════════════════════
# Parser de ítems
# ════════════════════════════════════════════════════════════════════

_HEADER_ITEM_RE = re.compile(r"^\s*ITEM\b", re.I)
_STOP_MARKERS_RE = re.compile(
    r"^\s*(DESCUENTO\s+GLOBAL|CASO\s+\d+|REFERENCIA|RAZON|DOCUMENTO|MOTIVO|TRASLADO)",
    re.I,
)


def _parse_items_de_caso(texto: str, tipo_dte: int) -> list[ItemCaso]:
    lineas = texto.splitlines()

    header_idx: int | None = None
    for i, linea in enumerate(lineas):
        if not _HEADER_ITEM_RE.match(linea):
            continue
        upper = linea.upper()
        if "CANTIDAD" in upper or "PRECIO" in upper or "VALOR" in upper:
            header_idx = i
            break

    if header_idx is None:
        return []

    columnas = _normalizar_columnas_header(lineas[header_idx])
    if not columnas:
        return []

    items: list[ItemCaso] = []
    for j in range(header_idx + 1, len(lineas)):
        linea = lineas[j]
        if not linea.strip():
            # blank line: si ya teníamos ítems, terminar
            if items:
                break
            continue
        if _STOP_MARKERS_RE.match(linea):
            break
        item = _parse_item_line(linea, columnas, tipo_dte)
        if item is not None:
            items.append(item)
    return items


def _normalizar_columnas_header(linea_header: str) -> list[str]:
    """Convierte la línea 'ITEM\\tCANTIDAD\\tPRECIO UNITARIO' en
    una lista de identificadores de columna ['cantidad', 'precio'].
    """
    partes = re.split(r"\s{2,}|\t+", linea_header.strip())
    out: list[str] = []
    for p in partes[1:]:  # saltar 'ITEM'
        u = p.upper()
        if "CANTIDAD" in u:
            out.append("cantidad")
        elif "DESCUENTO" in u:
            out.append("descuento")
        elif "UNIDAD" in u and "MEDIDA" in u:
            out.append("unidad")
        elif "PRECIO" in u or "VALOR" in u:
            out.append("precio")
    return out


def _parse_item_line(linea: str, columnas: list[str], tipo_dte: int) -> ItemCaso | None:
    partes = re.split(r"\s{2,}|\t+", linea.strip())
    if len(partes) < 2:
        return None
    nombre = partes[0].strip()
    valores = [p.strip() for p in partes[1:] if p.strip()]
    if not nombre:
        return None

    item = ItemCaso(nombre=nombre)
    nombre_upper = nombre.upper()
    # Items con "EXENTO" en el nombre, o T34 (todo el doc es exento)
    if tipo_dte == TIPO_FACTURA_EXENTA or "EXENTO" in nombre_upper:
        item.exento = True

    for col, valor in zip(columnas, valores):
        if col == "cantidad":
            n = _to_int(valor)
            if n is not None:
                item.cantidad = n
        elif col == "precio":
            n = _to_int(valor)
            if n is not None:
                item.precio_unitario = n
        elif col == "descuento":
            n = _to_int(valor.replace("%", ""))
            if n is not None:
                item.descuento_pct = n
        elif col == "unidad":
            item.unidad_medida = valor
    return item


# ════════════════════════════════════════════════════════════════════
# Parser del libro de compras
# ════════════════════════════════════════════════════════════════════

_SEPARADOR_LIBRO_RE = re.compile(r"^={20,}\s*$", re.M)


def _parse_libro_compras(cuerpo: str) -> tuple[list[CompraLibro], list[str]]:
    """Parsea la sección 'SET LIBRO DE COMPRAS'.

    El bloque tiene formato:
        ====
        TIPO DOCUMENTO   FOLIO
        OBSERVACIONES
        MONTO EXENTO  MONTO AFECTO
        ====
        FACTURA          234
        FACTURA DEL GIRO CON DERECHO A CREDITO
                  32996

        FACTURA ELECTRONICA   32
        ...
        ====

        OBSERVACIONES GENERALES
        ...
    """
    bloques = _SEPARADOR_LIBRO_RE.split(cuerpo)
    compras: list[CompraLibro] = []
    observaciones: list[str] = []

    if len(bloques) < 3:
        return compras, observaciones

    entries_block = bloques[2]  # entre el 2do y 3er '===='
    post_block = bloques[3] if len(bloques) > 3 else ""

    grupos = _agrupar_por_lineas_vacias(entries_block.splitlines())
    for grupo in grupos:
        compra = _parse_compra_grupo(grupo)
        if compra is not None:
            compras.append(compra)

    if post_block:
        for l in post_block.splitlines():
            ls = l.strip()
            if not ls or ls.startswith("---"):
                continue
            if ls.upper() == "OBSERVACIONES GENERALES":
                continue
            observaciones.append(ls)

    return compras, observaciones


def _agrupar_por_lineas_vacias(lineas: list[str]) -> list[list[str]]:
    grupos: list[list[str]] = []
    actual: list[str] = []
    for l in lineas:
        if l.strip():
            actual.append(l)
        elif actual:
            grupos.append(actual)
            actual = []
    if actual:
        grupos.append(actual)
    return grupos


def _parse_compra_grupo(grupo: list[str]) -> CompraLibro | None:
    if len(grupo) < 3:
        return None

    # Línea 1: "TIPO DOC ... FOLIO"
    partes_1 = re.split(r"\s{2,}|\t+", grupo[0].strip())
    if len(partes_1) < 2:
        return None
    tipo_str = partes_1[0].strip().upper()
    folio = _to_int(partes_1[-1])
    if folio is None:
        return None

    observaciones = grupo[1].strip()

    # Línea 3: montos
    partes_3 = re.split(r"\s{2,}|\t+", grupo[2].strip())
    nums = [_to_int(p) for p in partes_3 if _to_int(p) is not None]
    if len(nums) == 1:
        monto_exento = 0
        monto_afecto = nums[0]
    elif len(nums) >= 2:
        monto_exento = nums[0] or 0
        monto_afecto = nums[1] or 0
    else:
        monto_exento = 0
        monto_afecto = 0

    tipo_doc = _TIPO_DOC_LIBRO_COMPRAS.get(tipo_str, 0)

    return CompraLibro(
        tipo_doc_texto=tipo_str,
        tipo_doc=tipo_doc,
        folio=folio,
        observaciones=observaciones,
        monto_exento=monto_exento,
        monto_afecto=monto_afecto,
    )


# ════════════════════════════════════════════════════════════════════
# Inferencias y helpers
# ════════════════════════════════════════════════════════════════════

def _doc_to_tipo(doc_str: str) -> int | None:
    s = doc_str.strip().upper()
    for clave, tipo in _DOC_TIPO_MAP:
        if clave in s:
            return tipo
    return None


def _extract_tipo_doc_ref(ref_texto: str) -> int:
    upper = ref_texto.upper()
    if "FACTURA NO AFECTA" in upper or "FACTURA EXENTA" in upper:
        return 34
    if "NOTA DE CREDITO" in upper:
        return 61
    if "NOTA DE DEBITO" in upper:
        return 56
    if "GUIA" in upper:
        return 52
    if "FACTURA" in upper:
        return 33
    return 33


def _inferir_cod_ref(razon: str) -> int:
    """Infiere CodRef desde la 'RAZON REFERENCIA'.

    1 = anula doc completo
    2 = corrige texto (sin montos)
    3 = corrige montos
    """
    r = razon.upper()
    if "ANULA" in r:
        return 1
    if "CORRIGE" in r:
        if "MONTO" in r:
            return 3
        return 2  # CORRIGE GIRO / CORRIGE TEXTO / CORRIGE RECEPTOR
    if "MODIFICA" in r and "MONTO" in r:
        return 3
    if "DEVOLUCION" in r or "DEVUELVE" in r:
        return 3
    if "DESCUENTO" in r:
        return 3
    return 1


def _inferir_indicadores_guia(
    motivo: str | None, traslado_por: str | None,
) -> tuple[int | None, int | None]:
    """Infiere IndTraslado y TipoDespacho desde MOTIVO y TRASLADO POR."""
    if not motivo:
        return None, None
    m = motivo.upper()

    if "VENTA POR EFECTUAR" in m:
        ind = 2
    elif "VENTA" in m:
        ind = 1
    elif "CONSIGNACION" in m:
        ind = 3
    elif "PROMOCION" in m or "DONACION" in m:
        ind = 4
    elif "TRASLADO INTERNO" in m or ("TRASLADO" in m and "BODEGA" in m):
        ind = 5
    elif "DEVOLUCION" in m:
        ind = 7
    else:
        ind = 6

    td: int | None = None
    if ind in (1, 3) and traslado_por:
        tp = traslado_por.upper()
        # "EMISOR DEL DOCUMENTO AL LOCAL DEL CLIENTE" → emisor a instalaciones cliente
        if "EMISOR" in tp and "CLIENTE" in tp:
            td = 2
        elif "CLIENTE" in tp:
            td = 1   # por cuenta del receptor
        elif "EMISOR" in tp:
            td = 3   # emisor a otras instalaciones
    return ind, td


def _normalizar_inline(texto: str) -> str:
    return re.sub(r"\s+", " ", texto.strip())


def _to_int(valor: str) -> int | None:
    if valor is None:
        return None
    s = str(valor).strip().replace(".", "").replace(",", "").replace("%", "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _rut_from_filename(filename: str) -> str | None:
    """Extrae el RUT desde el nombre 'SIISetDePruebas{NNNNNNNND}.txt'."""
    m = re.search(r"SIISetDePruebas(\d+)", filename, re.I)
    if not m:
        return None
    digitos = m.group(1)
    if len(digitos) < 2:
        return None
    cuerpo, dv = digitos[:-1], digitos[-1]
    return f"{cuerpo}-{dv}"
