"""Tests para el backfill de ``primer_envio_sii_at`` en
``_migrate_empresa_schema``.

Contexto: la columna ``CertificacionLibro.primer_envio_sii_at`` se
agregó para detectar re-envíos y emitir ``TipoEnvio=AJUSTE``. En DBs
preexistentes hay libros con ``enviado_at`` poblado pero la columna
recién se crea vacía — si no hacemos backfill, el próximo
``reiniciar_envio_libro + generar_libro + enviar_libro`` los mandará
como TOTAL de nuevo y el SII rechazará con LNC.

Regla del backfill (idempotente):
  - Al agregar la columna (y solo esa vez), para cada libro donde
    ``enviado_at IS NOT NULL`` y ``primer_envio_sii_at IS NULL``, copiar
    ``enviado_at → primer_envio_sii_at``.
  - Si la columna ya existía antes del migrate, no tocar nada (la
    data en el campo es la verdad).
"""
from __future__ import annotations

from sqlalchemy import create_engine, text

from crumbpos.db.models import Base
from crumbpos.db.multi_tenant import _migrate_empresa_schema


def _crear_db_sin_columna(engine):
    """Crea una DB con el schema original (sin primer_envio_sii_at).

    Simula una BD generada por una versión del código anterior al fix.
    Usa ``create_all`` y luego elimina la columna con un workaround
    (SQLite no soporta DROP COLUMN antes de 3.35; recreamos la tabla).
    """
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Renombrar tabla, crearla sin la columna, copiar datos.
        cols_info = list(conn.execute(text("PRAGMA table_info(certificacion_libro)")))
        cols_sin_nueva = [
            (row[1], row[2], row[3], row[5])
            for row in cols_info if row[1] != "primer_envio_sii_at"
        ]
        conn.execute(text("ALTER TABLE certificacion_libro RENAME TO _tmp_libro"))
        col_defs = []
        for name, tipo, notnull, pk in cols_sin_nueva:
            pieces = [f'"{name}"', tipo or "TEXT"]
            if notnull:
                pieces.append("NOT NULL")
            if pk:
                pieces.append("PRIMARY KEY")
            col_defs.append(" ".join(pieces))
        conn.execute(text(
            f"CREATE TABLE certificacion_libro ({', '.join(col_defs)})"
        ))
        col_names = ", ".join(f'"{c[0]}"' for c in cols_sin_nueva)
        conn.execute(text(
            f"INSERT INTO certificacion_libro ({col_names}) "
            f"SELECT {col_names} FROM _tmp_libro"
        ))
        conn.execute(text("DROP TABLE _tmp_libro"))


def _insertar_run(conn):
    conn.execute(text(
        "INSERT INTO certificacion_run (id, rut_empresa, estado, "
        "screen_actual, created_at, updated_at) "
        "VALUES ('run-1', '77829149-5', 'emitiendo', 3, "
        "'2026-04-23', '2026-04-23')"
    ))


def _insertar_libro(conn, libro_id, tipo, enviado_at, trackid="TRACK-X"):
    trackid_sql = "NULL" if trackid is None else f"'{trackid}'"
    enviado_sql = "NULL" if enviado_at is None else f"'{enviado_at}'"
    conn.execute(text(
        "INSERT INTO certificacion_libro (id, run_id, tipo_libro, estado, "
        "trackid, enviado_at, updated_at) "
        f"VALUES ('{libro_id}', 'run-1', '{tipo}', 'enviado', "
        f"{trackid_sql}, {enviado_sql}, '2026-04-23')"
    ))


def test_backfill_copia_enviado_at_cuando_se_agrega_columna(tmp_path):
    """Escenario real del cliente 77829149-5 el 2026-04-23:

    Libros enviados antes del fix TipoEnvio=AJUSTE tienen trackid +
    ``enviado_at`` pero ``primer_envio_sii_at=NULL`` (la columna no
    existía). El migrate debe poblarla con ``enviado_at`` para que el
    próximo re-envío salga como AJUSTE.
    """
    db_path = tmp_path / "cert.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    _crear_db_sin_columna(engine)

    with engine.begin() as conn:
        _insertar_run(conn)
        _insertar_libro(conn, "lib-vent", "ventas", "2026-04-23 14:10:24")
        _insertar_libro(conn, "lib-comp", "compras", "2026-04-23 14:10:06")
        _insertar_libro(conn, "lib-guia", "guias", "2026-04-23 14:10:16")

    _migrate_empresa_schema(engine)

    with engine.begin() as conn:
        rows = list(conn.execute(text(
            "SELECT id, enviado_at, primer_envio_sii_at "
            "FROM certificacion_libro ORDER BY id"
        )))

    # Cada libro enviado debe tener primer_envio_sii_at == enviado_at
    assert len(rows) == 3
    for libro_id, enviado_at, primer_envio in rows:
        assert primer_envio == enviado_at, (
            f"Libro {libro_id}: backfill debe copiar enviado_at "
            f"({enviado_at!r}) a primer_envio_sii_at ({primer_envio!r})."
        )


def test_backfill_no_toca_libros_sin_enviar(tmp_path):
    """Libros nunca enviados (``enviado_at IS NULL``) no reciben valor."""
    db_path = tmp_path / "cert.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    _crear_db_sin_columna(engine)

    with engine.begin() as conn:
        _insertar_run(conn)
        _insertar_libro(conn, "lib-pendiente", "ventas",
                        enviado_at=None, trackid=None)

    _migrate_empresa_schema(engine)

    with engine.begin() as conn:
        primer = conn.execute(text(
            "SELECT primer_envio_sii_at FROM certificacion_libro "
            "WHERE id='lib-pendiente'"
        )).scalar()

    assert primer is None, (
        "Libro nunca enviado debe quedar con primer_envio_sii_at=NULL "
        "(el próximo envío será su primer TOTAL legítimo)."
    )


def test_backfill_idempotente_segunda_corrida(tmp_path):
    """Correr el migrate dos veces seguidas no debe pisar valores.

    La segunda vez la columna ya existe; el bloque de backfill no
    debe ejecutarse. Esto protege contra borrar valores reales si en
    algún momento alguien modifica manualmente ``enviado_at`` sin
    querer tocar ``primer_envio_sii_at``.
    """
    db_path = tmp_path / "cert.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    _crear_db_sin_columna(engine)

    with engine.begin() as conn:
        _insertar_run(conn)
        _insertar_libro(conn, "lib-1", "ventas", "2026-04-23 14:10:24")

    _migrate_empresa_schema(engine)

    # Simular que el usuario reenvió y el enviado_at cambió, pero
    # primer_envio_sii_at debe conservar el valor original.
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE certificacion_libro SET enviado_at='2026-04-24 10:00:00' "
            "WHERE id='lib-1'"
        ))

    _migrate_empresa_schema(engine)

    with engine.begin() as conn:
        primer = conn.execute(text(
            "SELECT primer_envio_sii_at FROM certificacion_libro "
            "WHERE id='lib-1'"
        )).scalar()

    assert str(primer).startswith("2026-04-23"), (
        "Segunda corrida del migrate no debe pisar el valor original "
        "con un enviado_at más nuevo."
    )


def test_backfill_no_corre_si_columna_ya_existia(tmp_path):
    """Si la DB ya fue migrada antes (columna presente + valores),
    no debemos backfillear — la data existente es la verdad."""
    db_path = tmp_path / "cert.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    # DB NUEVA con la columna desde el origen (Base.metadata.create_all)
    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        _insertar_run(conn)
        # Libro con primer_envio_sii_at ya seteado manualmente a otro
        # valor distinto de enviado_at — representa un estado legítimo.
        conn.execute(text(
            "INSERT INTO certificacion_libro (id, run_id, tipo_libro, "
            "estado, trackid, enviado_at, primer_envio_sii_at, "
            "updated_at) "
            "VALUES ('lib-1', 'run-1', 'ventas', 'enviado', 'T1', "
            "'2026-04-24 10:00:00', '2026-04-20 09:00:00', "
            "'2026-04-23')"
        ))

    _migrate_empresa_schema(engine)

    with engine.begin() as conn:
        primer = conn.execute(text(
            "SELECT primer_envio_sii_at FROM certificacion_libro "
            "WHERE id='lib-1'"
        )).scalar()

    assert str(primer).startswith("2026-04-20"), (
        "Si la columna ya existía con valor, el migrate NO debe "
        "sobreescribirla con enviado_at."
    )
