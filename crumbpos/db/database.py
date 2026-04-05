"""Conexión a base de datos.

Soporta SQLite (desarrollo local / POS desktop) y PostgreSQL (cloud AWS).
Se configura con variable de entorno DATABASE_URL.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# Default: SQLite local para desarrollo
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./crumbpos.db"
)

# SQLite necesita check_same_thread=False para FastAPI async
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(DATABASE_URL, connect_args=connect_args, echo=False)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """Dependency de FastAPI: provee sesión de DB por request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_add_columns():
    """Add columns that may be missing in existing databases (no Alembic)."""
    import logging
    logger = logging.getLogger(__name__)

    migrations = [
        # DteEmitido: new fields for SII polling
        ("dte_emitido", "glosa_sii", "VARCHAR(255)"),
        ("dte_emitido", "fecha_consulta_sii", "DATETIME"),
        # DteEmitido: sucursal_id may have been NOT NULL before — SQLite can't ALTER
        # constraints, but the column already exists, so we only add if missing.
        # Stock: stock_minimo for inventory alerts
        ("stock", "stock_minimo", "REAL DEFAULT 0"),
    ]

    with engine.connect() as conn:
        for table, column, col_type in migrations:
            try:
                conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                    )
                )
                conn.commit()
                logger.info("Migration: added %s.%s", table, column)
            except Exception:
                # Column already exists — ignore
                try:
                    conn.rollback()
                except Exception:
                    pass


def init_db():
    """Crea todas las tablas. Usar solo en desarrollo."""
    Base.metadata.create_all(bind=engine)
    _migrate_add_columns()
