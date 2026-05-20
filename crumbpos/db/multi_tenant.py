"""Multi-tenant database manager — aislamiento total por empresa y ambiente.

Arquitectura:
  data/
  ├── master.db                    ← Registro de empresas + usuarios (auth)
  ├── {RUT}/
  │   ├── certificacion.db        ← BD completa para certificación SII
  │   └── produccion.db           ← BD completa para producción SII

Cada empresa DB contiene TODAS las tablas (Empresa, Sucursal, DTE, CAF, etc.)
El master.db solo contiene tablas ligeras para autenticación y routing.

Flujo:
  1. Login → master.db (UsuarioAuth) → JWT con empresa_rut + rol
  2. Request autenticado → JWT.empresa_rut → master.db (EmpresaRegistro.ambiente_activo)
     → data/{rut}/{ambiente}.db para operaciones
  3. Super admin → puede especificar empresa_rut + ambiente en la request
"""
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    create_engine, String, Integer, Boolean, DateTime, Text, event, text,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    sessionmaker, DeclarativeBase, Mapped, mapped_column, Session,
)

# ══════════════════════════════════════════════════════════════════
# CONSTANTES DE NEGOCIO
# ══════════════════════════════════════════════════════════════════

ETAPAS_VALIDAS = (
    "pendiente_certificacion",  # recién creada, aún no inicia el wizard
    "proceso_certificacion",    # certificación en curso
    "produccion",               # aprobada por SII, operando en producción
)

PLANES_DISPONIBLES = (
    "full_free",  # plan de cortesía — todas las features gratis
)

# Estados del ciclo de vida de una empresa en master.db.
#
# activa           → operando normalmente, visible en el listado principal
# eliminada_soft   → baja solicitada, datos movidos a data/.trash/, visible
#                    solo en "Papelera". El super admin puede restaurarla o
#                    (tras `puede_eliminarse_desde`) eliminarla definitivamente.
# eliminada_hard   → datos borrados del disco. El registro en master queda
#                    como tombstone inmutable para auditoría. Nunca vuelve
#                    a aparecer en ninguna UI operativa.
EMPRESA_ESTADOS = (
    "activa",
    "eliminada_soft",
    "eliminada_hard",
)

# Ventana de gracia entre baja soft y eliminación definitiva.
# La eliminación NO es automática — pasada esta ventana, el super admin
# ve el botón "Eliminar definitivamente" en la Papelera. Nada se borra
# solo: siempre hay un humano apretando el botón final.
ELIMINACION_GRACIA_DIAS = 30

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# DATA DIRECTORY
# ══════════════════════════════════════════════════════════════════

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


# ══════════════════════════════════════════════════════════════════
# MASTER DB MODELS
# ══════════════════════════════════════════════════════════════════

class BaseMaster(DeclarativeBase):
    """Base para tablas del master.db."""
    pass


class EmpresaRegistro(BaseMaster):
    """Registro central de empresas — solo en master.db.

    Contiene solo lo mínimo para routing. La config completa
    (cert, SII, etc.) está en la BD de la empresa.
    """
    __tablename__ = "empresa_registro"

    rut: Mapped[str] = mapped_column(String(12), primary_key=True)
    razon_social: Mapped[str] = mapped_column(String(100), nullable=False)
    ambiente_activo: Mapped[str] = mapped_column(
        String(15), default="certificacion",
    )  # "certificacion" | "produccion"
    etapa: Mapped[str] = mapped_column(
        String(30), default="pendiente_certificacion",
    )  # "pendiente_certificacion" | "proceso_certificacion" | "produccion"
    plan: Mapped[str] = mapped_column(
        String(30), default="full_free",
    )  # plan comercial — hoy solo "full_free" (plan de cortesía)
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
    )

    # ── Ciclo de vida: baja / papelera / eliminación definitiva ──
    #
    # El campo `activa` arriba es un flag legado (pausar / reactivar una
    # empresa sin darla de baja). `estado` es el nuevo campo que modela el
    # flujo completo de eliminación (ver EMPRESA_ESTADOS arriba). Ambos
    # conviven porque `activa` es barato de consultar y se usa en varias
    # partes del core; `estado` manda cuando hay un conflicto.
    estado: Mapped[str] = mapped_column(String(20), default="activa")
    # Momento en que el super admin confirmó la baja. Hasta que llega
    # `puede_eliminarse_desde`, la empresa vive en `data/.trash/{rut}_*`
    # y puede restaurarse sin perder nada.
    fecha_eliminacion_soft: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    puede_eliminarse_desde: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    # Momento en que se borró del disco. Inmutable — una vez seteado
    # ya no hay vuelta atrás. El registro queda como tombstone.
    fecha_eliminacion_hard: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    eliminado_por_user_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
    )
    # sha256 del último ZIP exportado para esta empresa. Sirve como
    # garantía auditable de que el cliente recibió su info antes de
    # que tocáramos sus archivos. Si está vacío, `confirmar_baja`
    # rechaza la operación.
    zip_descargado_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    # Momento en que se archivó la certificación (cleanup.py). Los datos
    # de la run/casos/libros se borraron de certificacion.db pero Empresa,
    # Sucursal y Usuario se preservan. No afecta produccion.db en absoluto.
    cert_archivada_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )

    # ── Representante legal de la empresa ──
    #
    # Metadata fiscal (no auth). Quién es la persona natural que figura
    # como representante legal en el SII. Puede coincidir con el master
    # cliente (mismo correo), pero son conceptos separados: el master
    # cliente es la cuenta que hace login; el representante legal es la
    # persona con responsabilidad legal ante el SII. Los 3 campos son
    # nullable hasta que se capturan (empresas preexistentes los tienen
    # en NULL hasta que el super admin las actualiza).
    representante_legal_nombre: Mapped[str | None] = mapped_column(
        String(80), nullable=True,
    )
    representante_legal_rut: Mapped[str | None] = mapped_column(
        String(12), nullable=True,
    )
    representante_legal_email: Mapped[str | None] = mapped_column(
        String(120), nullable=True,
    )


class EmpresaEliminacionLog(BaseMaster):
    """Log append-only de eventos del ciclo de baja/restauración/eliminación.

    Este log es la fuente de verdad para auditoría del super admin. Cada
    transición (exportar ZIP, soft-delete, restaurar, hard-delete) agrega
    una fila con quién la ejecutó y un JSON con contexto. Nunca se actualiza
    ni se borra — las empresas eliminadas mantienen su historial aquí aunque
    `empresa_registro` las haya movido a estado='eliminada_hard'.
    """
    __tablename__ = "empresa_eliminacion_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    empresa_rut: Mapped[str] = mapped_column(
        String(12), nullable=False, index=True,
    )
    evento: Mapped[str] = mapped_column(String(30), nullable=False)
    # Eventos posibles:
    #   zip_exportado   → super admin descargó el ZIP de la empresa
    #   baja_soft       → empresa movida a papelera
    #   restaurada      → empresa restaurada desde papelera
    #   baja_hard       → datos borrados del disco
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_email: Mapped[str | None] = mapped_column(String(120))
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
    )
    detalle_json: Mapped[str | None] = mapped_column(Text)
    # detalle_json: contexto libre. Ej:
    #   {"sha256": "...", "bytes": 1234567, "path_trash": "data/.trash/..."}
    #   {"dtes_exportados": 220, "libros_exportados": 12, "rcofs": 45}


class SchedulerEstado(BaseMaster):
    """Persistencia de estado del scheduler en master.db.

    Tabla key-value que permite al scheduler recordar qué tareas ejecutó para
    evitar repeticiones o detectar períodos perdidos al reiniciar (catch-up).

    Claves canónicas:
      ``iecv_ultimo_periodo_recordado``  → "YYYY-MM" del período que se
          recordó por última vez (usado por el catch-up IECV al boot).
    """
    __tablename__ = "scheduler_estado"

    clave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class SolicitudArco(BaseMaster):
    """Solicitudes de derechos ARCO (Acceso / Rectificación / Cancelación / Oposición).

    Ley 19.628 (Protección de la Vida Privada, Chile) obliga al responsable
    del tratamiento de datos a dar respuesta a solicitudes del titular.

    Cada solicitud queda persistida en master.db con estado ``pendiente``.
    El operador de Crumb debe procesarla en un plazo razonable (máximo
    5 días hábiles según la ley) y actualizar ``estado`` a ``completada``.
    """
    __tablename__ = "solicitud_arco"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    usuario_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # UID del UsuarioAuth que originó la solicitud.
    empresa_rut: Mapped[str] = mapped_column(String(12), nullable=False)
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    # Tipos: acceso | rectificacion | cancelacion | oposicion
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Descripción libre del titular sobre qué datos afectan la solicitud.
    estado: Mapped[str] = mapped_column(String(20), default="pendiente", nullable=False)
    # pendiente | en_proceso | completada | rechazada
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    resuelto_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolucion: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nota interna del operador al resolver.


class UsuarioAuth(BaseMaster):
    """Usuarios para autenticación centralizada — solo en master.db.

    Namespacing por empresa: la unicidad es (empresa_rut, email), no solo
    (email). Así un mismo correo puede ser master de N empresas (típico cuando
    un representante legal tiene varios negocios), y cada empresa es un
    "espacio" aislado que convive con el patrón ``data/{rut}/...`` del
    filesystem. El super_admin usa empresa_rut="SYSTEM".

    Roles (fuente única: ``crumbpos.core.roles.ROLES_JERARQUIA``):
      - super_admin: staff Crumb — cross-empresa.
      - master_client: dueño/representante legal de la empresa.
      - administrador: admin general de la empresa (no dueño).
      - administrador_tienda: admin de sucursal.
      - cajero: solo POS en sucursales asignadas.
    """
    __tablename__ = "usuario_auth"
    __table_args__ = (
        UniqueConstraint(
            "empresa_rut", "email",
            name="uq_usuario_auth_empresa_email",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    empresa_rut: Mapped[str] = mapped_column(
        String(12), nullable=False,
    )  # "SYSTEM" para super_admin
    email: Mapped[str] = mapped_column(String(120), nullable=False)
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    # RUT personal del usuario (p.ej. el del representante legal para el
    # master del cliente). Nullable porque usuarios históricos (super_admin
    # inicial, admins creados antes de esta migración) pueden no tenerlo.
    rut_personal: Mapped[str | None] = mapped_column(String(12), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    rol: Mapped[str] = mapped_column(String(20), nullable=False)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    # Si True, el usuario debe cambiar su password en el primer login.
    # Se setea al crear master/admin con password generada y se limpia
    # al ejecutar /api/auth/cambiar-password.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
    )
    # Timestamp del último cambio exitoso de password (para auditoría y
    # políticas futuras de expiración de password, ej. cada 180 días).
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
    )


# ══════════════════════════════════════════════════════════════════
# MASTER DB ENGINE / SESSION
# ══════════════════════════════════════════════════════════════════

_master_engine = None
_MasterSessionFactory = None


def _ensure_master():
    """Lazy-init master engine + create tables if needed."""
    global _master_engine, _MasterSessionFactory
    if _master_engine is not None:
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_url = f"sqlite:///{DATA_DIR / 'master.db'}"
    _master_engine = create_engine(
        db_url, connect_args={"check_same_thread": False}, echo=False,
    )
    # D2: WAL + synchronous=FULL en master.db (mismo criterio que tenant DBs).
    @event.listens_for(_master_engine, "connect")
    def _set_master_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.close()

    BaseMaster.metadata.create_all(bind=_master_engine)
    _migrate_master_schema(_master_engine)
    _MasterSessionFactory = sessionmaker(
        bind=_master_engine, autocommit=False, autoflush=False,
    )
    logger.info("Master DB initialized: %s", DATA_DIR / "master.db")


def _rename_roles_sql(conn, tabla: str) -> None:
    """Aplica los renames de rol viejos→canónicos en ``{tabla}.rol``.

    Idempotente: los UPDATEs con ``WHERE rol=<viejo>`` no hacen nada
    si ya no quedan filas con el string antiguo. Funciona tanto para
    ``usuario_auth`` (master.db) como para ``usuario`` (tenant.db).

    Ver ``crumbpos.core.roles.ROLES_ALIASES`` — la fuente canónica del
    mapeo; esta función solo lo materializa en SQL.
    """
    from crumbpos.core.roles import ROLES_ALIASES

    for viejo, canonico in ROLES_ALIASES.items():
        result = conn.execute(
            text(f"UPDATE {tabla} SET rol = :nuevo WHERE rol = :viejo"),
            {"nuevo": canonico, "viejo": viejo},
        )
        if result.rowcount:
            logger.info(
                "Rol migrate en %s: %d fila(s) %r → %r",
                tabla, result.rowcount, viejo, canonico,
            )


def _migrate_master_schema(engine):
    """Agrega columnas nuevas a empresa_registro si no existen (idempotente).

    SQLite no soporta ALTER TABLE ... IF NOT EXISTS, por eso consultamos
    PRAGMA table_info y agregamos solo lo que falte.
    """
    with engine.begin() as conn:
        cols = {
            row[1] for row in conn.execute(
                text("PRAGMA table_info(empresa_registro)"),
            )
        }
        if "etapa" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro ADD COLUMN etapa "
                "VARCHAR(30) NOT NULL DEFAULT 'pendiente_certificacion'"
            ))
            # Empresas preexistentes: derivar etapa desde ambiente_activo
            conn.execute(text(
                "UPDATE empresa_registro SET etapa = 'produccion' "
                "WHERE ambiente_activo = 'produccion'"
            ))
            conn.execute(text(
                "UPDATE empresa_registro SET etapa = 'proceso_certificacion' "
                "WHERE ambiente_activo = 'certificacion' "
                "AND etapa = 'pendiente_certificacion'"
            ))
            logger.info("Master migrate: columna 'etapa' agregada a empresa_registro")
        if "plan" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro ADD COLUMN plan "
                "VARCHAR(30) NOT NULL DEFAULT 'full_free'"
            ))
            logger.info("Master migrate: columna 'plan' agregada a empresa_registro")

        # ── Ciclo de baja/papelera/hard-delete ──
        #
        # Cada columna se agrega por separado con su DEFAULT, así las
        # empresas preexistentes arrancan automáticamente con estado='activa'
        # y fechas en NULL. Ninguna modificación es destructiva.
        if "estado" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro ADD COLUMN estado "
                "VARCHAR(20) NOT NULL DEFAULT 'activa'"
            ))
            logger.info("Master migrate: columna 'estado' agregada a empresa_registro")
        if "fecha_eliminacion_soft" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro "
                "ADD COLUMN fecha_eliminacion_soft DATETIME"
            ))
            logger.info(
                "Master migrate: columna 'fecha_eliminacion_soft' agregada",
            )
        if "puede_eliminarse_desde" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro "
                "ADD COLUMN puede_eliminarse_desde DATETIME"
            ))
            logger.info(
                "Master migrate: columna 'puede_eliminarse_desde' agregada",
            )
        if "fecha_eliminacion_hard" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro "
                "ADD COLUMN fecha_eliminacion_hard DATETIME"
            ))
            logger.info(
                "Master migrate: columna 'fecha_eliminacion_hard' agregada",
            )
        if "eliminado_por_user_id" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro "
                "ADD COLUMN eliminado_por_user_id VARCHAR(36)"
            ))
            logger.info(
                "Master migrate: columna 'eliminado_por_user_id' agregada",
            )
        if "zip_descargado_sha256" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro "
                "ADD COLUMN zip_descargado_sha256 VARCHAR(64)"
            ))
            logger.info(
                "Master migrate: columna 'zip_descargado_sha256' agregada",
            )

        # empresa_eliminacion_log: la creación se hace vía
        # BaseMaster.metadata.create_all() que ya corre antes de esta
        # función en `_ensure_master`, pero para ser explícitos y
        # sobrevivir a un rollback parcial, la creamos también aquí
        # con IF NOT EXISTS.
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS empresa_eliminacion_log ("
            "id VARCHAR(36) PRIMARY KEY, "
            "empresa_rut VARCHAR(12) NOT NULL, "
            "evento VARCHAR(30) NOT NULL, "
            "user_id VARCHAR(36) NOT NULL, "
            "user_email VARCHAR(120), "
            "timestamp DATETIME, "
            "detalle_json TEXT"
            ")"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_elim_log_rut "
            "ON empresa_eliminacion_log (empresa_rut)"
        ))

        # ── Representante legal en empresa_registro ──
        if "representante_legal_nombre" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro "
                "ADD COLUMN representante_legal_nombre VARCHAR(80)"
            ))
            logger.info(
                "Master migrate: columna 'representante_legal_nombre' "
                "agregada a empresa_registro",
            )
        if "representante_legal_rut" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro "
                "ADD COLUMN representante_legal_rut VARCHAR(12)"
            ))
            logger.info(
                "Master migrate: columna 'representante_legal_rut' "
                "agregada a empresa_registro",
            )
        if "representante_legal_email" not in cols:
            conn.execute(text(
                "ALTER TABLE empresa_registro "
                "ADD COLUMN representante_legal_email VARCHAR(120)"
            ))
            logger.info(
                "Master migrate: columna 'representante_legal_email' "
                "agregada a empresa_registro",
            )

        # ── Migración de usuario_auth: slug por RUT ──
        #
        # Hasta ahora la tabla tenía UNIQUE(email) — un correo solo podía
        # existir una vez en toda la plataforma. Eso chocaba con el caso
        # real: un representante legal puede ser master de N empresas con
        # el mismo correo. Movemos el scope de unicidad a (empresa_rut,
        # email) alineado con el namespacing por RUT del filesystem
        # (data/{rut}/...) y de las URLs (/{rut}/login).
        #
        # SQLite no soporta DROP/ADD CONSTRAINT, así que recreamos la tabla
        # cuando detectamos el schema viejo. Idempotente: si ya está
        # migrado (UNIQUE(empresa_rut, email) presente), no hace nada.
        ua_cols = {
            row[1] for row in conn.execute(
                text("PRAGMA table_info(usuario_auth)"),
            )
        }
        needs_rut_personal = "rut_personal" not in ua_cols
        schema_sql_row = conn.execute(text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='usuario_auth'"
        )).first()
        schema_sql = (schema_sql_row[0] if schema_sql_row else "") or ""
        # El constraint viejo venía como "UNIQUE (email)" sin empresa_rut.
        has_old_unique = (
            "UNIQUE (email)" in schema_sql
            and "empresa_rut" not in schema_sql.split("UNIQUE (email)")[0][-40:]
        )
        # Detectar si ya tiene el UNIQUE nuevo (idempotencia).
        has_new_unique = "uq_usuario_auth_empresa_email" in schema_sql or (
            "UNIQUE (empresa_rut, email)" in schema_sql
        )

        if has_old_unique and not has_new_unique:
            # Recrear tabla con el nuevo UNIQUE compuesto + rut_personal.
            conn.execute(text(
                "CREATE TABLE usuario_auth_new ("
                "id VARCHAR(36) NOT NULL PRIMARY KEY, "
                "empresa_rut VARCHAR(12) NOT NULL, "
                "email VARCHAR(120) NOT NULL, "
                "rut_personal VARCHAR(12), "
                "nombre VARCHAR(80) NOT NULL, "
                "password_hash VARCHAR(255) NOT NULL, "
                "rol VARCHAR(20) NOT NULL, "
                "activo BOOLEAN NOT NULL, "
                "created_at DATETIME NOT NULL, "
                "CONSTRAINT uq_usuario_auth_empresa_email "
                "UNIQUE (empresa_rut, email)"
                ")"
            ))
            # Copiar datos existentes. rut_personal queda NULL (se rellena
            # después al seedear / re-crear usuarios con los nuevos forms).
            conn.execute(text(
                "INSERT INTO usuario_auth_new "
                "(id, empresa_rut, email, rut_personal, nombre, "
                "password_hash, rol, activo, created_at) "
                "SELECT id, empresa_rut, email, NULL, nombre, "
                "password_hash, rol, activo, created_at "
                "FROM usuario_auth"
            ))
            conn.execute(text("DROP TABLE usuario_auth"))
            conn.execute(text(
                "ALTER TABLE usuario_auth_new RENAME TO usuario_auth"
            ))
            logger.info(
                "Master migrate: usuario_auth recreada con UNIQUE(empresa_rut, "
                "email) + columna rut_personal",
            )
        elif needs_rut_personal:
            # Tabla ya tiene el UNIQUE correcto pero falta rut_personal
            # (caso: la tabla fue creada por create_all con el modelo nuevo
            # antes de que hubiera datos; poco probable pero cubierto).
            conn.execute(text(
                "ALTER TABLE usuario_auth ADD COLUMN rut_personal VARCHAR(12)"
            ))
            logger.info(
                "Master migrate: columna 'rut_personal' agregada a usuario_auth",
            )

        # ── Migración must_change_password + password_changed_at ──
        # Idempotente: solo agrega las columnas si no existen.
        # Re-leemos las columnas porque arriba pudimos haber recreado la tabla.
        ua_cols = {
            row[1] for row in conn.execute(
                text("PRAGMA table_info(usuario_auth)"),
            )
        }
        if "must_change_password" not in ua_cols:
            conn.execute(text(
                "ALTER TABLE usuario_auth ADD COLUMN "
                "must_change_password BOOLEAN NOT NULL DEFAULT 0"
            ))
            logger.info(
                "Master migrate: columna 'must_change_password' agregada a usuario_auth",
            )
        if "password_changed_at" not in ua_cols:
            conn.execute(text(
                "ALTER TABLE usuario_auth ADD COLUMN "
                "password_changed_at DATETIME"
            ))
            logger.info(
                "Master migrate: columna 'password_changed_at' agregada a usuario_auth",
            )

        # ── Rename de valores de rol (idempotente) ──
        #
        # Antes solo había ``admin_empresa`` y ``admin_sucursal``. La
        # taxonomía nueva (crumbpos.core.roles) introduce
        # ``master_client`` y ``administrador_tienda``. Los dueños
        # históricos (``admin_empresa``) pasan a ``master_client`` —
        # todos los users que ya existían eran dueños, porque el form
        # viejo era "admin inicial de la empresa".
        #
        # Los aliases siguen siendo válidos en código (``normalizar()``
        # los traduce), pero queremos los canónicos en BD para que las
        # queries no mezclen strings. Si alguien quiere un ``administrador``
        # regular (no dueño), el master_client lo crea después con el
        # rol canónico nuevo.
        _rename_roles_sql(conn, "usuario_auth")


def get_master_session() -> Session:
    """Get a raw master DB session (no FastAPI dependency)."""
    _ensure_master()
    return _MasterSessionFactory()


def get_master_db():
    """FastAPI dependency: yields master DB session."""
    _ensure_master()
    db = _MasterSessionFactory()
    try:
        yield db
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════
# EMPRESA DB ENGINE / SESSION (cached per rut+ambiente)
# ══════════════════════════════════════════════════════════════════

_empresa_engines: dict[tuple[str, str], object] = {}
_empresa_session_factories: dict[tuple[str, str], object] = {}


def _empresa_db_path(rut: str, ambiente: str) -> Path:
    """Resolve path to empresa-specific database file.

    Validación estricta de `rut` contra regex XXXXXXX-X. Sin esto, un
    `rut="../../etc"` produciría path traversal y lectura/creación de DB
    fuera de `DATA_DIR`. Validación delegada a `utils.rut.validar_formato_rut`.
    """
    from crumbpos.utils.rut import validar_formato_rut
    rut_clean = validar_formato_rut(rut)
    if ambiente not in ("certificacion", "produccion"):
        raise ValueError(f"Ambiente inválido: {ambiente}. Debe ser 'certificacion' o 'produccion'")
    return DATA_DIR / rut_clean / f"{ambiente}.db"


def _migrate_empresa_schema(engine):
    """Agrega columnas nuevas a tablas de empresa si no existen (idempotente).

    `Base.metadata.create_all` solo crea tablas faltantes — NO agrega
    columnas a tablas preexistentes. Esta función cubre ese gap usando
    PRAGMA table_info + ALTER TABLE para SQLite.

    Cualquier columna nueva que se sume a un modelo de empresa debe
    registrarse aquí para que las DBs viejas la reciban al primer acceso.
    """
    with engine.begin() as conn:
        # ── caf_folio: sucursal_id (asignación CAF→sucursal, legacy) ──
        caf_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(caf_folio)"))
        }
        if caf_cols and "sucursal_id" not in caf_cols:
            conn.execute(text(
                "ALTER TABLE caf_folio ADD COLUMN sucursal_id VARCHAR(36)"
            ))
            # Recrear el índice viejo si existe, cambiando por uno que incluya sucursal_id.
            conn.execute(text("DROP INDEX IF EXISTS ix_caf_empresa_tipo"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_caf_empresa_tipo_sucursal "
                "ON caf_folio (empresa_id, tipo_dte, sucursal_id, estado)"
            ))
            logger.info("Empresa migrate: caf_folio.sucursal_id agregada")

        # ── caf_asignacion: backfill desde caf_folio (legacy → tramos) ──
        #
        # ``Base.metadata.create_all`` ya creó la tabla ``caf_asignacion``
        # vacía. La migración funcional es: por cada ``caf_folio`` existente
        # que aún no tenga ninguna asignación, generar UN tramo único
        # cubriendo todo el rango con el ``sucursal_id`` y ``folio_actual``
        # del CAF padre. Esto preserva el comportamiento previo al cambio
        # (un CAF = un dueño, todo el rango) sin que el master tenga que
        # tocar nada.
        #
        # Idempotente: si ya existe al menos una asignación para un caf_id,
        # no se crea nada. El uso de ``INSERT ... WHERE NOT EXISTS`` lo
        # garantiza incluso si la migración corre N veces (cada arranque
        # del proceso).
        asig_exists = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='caf_asignacion'"
        )).first()
        if caf_cols and asig_exists:
            result = conn.execute(text(
                "INSERT INTO caf_asignacion ("
                "id, caf_id, sucursal_id, rango_desde, rango_hasta, "
                "folio_actual, estado, created_at, updated_at) "
                "SELECT "
                "  lower(hex(randomblob(4))) || '-' || "
                "  lower(hex(randomblob(2))) || '-4' || "
                "  substr(lower(hex(randomblob(2))), 2) || '-' || "
                "  substr('89ab', abs(random()) % 4 + 1, 1) || "
                "  substr(lower(hex(randomblob(2))), 2) || '-' || "
                "  lower(hex(randomblob(6))), "
                "  c.id, c.sucursal_id, c.rango_desde, c.rango_hasta, "
                "  c.folio_actual, c.estado, "
                "  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                "FROM caf_folio c "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM caf_asignacion a WHERE a.caf_id = c.id"
                ")"
            ))
            if result.rowcount:
                logger.info(
                    "Empresa migrate: caf_asignacion backfill — %d tramo(s) "
                    "creado(s) desde caf_folio existentes",
                    result.rowcount,
                )

        # ── caf_folio: ambiente (marcador cert/prod) ──────────────────────
        # C1: un CAF autorizado en certificación no debe consumirse en
        # producción y viceversa. La columna es NOT NULL DEFAULT 'certificacion'
        # para que SQLite asigne un valor coherente al agregar la columna;
        # el UPDATE posterior la corrige al ambiente real de la empresa.
        if caf_cols and "ambiente" not in caf_cols:
            conn.execute(text(
                "ALTER TABLE caf_folio "
                "ADD COLUMN ambiente VARCHAR(15) NOT NULL DEFAULT 'certificacion'"
            ))
            # Backfill: la empresa sabe en qué ambiente opera esta DB.
            # COALESCE por si algún caf_folio tiene empresa_id huérfano.
            conn.execute(text(
                "UPDATE caf_folio SET ambiente = COALESCE("
                "  (SELECT e.ambiente_sii FROM empresa e "
                "   WHERE e.id = caf_folio.empresa_id),"
                "  'certificacion'"
                ")"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_caf_empresa_tipo_ambiente "
                "ON caf_folio (empresa_id, tipo_dte, ambiente, estado)"
            ))
            logger.info(
                "Empresa migrate: caf_folio.ambiente agregada "
                "+ backfill desde empresa.ambiente_sii"
            )

        # ── certificacion_caso: substates EPR / declarar avance / aprobado ──
        caso_cols = {
            row[1] for row in conn.execute(
                text("PRAGMA table_info(certificacion_caso)"),
            )
        }
        if caso_cols:
            if "avance_declarado_at" not in caso_cols:
                conn.execute(text(
                    "ALTER TABLE certificacion_caso "
                    "ADD COLUMN avance_declarado_at DATETIME"
                ))
                logger.info(
                    "Empresa migrate: certificacion_caso.avance_declarado_at agregada"
                )
            if "aprobado_at" not in caso_cols:
                conn.execute(text(
                    "ALTER TABLE certificacion_caso ADD COLUMN aprobado_at DATETIME"
                ))
                logger.info(
                    "Empresa migrate: certificacion_caso.aprobado_at agregada"
                )
            if "observaciones" not in caso_cols:
                conn.execute(text(
                    "ALTER TABLE certificacion_caso ADD COLUMN observaciones TEXT"
                ))
                logger.info(
                    "Empresa migrate: certificacion_caso.observaciones agregada"
                )

        # ── certificacion_libro: xml + substates EPR / declarar avance / aprobado ──
        libro_cols = {
            row[1] for row in conn.execute(
                text("PRAGMA table_info(certificacion_libro)"),
            )
        }
        if libro_cols:
            if "xml_libro" not in libro_cols:
                conn.execute(text(
                    "ALTER TABLE certificacion_libro ADD COLUMN xml_libro TEXT"
                ))
                logger.info(
                    "Empresa migrate: certificacion_libro.xml_libro agregada"
                )
            if "avance_declarado_at" not in libro_cols:
                conn.execute(text(
                    "ALTER TABLE certificacion_libro "
                    "ADD COLUMN avance_declarado_at DATETIME"
                ))
                logger.info(
                    "Empresa migrate: certificacion_libro.avance_declarado_at agregada"
                )
            if "aprobado_at" not in libro_cols:
                conn.execute(text(
                    "ALTER TABLE certificacion_libro ADD COLUMN aprobado_at DATETIME"
                ))
                logger.info(
                    "Empresa migrate: certificacion_libro.aprobado_at agregada"
                )
            if "observaciones" not in libro_cols:
                conn.execute(text(
                    "ALTER TABLE certificacion_libro ADD COLUMN observaciones TEXT"
                ))
                logger.info(
                    "Empresa migrate: certificacion_libro.observaciones agregada"
                )
            # ``primer_envio_sii_at`` marca la primera vez que el SII
            # aceptó el upload de este libro (trackid válido). Sobrevive
            # a ``reiniciar_envio_libro`` — si tiene valor, los re-envíos
            # posteriores se generan con ``TipoEnvio=AJUSTE`` en vez de
            # ``TOTAL`` para evitar el rechazo LNC ("Tipo de Envío No
            # Corresponde") que devuelve el SII cuando ya tiene registrado
            # un TOTAL para el mismo N°Atención+Periodo+TipoLibro.
            if "primer_envio_sii_at" not in libro_cols:
                conn.execute(text(
                    "ALTER TABLE certificacion_libro "
                    "ADD COLUMN primer_envio_sii_at DATETIME"
                ))
                logger.info(
                    "Empresa migrate: certificacion_libro.primer_envio_sii_at agregada"
                )
                # Backfill one-shot: libros que ya fueron enviados antes
                # del fix TipoEnvio=AJUSTE tienen ``enviado_at`` poblado
                # pero la columna nueva nace NULL. Sin este backfill, el
                # próximo re-envío sale como TOTAL y el SII rechaza con
                # LNC. Copiamos ``enviado_at → primer_envio_sii_at`` para
                # que los re-envíos salgan como AJUSTE.
                # Corre SOLO cuando se agrega la columna (dentro del if)
                # — es idempotente por construcción: la segunda vez que
                # corra el migrate, la columna ya existe y no entramos
                # aquí, así que nunca pisamos valores existentes.
                result = conn.execute(text(
                    "UPDATE certificacion_libro "
                    "SET primer_envio_sii_at = enviado_at "
                    "WHERE enviado_at IS NOT NULL "
                    "AND primer_envio_sii_at IS NULL"
                ))
                if result.rowcount:
                    logger.info(
                        "Empresa migrate: backfill primer_envio_sii_at en %d libros "
                        "ya enviados (para que los re-envíos usen TipoEnvio=AJUSTE)",
                        result.rowcount,
                    )

        # ── Rename de valores de rol en la tabla usuario (tenant) ──
        # Mismo criterio que en master.db: traducir strings viejos al
        # canónico (master_client / administrador_tienda). Idempotente.
        usuario_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(usuario)"))
        }
        if "rol" in usuario_cols:
            _rename_roles_sql(conn, "usuario")

        # ── dte_emitido: trazabilidad (usuario, caja, IP, user agent) + timestamp_envio ──
        # Idempotente: solo agrega columnas si no existen.
        dte_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(dte_emitido)"))
        }
        if dte_cols:
            if "usuario_id" not in dte_cols:
                conn.execute(text("ALTER TABLE dte_emitido ADD COLUMN usuario_id VARCHAR(36)"))
                logger.info("Empresa migrate: dte_emitido.usuario_id agregada")
            if "caja_id" not in dte_cols:
                conn.execute(text("ALTER TABLE dte_emitido ADD COLUMN caja_id VARCHAR(36)"))
                logger.info("Empresa migrate: dte_emitido.caja_id agregada")
            if "ip_origen" not in dte_cols:
                conn.execute(text("ALTER TABLE dte_emitido ADD COLUMN ip_origen VARCHAR(45)"))
                logger.info("Empresa migrate: dte_emitido.ip_origen agregada")
            if "user_agent" not in dte_cols:
                conn.execute(text("ALTER TABLE dte_emitido ADD COLUMN user_agent VARCHAR(255)"))
                logger.info("Empresa migrate: dte_emitido.user_agent agregada")
            if "timestamp_envio" not in dte_cols:
                conn.execute(text("ALTER TABLE dte_emitido ADD COLUMN timestamp_envio DATETIME"))
                logger.info("Empresa migrate: dte_emitido.timestamp_envio agregada")

        # ── sucursal: cdg_sii_sucursal (código numérico SII de sucursal) ──────
        # Campo INTEGER nullable que va en <CdgSIISucur> del XML del DTE.
        # Distinto de sii_sucursal (texto libre = Unidad Regional del SII).
        suc_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(sucursal)"))
        }
        if suc_cols and "cdg_sii_sucursal" not in suc_cols:
            conn.execute(text(
                "ALTER TABLE sucursal ADD COLUMN cdg_sii_sucursal INTEGER"
            ))
            logger.info("Empresa migrate: sucursal.cdg_sii_sucursal agregada")

        # ── B2: WORM — trigger que bloquea DELETE en dte_emitido < 6 años ──
        # La Resolución Exenta SII N°74 (2017) obliga a conservar los DTEs
        # electrónicos por 6 años desde su emisión. Este trigger es la
        # última barrera: bloquea DELETE a nivel de motor SQLite antes de
        # que ningún código de aplicación pueda eliminar un DTE reciente.
        #
        # Idempotente: CREATE TRIGGER IF NOT EXISTS.
        # Cálculo: julianday('now') − julianday(OLD.fecha_emision) < 2191.5
        # (6 * 365.25 días).
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_dte_emitido_worm "
            "BEFORE DELETE ON dte_emitido "
            "BEGIN "
            "  SELECT CASE "
            "    WHEN julianday('now') - julianday(OLD.fecha_emision) < 2191.5 "
            "    THEN RAISE(ABORT, "
            "      'B2-WORM: No se puede eliminar un DTE con menos de 6 anios "
            "de antiguedad (Resolucion SII N74/2017)') "
            "  END; "
            "END"
        ))

        # ── B1: auditoria_evento — triggers append-only ──────────────────
        # La tabla es de solo-inserción. Estos triggers bloquean UPDATE y
        # DELETE a nivel de motor SQLite, impidiendo que ningún proceso
        # (incluyendo acceso directo a la DB) modifique o elimine un
        # registro de auditoría ya escrito.
        audit_table_exists = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='auditoria_evento'"
        )).first()
        if audit_table_exists:
            conn.execute(text(
                "CREATE TRIGGER IF NOT EXISTS trg_auditoria_no_update "
                "BEFORE UPDATE ON auditoria_evento "
                "BEGIN "
                "  SELECT RAISE(ABORT, "
                "    'B1: La tabla auditoria_evento es append-only. "
                "UPDATE no permitido.'); "
                "END"
            ))
            conn.execute(text(
                "CREATE TRIGGER IF NOT EXISTS trg_auditoria_no_delete "
                "BEFORE DELETE ON auditoria_evento "
                "BEGIN "
                "  SELECT RAISE(ABORT, "
                "    'B1: La tabla auditoria_evento es append-only. "
                "DELETE no permitido.'); "
                "END"
            ))
            logger.info(
                "Empresa migrate: triggers B1 (auditoria append-only) "
                "+ B2 (dte_emitido WORM 6 años) instalados"
            )


def get_empresa_engine(rut: str, ambiente: str):
    """Get or create SQLAlchemy engine for empresa/ambiente.

    Al crear el engine por primera vez, asegura que todas las tablas
    del modelo existan (create_all es idempotente — solo crea las que
    falten). Esto permite agregar tablas nuevas sin escribir migraciones
    manuales: las DBs ya existentes reciben las tablas nuevas al
    primer acceso.

    Para columnas nuevas en tablas preexistentes (que create_all NO
    agrega), `_migrate_empresa_schema` corre justo después con ALTER
    TABLE idempotente.
    """
    key = (rut, ambiente)
    if key not in _empresa_engines:
        db_path = _empresa_db_path(rut, ambiente)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={
                "check_same_thread": False,
                # Busy timeout: si otro proceso/hilo tiene el write lock,
                # SQLite reintenta durante 10 segundos antes de fallar.
                # Necesario con WAL + BEGIN IMMEDIATE para que las
                # transacciones concurrentes esperen en vez de fallar.
                "timeout": 10,
            },
            echo=False,
        )

        # D2: WAL mode + synchronous=FULL en cada conexión nueva.
        # WAL permite lecturas concurrentes mientras hay escrituras activas.
        # synchronous=FULL garantiza que cada commit quede físicamente en disco
        # antes de continuar — crítico para datos contables con valor legal (DTEs,
        # libros SII). Rendimiento: ~2× más lento que NORMAL en escritura, pero
        # en SQLite el cuello de botella es habitualmente el round-trip HTTP, no
        # el fsync, así que el impacto práctico en POS es mínimo.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.close()

        # Import local para evitar ciclo con crumbpos.db.models
        from crumbpos.db.models import Base
        Base.metadata.create_all(bind=engine)
        _migrate_empresa_schema(engine)
        _ensure_empresa_row_seeded(engine, rut, ambiente)
        _ensure_casa_matriz_seeded(engine, rut, ambiente)
        _empresa_engines[key] = engine
    return _empresa_engines[key]


def _ensure_empresa_row_seeded(engine, rut: str, ambiente: str) -> None:
    """Defensa en profundidad: garantiza que la fila Empresa exista.

    Si el ``EmpresaRegistro`` existe en master.db pero la fila ``Empresa``
    está ausente de la BD de la empresa, inserta un stub mínimo con RUT
    + razón social del registro + campos fiscales vacíos. Los campos
    vacíos se llenan más adelante cuando el wizard envía ``datos_setup``
    vía ``PATCH /api/certificacion/runs/...`` (el handler del router
    upsertea la fila con los valores reales del formulario).

    Esta función se encarga del caso en que ``data/{rut}/`` fue eliminada
    a mano o por un flujo que dejó el estado inconsistente (p. ej. restore
    parcial desde papelera, o ``rmtree`` seguido de un re-registro en
    master sin pasar por ``provision_empresa``). Sin este seed, cualquier
    endpoint que pase por ``get_tenant`` tira 500 "Empresa ... no
    inicializada en BD ..." — que es exactamente el síntoma que observamos
    con 77829149-5 en 2026-04-21.
    """
    # Import local para evitar ciclos.
    from crumbpos.db.models import Empresa
    import uuid as _uuid

    # Leer registro del master (si no existe, no hacemos nada — la empresa
    # aún no fue creada y este path no debería correr).
    try:
        registro = get_empresa_registro(rut)
    except Exception:
        return
    if registro is None:
        return

    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        existing = session.query(Empresa).filter(Empresa.rut == rut).first()
        if existing is not None:
            return  # ya está, nada que hacer

        # Stub mínimo: los campos NOT NULL se llenan con "" y se
        # sobreescriben cuando el wizard manda datos_setup.
        session.add(Empresa(
            id=str(_uuid.uuid4()),
            rut=rut,
            razon_social=registro.razon_social or "",
            giro="",
            direccion="",
            comuna="",
            ciudad="",
            ambiente_sii=ambiente,
        ))
        session.commit()
    except Exception:
        session.rollback()
        # Silenciamos el error — el self-heal es best-effort; si falla,
        # el caller verá el 500 original y al menos sabemos que este path
        # se intentó. Logs de SQLAlchemy quedan capturados igual.
    finally:
        session.close()


def _ensure_casa_matriz_seeded(engine, rut: str, ambiente: str) -> None:
    """Defensa en profundidad: garantiza que la empresa tenga ≥1 sucursal.

    Toda empresa debe tener al menos una sucursal para que:
      · el módulo de CAFs pueda asignar folios a una sucursal real
        (además del "pool del server"),
      · las emisiones de DTE lleven dirección de emisor válida
        (``Sucursal.direccion`` override la de la Empresa),
      · el POS de caja pueda loguearse contra una sucursal concreta.

    Si la BD de la empresa quedó sin sucursales (caso observado en
    77829149-5 tras el reinicio de certificación: la Casa Matriz se
    perdió en el flujo de reset), insertamos una "Casa Matriz" con los
    datos actuales de la Empresa. Si esos datos están vacíos (empresa
    recién seedeada por ``_ensure_empresa_row_seeded``), Casa Matriz
    nace con strings vacíos y se rellena más adelante cuando el master
    los complete desde su consola.
    """
    from crumbpos.db.models import Empresa, Sucursal

    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        empresa = session.query(Empresa).filter(Empresa.rut == rut).first()
        if empresa is None:
            # La fila Empresa aún no existe — la función hermana
            # (_ensure_empresa_row_seeded) la crea. En un próximo acceso
            # este seed correrá. No hay nada que hacer ahora.
            return

        existente = session.query(Sucursal).filter(
            Sucursal.empresa_id == empresa.id,
        ).first()
        if existente is not None:
            return  # ya hay al menos una sucursal, nada que hacer

        session.add(Sucursal(
            empresa_id=empresa.id,
            nombre="Casa Matriz",
            codigo="001",
            direccion=empresa.direccion or "",
            comuna=empresa.comuna or "",
            ciudad=empresa.ciudad or "",
        ))
        session.commit()
        logger.info(
            "Self-heal: Casa Matriz creada para %s/%s (empresa sin sucursales)",
            rut, ambiente,
        )
    except Exception:
        session.rollback()
        # Best-effort como su función hermana. Si falla, el dropdown
        # sigue mostrando solo "Pool del server" pero nada operativo
        # se rompe — el master puede crear sucursales desde su consola
        # cuando el módulo exista.
    finally:
        session.close()


def get_empresa_session_factory(rut: str, ambiente: str):
    """Get or create session factory for empresa/ambiente."""
    key = (rut, ambiente)
    if key not in _empresa_session_factories:
        engine = get_empresa_engine(rut, ambiente)
        _empresa_session_factories[key] = sessionmaker(
            bind=engine, autocommit=False, autoflush=False,
        )
    return _empresa_session_factories[key]


def get_empresa_db_session(rut: str, ambiente: str) -> Session:
    """Get a raw empresa DB session (no FastAPI dependency)."""
    factory = get_empresa_session_factory(rut, ambiente)
    return factory()


def get_empresa_db(rut: str, ambiente: str):
    """Generator that yields empresa DB session (for FastAPI Depends)."""
    factory = get_empresa_session_factory(rut, ambiente)
    db = factory()
    try:
        yield db
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════
# PROVISIONING — Crear empresa completa
# ══════════════════════════════════════════════════════════════════

def provision_empresa(
    rut: str,
    razon_social: str,
    giro: str,
    direccion: str,
    comuna: str,
    ciudad: str,
    admin_email: str,
    admin_password_hash: str,
    admin_nombre: str,
    acteco: int | None = None,
    sucursales: list[dict] | None = None,
    plan: str = "full_free",
    admin_rut_personal: str | None = None,
) -> tuple[str, str]:
    """Provisiona una nueva empresa completa.

    1. Crea registro en master.db (incluye representante_legal_* si
       viene ``admin_rut_personal``, porque ese usuario ES el master
       cliente = representante legal).
    2. Crea certificacion.db y produccion.db con todas las tablas.
    3. Inserta Empresa + admin Usuario + Sucursal "Casa Matriz" en cada BD.
    4. Crea sucursales adicionales si se proporcionan.

    Args:
        sucursales: lista de dicts con {nombre, codigo, direccion, comuna, ciudad,
            sii_sucursal, cdg_sii_sucursal (int|None)}
        admin_rut_personal: RUT personal del master cliente (el dueño/representante
            legal). Se guarda en ``UsuarioAuth.rut_personal`` para la
            consola ``/{rut}/login``, y se copia a
            ``EmpresaRegistro.representante_legal_*`` para los DTEs.

    Returns:
        (empresa_id, user_id)

    Raises:
        ValueError: si la empresa ya existe
    """
    from crumbpos.db.models import Base, Empresa, Usuario, Sucursal

    _ensure_master()
    empresa_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    # ── 1. Registrar en master.db ──
    master = _MasterSessionFactory()
    try:
        existing = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if existing:
            raise ValueError(f"Empresa {rut} ya está registrada")

        if plan not in PLANES_DISPONIBLES:
            raise ValueError(f"Plan inválido: {plan}")

        master.add(EmpresaRegistro(
            rut=rut,
            razon_social=razon_social,
            ambiente_activo="certificacion",
            etapa="pendiente_certificacion",
            plan=plan,
            # El master cliente es el representante legal de la empresa
            # — se precargan estos campos desde el alta para que el
            # wizard de certificación no tenga que volver a pedirlos.
            representante_legal_nombre=admin_nombre,
            representante_legal_rut=admin_rut_personal,
            representante_legal_email=admin_email,
        ))
        master.add(UsuarioAuth(
            id=user_id,
            empresa_rut=rut,
            email=admin_email,
            nombre=admin_nombre,
            rut_personal=admin_rut_personal,
            password_hash=admin_password_hash,
            # El usuario creado junto con la empresa es SIEMPRE el
            # dueño/representante legal: rol master_client. Sub-usuarios
            # (administrador / administrador_tienda / cajero) se crean
            # después desde la consola. Ver ``crumbpos.core.roles``.
            rol="master_client",
            # Password generada automáticamente — forzar cambio en primer login.
            must_change_password=True,
        ))
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()

    # ── 2. Crear ambas BDs de empresa ──
    for ambiente in ("certificacion", "produccion"):
        engine = get_empresa_engine(rut, ambiente)
        Base.metadata.create_all(bind=engine)

        session = get_empresa_db_session(rut, ambiente)
        try:
            # Insertar Empresa (para que FKs funcionen)
            session.add(Empresa(
                id=empresa_id,
                rut=rut,
                razon_social=razon_social,
                giro=giro,
                acteco=acteco,
                direccion=direccion,
                comuna=comuna,
                ciudad=ciudad,
                ambiente_sii=ambiente,
            ))

            # Insertar master_client user (para queries internas del tenant).
            # Mismo rol que en master.db — ver comentario en la inserción
            # de UsuarioAuth más arriba.
            session.add(Usuario(
                id=user_id,
                empresa_id=empresa_id,
                email=admin_email,
                nombre=admin_nombre,
                password_hash=admin_password_hash,
                rol="master_client",
            ))

            # Sucursal default: Casa Matriz (dirección de la empresa)
            session.add(Sucursal(
                empresa_id=empresa_id,
                nombre="Casa Matriz",
                codigo="001",
                direccion=direccion,
                comuna=comuna,
                ciudad=ciudad,
            ))

            # Sucursales adicionales
            if sucursales:
                for i, suc_data in enumerate(sucursales, start=2):
                    session.add(Sucursal(
                        empresa_id=empresa_id,
                        nombre=suc_data["nombre"],
                        codigo=suc_data.get("codigo") or f"{i:03d}",
                        direccion=suc_data["direccion"],
                        comuna=suc_data["comuna"],
                        ciudad=suc_data["ciudad"],
                        sii_sucursal=suc_data.get("sii_sucursal", "SANTIAGO ORIENTE"),
                        cdg_sii_sucursal=suc_data.get("cdg_sii_sucursal"),
                    ))

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    n_sucursales = 1 + (len(sucursales) if sucursales else 0)
    logger.info(
        "Empresa provisionada: %s (%s) — cert + prod DBs creadas, %d sucursal(es)",
        rut, razon_social, n_sucursales,
    )
    return empresa_id, user_id


def ensure_super_admin(email: str, password_hash: str, nombre: str) -> str:
    """Asegura que exista un super_admin en master.db.

    Busca en el namespace "SYSTEM" (empresa_rut="SYSTEM") porque el
    UNIQUE es (empresa_rut, email): el mismo correo puede existir como
    master cliente de empresas reales. El super_admin es un caso
    separado con empresa_rut="SYSTEM".

    Si ya existe, no hace nada. Si no, lo crea. Returns user_id.
    """
    _ensure_master()
    master = _MasterSessionFactory()
    try:
        existing = master.query(UsuarioAuth).filter(
            UsuarioAuth.empresa_rut == "SYSTEM",
            UsuarioAuth.email == email,
        ).first()
        if existing:
            return existing.id

        user_id = str(uuid.uuid4())
        master.add(UsuarioAuth(
            id=user_id,
            empresa_rut="SYSTEM",
            email=email,
            nombre=nombre,
            password_hash=password_hash,
            rol="super_admin",
        ))
        master.commit()
        logger.info("Super admin creado: %s", email)
        return user_id
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()


def cambiar_etapa(rut: str, nueva_etapa: str) -> str:
    """Actualiza la etapa de una empresa.

    Transiciones válidas:
      pendiente_certificacion → proceso_certificacion → produccion

    Returns la etapa resultante.
    """
    if nueva_etapa not in ETAPAS_VALIDAS:
        raise ValueError(f"Etapa inválida: {nueva_etapa}")

    _ensure_master()
    master = _MasterSessionFactory()
    try:
        registro = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if not registro:
            raise ValueError(f"Empresa {rut} no encontrada")

        registro.etapa = nueva_etapa
        # Cuando pasa a producción, el ambiente activo también se mueve
        if nueva_etapa == "produccion":
            registro.ambiente_activo = "produccion"
        master.commit()
        logger.info("Empresa %s → etapa: %s", rut, nueva_etapa)
        return nueva_etapa
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()


def cambiar_ambiente(rut: str, nuevo_ambiente: str) -> str:
    """Cambia el ambiente activo de una empresa (certificacion ↔ produccion).

    Returns el ambiente activo resultante.
    """
    if nuevo_ambiente not in ("certificacion", "produccion"):
        raise ValueError(f"Ambiente inválido: {nuevo_ambiente}")

    _ensure_master()
    master = _MasterSessionFactory()
    try:
        registro = master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
        if not registro:
            raise ValueError(f"Empresa {rut} no encontrada")

        registro.ambiente_activo = nuevo_ambiente
        master.commit()
        logger.info("Empresa %s → ambiente: %s", rut, nuevo_ambiente)
        return nuevo_ambiente
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()


def get_empresa_registro(rut: str) -> EmpresaRegistro | None:
    """Look up empresa in master DB."""
    _ensure_master()
    master = _MasterSessionFactory()
    try:
        return master.query(EmpresaRegistro).filter(
            EmpresaRegistro.rut == rut,
        ).first()
    finally:
        master.close()


def get_scheduler_estado(clave: str) -> str | None:
    """Lee el valor de una clave del scheduler en master.db.

    Devuelve ``None`` si la clave no existe o si la tabla aún no fue creada
    (primera ejecución antes de cualquier tarea de scheduler).
    """
    _ensure_master()
    session = _MasterSessionFactory()
    try:
        fila = (
            session.query(SchedulerEstado)
            .filter(SchedulerEstado.clave == clave)
            .first()
        )
        return fila.valor if fila else None
    except Exception:
        return None
    finally:
        session.close()


def set_scheduler_estado(clave: str, valor: str) -> None:
    """Escribe (upsert) el valor de una clave del scheduler en master.db."""
    _ensure_master()
    session = _MasterSessionFactory()
    try:
        fila = (
            session.query(SchedulerEstado)
            .filter(SchedulerEstado.clave == clave)
            .first()
        )
        if fila:
            fila.valor = valor
            fila.updated_at = datetime.now(timezone.utc)
        else:
            session.add(SchedulerEstado(clave=clave, valor=valor))
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error("set_scheduler_estado(%r): %s", clave, exc)
        raise
    finally:
        session.close()


def listar_empresas(
    incluir_eliminadas: bool = False,
) -> list[EmpresaRegistro]:
    """List registered empresas from master DB.

    Por defecto excluye las que están en papelera o eliminadas definitivamente
    — esas solo deben verse en el listado de la Papelera del super admin.
    Pasar `incluir_eliminadas=True` solo cuando el caller necesita el
    listado crudo (ej: auditoría, tooling interno).
    """
    _ensure_master()
    master = _MasterSessionFactory()
    try:
        q = master.query(EmpresaRegistro)
        if not incluir_eliminadas:
            q = q.filter(EmpresaRegistro.estado == "activa")
        return q.order_by(EmpresaRegistro.razon_social).all()
    finally:
        master.close()


def listar_papelera() -> list[EmpresaRegistro]:
    """Lista las empresas en soft-delete (papelera).

    Solo incluye estado='eliminada_soft'. Las que ya fueron eliminadas
    definitivamente (estado='eliminada_hard') NO aparecen aquí — ya no
    existen operativamente, solo quedan en empresa_eliminacion_log como
    tombstone de auditoría.
    """
    _ensure_master()
    master = _MasterSessionFactory()
    try:
        return master.query(EmpresaRegistro).filter(
            EmpresaRegistro.estado == "eliminada_soft",
        ).order_by(
            EmpresaRegistro.fecha_eliminacion_soft.desc(),
        ).all()
    finally:
        master.close()


def init_multi_tenant():
    """Initialize multi-tenant system.

    Called at application startup.
    Creates master.db and ensures tables exist.
    """
    _ensure_master()
    logger.info(
        "Multi-tenant system initialized. Data dir: %s", DATA_DIR,
    )
