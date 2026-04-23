"""Invariantes de producción — pytest.

Capa 3 del sistema de auto-verificación (ver AGENTS.md).

Este archivo codifica las reglas del proyecto como aserciones pytest que
recorren el repo completo y validan que el estado del código respeta las
invariantes. A diferencia del hook `.claude/hooks/guardian.py` (que actúa
antes de cada edición), estos tests auditan el estado ya escrito — así
capturan violaciones que entraron por otras vías (git pull, edición manual,
merges) o que ya existían antes de que instaláramos el hook.

Correr manualmente:
    pytest tests/test_invariantes_produccion.py -q

Correr solo un test:
    pytest tests/test_invariantes_produccion.py::test_R1 -q

Convenciones:
- Cada test tiene el número de la regla en su nombre (test_R1, test_R4_a, etc.)
- Cuando un test falla, el mensaje debe incluir archivo + línea para que
  sea trivial encontrar el problema. Nada de "alguna regla está rota".
- Los tests son read-only sobre el filesystem — nunca escriben, nunca mueven,
  nunca tocan BDs. Esto hace que sea seguro correrlos en cualquier estado.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "crumbpos"


# ───────── Helpers ─────────

def _all_py_files(*roots: Path):
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts or ".git" in p.parts:
                continue
            yield p


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return p.read_text(encoding="latin-1", errors="replace")


def _line_of(text: str, offset: int) -> int:
    return text[:offset].count("\n") + 1


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


# Zonas del core donde R1 (no bifurcar por ambiente) aplica.
CORE_PROTECTED = [
    CORE / "core",
    CORE / "db",
    CORE / "models",
    CORE / "utils",
]

# Routers que sí pueden referenciar ambiente literal (meta-gestores).
ROUTERS_DIR = CORE / "api" / "routers"
ROUTERS_EXCEPTIONS = {"certificacion.py", "empresas.py", "__init__.py"}


def _protected_routers():
    if not ROUTERS_DIR.exists():
        return
    for p in ROUTERS_DIR.glob("*.py"):
        if p.name in ROUTERS_EXCEPTIONS:
            continue
        yield p


# ───────── R1 — Un solo código, dos almacenes ─────────

R1_PATTERN = re.compile(
    r'(?:ambiente(?:_activo)?|tenant\.ambiente)\s*==\s*[\'"](?:certificacion|produccion)[\'"]',
    re.IGNORECASE,
)


def test_R1_sin_if_ambiente_en_core():
    """R1: la lógica de negocio no bifurca por ambiente."""
    ofensores = []
    for f in _all_py_files(*CORE_PROTECTED, *_protected_routers()):
        src = _read_text(f)
        for m in R1_PATTERN.finditer(src):
            ofensores.append(
                f"{_rel(f)}:{_line_of(src, m.start())}  →  {m.group(0)!r}"
            )
    assert not ofensores, (
        "\nR1 violada — bifurcación por ambiente detectada en zona protegida:\n  "
        + "\n  ".join(ofensores)
        + "\n\nLa lógica de negocio es única para certificación y producción. "
        "La diferencia vive en configuración (URLs SII, path BD), nunca en "
        "if/else del core. Ver AGENTS.md sección R1."
    )


# ───────── R2 — Fixes en el core, no parches ─────────

R2_FORBIDDEN_NAMES = re.compile(
    r'^(fix|parche|hotfix|workaround|arreglar|temp|emergency|rapido)_.*\.py$',
    re.IGNORECASE,
)

R2_ALLOWED_DIRS = ("/sandbox/", "/tests/", "/OLD/")


def test_R2_sin_parches_sueltos():
    """R2: no hay archivos con nombre fix_*, parche_*, hotfix_*, etc."""
    ofensores = []
    for p in ROOT.rglob("*.py"):
        if any(part in (".git", "__pycache__", "node_modules") for part in p.parts):
            continue
        rel = _rel(p)
        # Migraciones en crumbpos/scripts/migrar_*.py están permitidas.
        if rel.startswith("crumbpos/scripts/migrar_"):
            continue
        if any(d in f"/{rel}" for d in R2_ALLOWED_DIRS):
            continue
        if R2_FORBIDDEN_NAMES.match(p.name):
            ofensores.append(rel)
    assert not ofensores, (
        "\nR2 violada — archivos con nombre de parche detectados:\n  "
        + "\n  ".join(ofensores)
        + "\n\nLos fixes van en el core, no en archivos sueltos. "
        "Si es una migración legítima, ubicarla en crumbpos/scripts/migrar_*.py "
        "con docstring explicativo. Ver AGENTS.md sección R2."
    )


# ───────── R4 — Nunca mezclar documentos entre ambientes ─────────

R4_ROUTER_LITERAL = re.compile(
    r'get_empresa_db_session\s*\([^)]+,\s*[\'"]certificacion[\'"]',
)


def test_R4_routers_produccion_no_usan_literal_certificacion():
    """R4: routers de prod obtienen ambiente de tenant.ambiente, no literal."""
    ofensores = []
    for f in _protected_routers():
        src = _read_text(f)
        for m in R4_ROUTER_LITERAL.finditer(src):
            ofensores.append(
                f"{_rel(f)}:{_line_of(src, m.start())}  →  {m.group(0)!r}"
            )
    assert not ofensores, (
        "\nR4 violada — routers de producción pasan 'certificacion' literal:\n  "
        + "\n  ".join(ofensores)
        + "\n\nEstos routers deben leer el ambiente desde "
        "EmpresaRegistro.ambiente_activo vía get_tenant, nunca hardcodeado. "
        "Las únicas excepciones son empresas.py y certificacion.py. "
        "Ver AGENTS.md sección R4."
    )


def test_R4_cleanup_no_menciona_produccion():
    """R4: cleanup.py es production-safe por construcción — no menciona prod."""
    cleanup = CORE / "certificacion" / "cleanup.py"
    if not cleanup.exists():
        # El módulo aún no fue implementado (Fase 6.b). El test queda latente
        # y se activa automáticamente cuando alguien cree el archivo.
        return

    src = _read_text(cleanup)
    # Solo verificar referencia al archivo .db del ambiente productivo.
    # La cadena "produccion" como nombre de etapa es legítima (precondición).
    tokens = ("produccion.db",)
    ofensores = []
    for token in tokens:
        idx = src.find(token)
        if idx >= 0:
            ofensores.append(f"línea {_line_of(src, idx)}: {token!r}")

    # Además: prohibido importar routers/ de producción.
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise AssertionError(f"cleanup.py no parsea: {e}")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if "routers" in node.module and "certificacion" not in node.module:
                ofensores.append(f"línea {node.lineno}: import {node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "routers" in alias.name and "certificacion" not in alias.name:
                    ofensores.append(f"línea {node.lineno}: import {alias.name}")

    assert not ofensores, (
        "\nR4 violada — cleanup.py viola su invariante de aislamiento:\n  "
        + "\n  ".join(ofensores)
        + "\n\ncleanup.py solo puede abrir certificacion.db y solo puede "
        "importar helpers del propio módulo de certificación. Ver AGENTS.md R4."
    )


# ───────── R7 — Español neutro ─────────

R7_WORDS = [
    "tenés", "vos", "hacés", "hacé", "querés", "sabés",
    "mirá", "andá", "dale", "podés", "decí", "sentís",
    "venís", "tené", "vení", "mandá", "pará",
]
R7_PATTERN = re.compile(
    r'\b(' + '|'.join(R7_WORDS) + r')\b',
    re.IGNORECASE,
)


def test_R7_espanol_neutro_en_core():
    """R7: sin voseo ni regionalismos argentinos en código."""
    ofensores = []
    for f in _all_py_files(CORE):
        src = _read_text(f)
        for m in R7_PATTERN.finditer(src):
            ofensores.append(
                f"{_rel(f)}:{_line_of(src, m.start())}  →  {m.group(0)!r}"
            )
    assert not ofensores, (
        "\nR7 violada — voseo/regionalismos detectados:\n  "
        + "\n  ".join(ofensores)
        + "\n\nTodo texto en código y strings visibles al usuario debe estar "
        "en español neutro. Ver AGENTS.md sección R7."
    )


# ───────── R4 adicional — master.db y produccion.db nunca se borran vía código ─────────

def test_R4_ningun_modulo_rm_produccion_db():
    """R4: ningún archivo Python ejecuta rm/unlink sobre produccion.db."""
    patron = re.compile(
        r'(os\.remove|os\.unlink|Path\([^)]*\)\.unlink|shutil\.rmtree)[^#\n]*produccion\.db',
    )
    ofensores = []
    for f in _all_py_files(CORE):
        src = _read_text(f)
        for m in patron.finditer(src):
            ofensores.append(
                f"{_rel(f)}:{_line_of(src, m.start())}  →  {m.group(0)!r}"
            )
    assert not ofensores, (
        "\nR4 violada — código Python intenta borrar produccion.db:\n  "
        + "\n  ".join(ofensores)
        + "\n\nEl archivo produccion.db nunca se borra programáticamente. "
        "Contiene DTEs fiscalmente válidos con retención legal. Ver AGENTS.md R4."
    )


# ───────── R4.a — Baja de empresa: guard + excepción narrow-scoped ─────────

ELIMINACION_EMPRESA_FILE = CORE / "admin" / "eliminacion_empresa.py"
R4A_AUTHORIZED_REL = "crumbpos/admin/eliminacion_empresa.py"

# Funciones destructivas dentro de eliminacion_empresa.py que deben iniciar
# con una llamada a _verificar_zip_descargado_o_error(rut).
R4A_FUNCIONES_GUARDADAS = ("confirmar_baja", "eliminar_definitivo")
R4A_GUARD_NAME = "_verificar_zip_descargado_o_error"


def _first_executable_stmt(fn_node: ast.FunctionDef) -> ast.stmt | None:
    """Primera sentencia ejecutable de una función, saltando docstring."""
    body = fn_node.body
    if not body:
        return None
    first = body[0]
    # Docstring = Expr cuyo value es Constant(str). No cuenta como ejecutable.
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1] if len(body) > 1 else None
    return first


def _stmt_llama_funcion(stmt: ast.stmt, nombre: str) -> bool:
    """True si la sentencia es una llamada top-level a `nombre(...)`."""
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if not isinstance(call, ast.Call):
        return False
    # Caso directo: nombre(...)
    if isinstance(call.func, ast.Name) and call.func.id == nombre:
        return True
    # Caso atributo: self.nombre(...) / mod.nombre(...)
    if isinstance(call.func, ast.Attribute) and call.func.attr == nombre:
        return True
    return False


def test_R4a_eliminacion_empresa_tiene_guard():
    """R4.a: confirmar_baja y eliminar_definitivo inician con el guard.

    Verificamos por AST que la primera sentencia ejecutable (ignorando
    docstring) de cada función destructiva sea una llamada a
    `_verificar_zip_descargado_o_error`. Si alguien reordena el código y
    mete otra línea antes del guard, este test falla y la operación
    destructiva deja de estar protegida.
    """
    assert ELIMINACION_EMPRESA_FILE.exists(), (
        f"{R4A_AUTHORIZED_REL} no existe. Fase 7 requiere este módulo "
        f"como único punto autorizado para operaciones destructivas "
        f"sobre data/. Ver AGENTS.md R4.a."
    )
    src = _read_text(ELIMINACION_EMPRESA_FILE)
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise AssertionError(f"{R4A_AUTHORIZED_REL} no parsea: {e}")

    # Index de funciones top-level por nombre.
    funciones: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            funciones[node.name] = node

    # 1. Existe el guard propiamente dicho.
    assert R4A_GUARD_NAME in funciones, (
        f"R4.a: el guard '{R4A_GUARD_NAME}' no existe como función "
        f"top-level en {R4A_AUTHORIZED_REL}. Sin ese guard, no hay "
        f"verificación de que el super admin descargó el ZIP de respaldo "
        f"antes de ejecutar una operación destructiva."
    )

    # 2. Cada función protegida debe iniciar con una llamada al guard.
    problemas = []
    for fn_name in R4A_FUNCIONES_GUARDADAS:
        fn = funciones.get(fn_name)
        if fn is None:
            problemas.append(
                f"función '{fn_name}' ausente en {R4A_AUTHORIZED_REL}"
            )
            continue
        first = _first_executable_stmt(fn)
        if first is None:
            problemas.append(
                f"función '{fn_name}' tiene cuerpo vacío "
                f"(línea {fn.lineno})"
            )
            continue
        if not _stmt_llama_funcion(first, R4A_GUARD_NAME):
            problemas.append(
                f"función '{fn_name}' (línea {fn.lineno}) no inicia con "
                f"una llamada a '{R4A_GUARD_NAME}'. Primera sentencia "
                f"ejecutable está en línea {first.lineno}."
            )

    assert not problemas, (
        "\nR4.a violada — el guard de baja no protege todas las "
        "operaciones destructivas:\n  "
        + "\n  ".join(problemas)
        + "\n\nTodas las funciones en "
        + R4A_AUTHORIZED_REL
        + " que tocan data/ deben empezar con "
        + f"{R4A_GUARD_NAME}(rut). Ver AGENTS.md R4.a."
    )


# Patrones destructivos bajo data/. Se permiten únicamente en el archivo
# autorizado. Cualquier otra ubicación del core es una violación.
R4A_DESTRUCTIVE_PATTERNS = [
    re.compile(r'shutil\.move\s*\('),
    re.compile(r'shutil\.rmtree\s*\('),
    re.compile(r'os\.remove\s*\('),
    re.compile(r'os\.unlink\s*\('),
    re.compile(r'\.unlink\s*\(\s*\)'),
]
R4A_DATA_HINT = re.compile(
    r'["\']\.?/?data/|\bDATA_DIR\b|\bTRASH_ROOT\b|\bZIP_EXPORT_ROOT\b',
)


def test_R4a_shutil_destructivo_solo_en_eliminacion():
    """R4.a: shutil.move/rmtree sobre data/ solo en eliminacion_empresa.py.

    Cualquier otro archivo del core que combine un patrón destructivo
    (`shutil.move`, `shutil.rmtree`, `os.remove`, `os.unlink`,
    `.unlink()`) con una referencia a `data/` queda prohibido. Si hace
    falta mover o borrar data de una empresa, se llama al módulo
    autorizado vía el router `baja_empresas`.

    Exclusiones válidas:
      - `crumbpos/admin/eliminacion_empresa.py` (el autorizado)
      - Archivos bajo `tests/`, `sandbox/`, `OLD/`, `crumbpos/scripts/migrar_*`
    """
    ofensores = []
    for f in _all_py_files(CORE):
        rel = _rel(f)
        if rel == R4A_AUTHORIZED_REL:
            continue
        # scripts/migrar_ quedan exentos por la misma lógica de R2.
        if rel.startswith("crumbpos/scripts/migrar_"):
            continue
        src = _read_text(f)
        # Optimización: si el archivo ni siquiera menciona data/, no puede
        # violar R4.a. Esto filtra la mayoría del core de una.
        if not R4A_DATA_HINT.search(src):
            continue
        for patron in R4A_DESTRUCTIVE_PATTERNS:
            for m in patron.finditer(src):
                ofensores.append(
                    f"{rel}:{_line_of(src, m.start())}  →  {m.group(0)!r}"
                )

    assert not ofensores, (
        "\nR4.a violada — operaciones destructivas sobre data/ fuera del "
        "archivo autorizado:\n  "
        + "\n  ".join(ofensores)
        + f"\n\nEl único archivo autorizado a hacer shutil.move/rmtree, "
        f"os.remove/unlink contra rutas en data/ es {R4A_AUTHORIZED_REL}, "
        f"y debe hacerlo siempre después del guard "
        f"{R4A_GUARD_NAME}(rut). Si el código nuevo necesita mover o "
        f"borrar archivos de una empresa, invocar a "
        f"crumbpos.admin.eliminacion_empresa vía el router "
        f"baja_empresas. Ver AGENTS.md R4.a."
    )


# ───────── Meta-test — existencia del contrato ─────────

def test_AGENTS_md_existe_y_tiene_las_reglas_clave():
    """Meta: el contrato de reglas debe existir en la raíz del repo."""
    agents = ROOT / "AGENTS.md"
    assert agents.exists(), (
        "AGENTS.md no existe en la raíz. Los tests de invariantes suponen "
        "que existe un contrato escrito. Ver la Capa 1 del sistema de "
        "auto-verificación."
    )
    src = _read_text(agents)
    # Chequeo mínimo: que mencione explícitamente las reglas críticas por número.
    reglas_criticas = ["R1", "R2", "R4", "R7", "R13"]
    faltantes = [r for r in reglas_criticas if f"### {r}" not in src]
    assert not faltantes, (
        f"AGENTS.md no documenta las reglas críticas: {faltantes}. "
        "Cada regla debe aparecer como encabezado `### Rn — ...` en el archivo."
    )


def test_guardian_hook_instalado():
    """Meta: el hook de Capa 2 está presente y se invoca desde settings.json."""
    hook = ROOT / ".claude" / "hooks" / "guardian.py"
    settings = ROOT / ".claude" / "settings.json"
    assert hook.exists(), (
        "Hook guardian.py ausente. Los tests suponen que la Capa 2 está "
        "instalada. Ver .claude/hooks/guardian.py."
    )
    assert settings.exists(), (
        ".claude/settings.json ausente. Sin este archivo Claude Code no "
        "invoca el hook."
    )
    import json as _json
    cfg = _json.loads(_read_text(settings))
    assert "hooks" in cfg and "PreToolUse" in cfg["hooks"], (
        "settings.json no declara un hook PreToolUse."
    )


# ───────── Harness reservado para Fase 6.b (cleanup) ─────────

def test_cleanup_no_muta_produccion_db():
    """Fase 6.b implementada — verifica aislamiento estático de cleanup.py.

    La cobertura funcional completa está en tests/test_cleanup_cert.py:
      - TestLimpiezaExitosa: borra runs/casos/libros/dtes/cafs
      - TestLimpiezaExitosa: preserva Empresa y Sucursal
      - TestPrecondiciones: valida etapa y estado
      - TestAislamientoR4: verificación AST

    Este test refuerza R4 verificando que cleanup.py no importa
    get_empresa_db_session con el argumento "produccion".
    """
    import ast as _ast

    cleanup = CORE / "certificacion" / "cleanup.py"
    assert cleanup.exists(), "cleanup.py no existe — Fase 6.b incompleta"
    src = _read_text(cleanup)

    # No debe contener llamadas a get_empresa_db_session con "produccion"
    assert 'get_empresa_db_session(rut, "produccion")' not in src, (
        "cleanup.py llama get_empresa_db_session con ambiente produccion — viola R4"
    )
    assert "get_empresa_db_session(rut, 'produccion')" not in src, (
        "cleanup.py llama get_empresa_db_session con ambiente produccion — viola R4"
    )
