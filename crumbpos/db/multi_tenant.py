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
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    create_engine, String, Integer, Boolean, DateTime, Text, event,
)
from sqlalchemy.orm import (
    sessionmaker, DeclarativeBase, Mapped, mapped_column, Session,
)

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
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow,
    )


class UsuarioAuth(BaseMaster):
    """Usuarios para autenticación centralizada — solo en master.db.

    Roles:
      - super_admin: ve todas las empresas, gestiona el sistema
      - admin_empresa: administra su empresa, ve solo su data
      - admin_sucursal: administra sucursal(es) asignadas
      - cajero: solo POS en sucursales asignadas
    """
    __tablename__ = "usuario_auth"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    empresa_rut: Mapped[str] = mapped_column(
        String(12), nullable=False,
    )  # "SYSTEM" para super_admin
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    rol: Mapped[str] = mapped_column(String(20), nullable=False)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow,
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
    BaseMaster.metadata.create_all(bind=_master_engine)
    _MasterSessionFactory = sessionmaker(
        bind=_master_engine, autocommit=False, autoflush=False,
    )
    logger.info("Master DB initialized: %s", DATA_DIR / "master.db")


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
    """Resolve path to empresa-specific database file."""
    rut_clean = rut.strip()
    if ambiente not in ("certificacion", "produccion"):
        raise ValueError(f"Ambiente inválido: {ambiente}. Debe ser 'certificacion' o 'produccion'")
    return DATA_DIR / rut_clean / f"{ambiente}.db"


def get_empresa_engine(rut: str, ambiente: str):
    """Get or create SQLAlchemy engine for empresa/ambiente."""
    key = (rut, ambiente)
    if key not in _empresa_engines:
        db_path = _empresa_db_path(rut, ambiente)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            echo=False,
        )
        _empresa_engines[key] = engine
    return _empresa_engines[key]


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
) -> tuple[str, str]:
    """Provisiona una nueva empresa completa.

    1. Crea registro en master.db
    2. Crea certificacion.db y produccion.db con todas las tablas
    3. Inserta Empresa + admin Usuario + Sucursal "Casa Matriz" en cada BD
    4. Crea sucursales adicionales si se proporcionan

    Args:
        sucursales: lista de dicts con {nombre, codigo, direccion, comuna, ciudad, sii_sucursal}

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

        master.add(EmpresaRegistro(
            rut=rut,
            razon_social=razon_social,
            ambiente_activo="certificacion",
        ))
        master.add(UsuarioAuth(
            id=user_id,
            empresa_rut=rut,
            email=admin_email,
            nombre=admin_nombre,
            password_hash=admin_password_hash,
            rol="admin_empresa",
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

            # Insertar admin user (para queries internas)
            session.add(Usuario(
                id=user_id,
                empresa_id=empresa_id,
                email=admin_email,
                nombre=admin_nombre,
                password_hash=admin_password_hash,
                rol="admin_empresa",
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

    Si ya existe con ese email, no hace nada. Si no, lo crea.
    Returns user_id.
    """
    _ensure_master()
    master = _MasterSessionFactory()
    try:
        existing = master.query(UsuarioAuth).filter(
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


def listar_empresas() -> list[EmpresaRegistro]:
    """List all registered empresas from master DB."""
    _ensure_master()
    master = _MasterSessionFactory()
    try:
        return master.query(EmpresaRegistro).order_by(
            EmpresaRegistro.razon_social,
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
