"""Tests del modelo de subdivisión por sucursal — ``CafAsignacion``.

Cubre el nuevo flujo donde un CAF (rango total autorizado por el SII) se
descompone en uno o más **tramos** (``CafAsignacion``) que el master
cliente asigna a sucursales o al pool del server (``sucursal_id IS NULL``).

Reglas críticas validadas aquí:

  * **Pool inicial.** ``registrar_caf`` deja el CAF entero en un único
    tramo pool (sin sucursal asignada).
  * **Cobertura sin solapes.** ``configurar_asignaciones`` valida que los
    tramos enviados no se solapen y queden dentro del rango del CAF;
    rellena automáticamente los huecos con tramos pool.
  * **Inmutabilidad de folios consumidos.** Folios que ya fueron emitidos
    no se pueden mover entre dueños — la API rechaza con glosa explícita.
  * **Aislamiento por slice.** ``siguiente_folio(sucursal_id=X)`` consume
    SOLO de los tramos de X (o del pool si X is None). Si el slice se
    queda sin folios, levanta ``FoliosAgotadosError`` — sin fallback
    automático.
  * **Devolución al pool.** ``devolver_folios_de_sucursal_al_pool`` mueve
    todos los tramos vivos de una sucursal al pool y emite eventos
    ``CafEventoSync`` para que el POS de esa sucursal invalide su cache.
  * **set_folio bloqueado en CAFs subdivididos.** Si el CAF tiene tramos
    asignados a sucursales, ``set_folio`` rechaza la operación: mover el
    folio_actual mientras hay sucursales con cache abierto rompe la
    inmutabilidad.

Igual que ``test_caf_override_folio_inicial.py``, los tests corren sobre
SQLite in-memory; los CAFs son sintéticos (firma FRMA basura, valida
porque no se configura ``SII_PUBLIC_KEY_PATH``).
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.core.caf.caf_manager_db import CAFManagerDB, FoliosAgotadosError
from crumbpos.db.models import (
    Base,
    CafAsignacion,
    CafEventoSync,
    CafFolio,
    Empresa,
    Sucursal,
)


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════


def _caf_xml(
    tipo_dte: int,
    folio_desde: int,
    folio_hasta: int,
    rut: str = "77051056-2",
    fecha_autorizacion: str | None = None,
) -> bytes:
    """CAF XML mínimo aceptado por ``registrar_caf`` en tests.

    Sin firma SII real (FRMA dummy + sin ``SII_PUBLIC_KEY_PATH``). FA
    por defecto en hoy para pasar el check de vigencia.
    """
    fa = fecha_autorizacion or date.today().isoformat()
    dummy_pk_pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "dummy\n"
        "-----END RSA PRIVATE KEY-----"
    )
    dummy_pub_pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        "dummy\n"
        "-----END PUBLIC KEY-----"
    )
    xml = f"""<?xml version="1.0"?>
<AUTORIZACION>
<CAF version="1.0">
<DA>
<RE>{rut}</RE>
<RS>EMPRESA DE PRUEBA</RS>
<TD>{tipo_dte}</TD>
<RNG><D>{folio_desde}</D><H>{folio_hasta}</H></RNG>
<FA>{fa}</FA>
<RSAPK>
<M>dGVzdG1vZHVsdXM=</M>
<E>AQAB</E>
</RSAPK>
<IDK>100</IDK>
</DA>
<FRMA algoritmo="SHA1withRSA">ZHVtbXlmaXJtYQ==</FRMA>
</CAF>
<RSASK>{dummy_pk_pem}</RSASK>
<RSAPUBK>{dummy_pub_pem}</RSAPUBK>
</AUTORIZACION>"""
    return xml.encode("ISO-8859-1")


# ══════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def empresa(session):
    e = Empresa(
        id="emp-asig",
        rut="77051056-2",
        razon_social="EMPRESA DE PRUEBA",
        giro="PRUEBAS",
        direccion="CALLE FALSA 123",
        comuna="SANTIAGO",
        ciudad="SANTIAGO",
    )
    session.add(e)
    session.commit()
    return e


@pytest.fixture
def sucursal_a(session, empresa):
    s = Sucursal(
        id="suc-a",
        empresa_id=empresa.id,
        nombre="Casa Matriz",
        direccion="Matriz 100",
        comuna="Providencia",
        ciudad="Santiago",
    )
    session.add(s)
    session.commit()
    return s


@pytest.fixture
def sucursal_b(session, empresa):
    s = Sucursal(
        id="suc-b",
        empresa_id=empresa.id,
        nombre="Sucursal Centro",
        direccion="Centro 200",
        comuna="Santiago",
        ciudad="Santiago",
    )
    session.add(s)
    session.commit()
    return s


@pytest.fixture
def sucursal_inactiva(session, empresa):
    s = Sucursal(
        id="suc-off",
        empresa_id=empresa.id,
        nombre="Sucursal Cerrada",
        direccion="Cerrada 300",
        comuna="Santiago",
        ciudad="Santiago",
        activa=False,
    )
    session.add(s)
    session.commit()
    return s


@pytest.fixture
def mgr(session, empresa):
    return CAFManagerDB(session, empresa.id)


# ══════════════════════════════════════════════════════════════════
# Tests: setup pool inicial
# ══════════════════════════════════════════════════════════════════


def test_registrar_caf_crea_tramo_pool_inicial(mgr, session):
    """Al subir un CAF, queda un único tramo pool cubriendo todo el rango."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    asignaciones = (
        session.query(CafAsignacion)
        .filter_by(caf_id=info["id"])
        .all()
    )
    assert len(asignaciones) == 1
    a = asignaciones[0]
    assert a.sucursal_id is None  # pool del server
    assert a.rango_desde == 1
    assert a.rango_hasta == 30
    assert a.folio_actual == 1
    assert a.estado == "activo"


def test_registrar_caf_con_override_propaga_a_la_asignacion(mgr, session):
    """Override de folio inicial llega también al tramo pool inicial."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30), folio_inicial_override=15)
    session.commit()

    a = session.query(CafAsignacion).filter_by(caf_id=info["id"]).one()
    assert a.folio_actual == 15


def test_listar_asignaciones_tras_registrar(mgr, session):
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    tramos = mgr.listar_asignaciones(info["id"])
    assert len(tramos) == 1
    assert tramos[0]["sucursal_id"] is None
    assert tramos[0]["rango_desde"] == 1
    assert tramos[0]["rango_hasta"] == 30


# ══════════════════════════════════════════════════════════════════
# Tests: configurar_asignaciones — happy path
# ══════════════════════════════════════════════════════════════════


def test_configurar_un_tramo_a_sucursal_a(mgr, session, sucursal_a):
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    tramos = mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 30},
    ])
    session.commit()

    assert len(tramos) == 1
    assert tramos[0]["sucursal_id"] == sucursal_a.id
    assert tramos[0]["rango_desde"] == 1
    assert tramos[0]["rango_hasta"] == 30


def test_subdividir_en_dos_sucursales_y_pool(
    mgr, session, sucursal_a, sucursal_b,
):
    """1-30 → suc_a 1-10 + suc_b 11-15 + pool 16-30 (relleno automático)."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    tramos = mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 10},
        {"sucursal_id": sucursal_b.id, "rango_desde": 11, "rango_hasta": 15},
    ])
    session.commit()

    # Backend rellena el hueco 16-30 con un tramo pool.
    assert len(tramos) == 3
    asig_a = next(t for t in tramos if t["sucursal_id"] == sucursal_a.id)
    asig_b = next(t for t in tramos if t["sucursal_id"] == sucursal_b.id)
    asig_pool = next(t for t in tramos if t["sucursal_id"] is None)

    assert (asig_a["rango_desde"], asig_a["rango_hasta"]) == (1, 10)
    assert (asig_b["rango_desde"], asig_b["rango_hasta"]) == (11, 15)
    assert (asig_pool["rango_desde"], asig_pool["rango_hasta"]) == (16, 30)


def test_huecos_iniciales_y_finales_se_rellenan_con_pool(
    mgr, session, sucursal_a,
):
    """Tramo 11-15 en CAF 1-30 → pool 1-10 + suc_a 11-15 + pool 16-30."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    tramos = mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 11, "rango_hasta": 15},
    ])
    session.commit()

    assert len(tramos) == 3
    rangos = sorted([(t["rango_desde"], t["rango_hasta"]) for t in tramos])
    assert rangos == [(1, 10), (11, 15), (16, 30)]
    pool_count = sum(1 for t in tramos if t["sucursal_id"] is None)
    assert pool_count == 2


def test_idempotencia_reescritura(mgr, session, sucursal_a):
    """Reescribir con la misma config dos veces no levanta error."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    config = [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 10},
    ]
    mgr.configurar_asignaciones(info["id"], config)
    session.commit()
    mgr.configurar_asignaciones(info["id"], config)
    session.commit()

    asignaciones = (
        session.query(CafAsignacion).filter_by(caf_id=info["id"]).all()
    )
    # 1-10 a sucursal A + 11-30 pool relleno → 2 tramos
    assert len(asignaciones) == 2


# ══════════════════════════════════════════════════════════════════
# Tests: configurar_asignaciones — validación de errores
# ══════════════════════════════════════════════════════════════════


def test_solape_entre_tramos_levanta_error(mgr, session, sucursal_a, sucursal_b):
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    with pytest.raises(ValueError, match="solapados"):
        mgr.configurar_asignaciones(info["id"], [
            {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 15},
            {"sucursal_id": sucursal_b.id, "rango_desde": 10, "rango_hasta": 20},
        ])


def test_tramo_fuera_del_rango_caf_levanta_error(mgr, session, sucursal_a):
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    with pytest.raises(ValueError, match="fuera del rango"):
        mgr.configurar_asignaciones(info["id"], [
            {"sucursal_id": sucursal_a.id, "rango_desde": 25, "rango_hasta": 50},
        ])


def test_rango_invertido_levanta_error(mgr, session, sucursal_a):
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    with pytest.raises(ValueError, match="rango_desde"):
        mgr.configurar_asignaciones(info["id"], [
            {"sucursal_id": sucursal_a.id, "rango_desde": 20, "rango_hasta": 10},
        ])


def test_sucursal_inexistente_levanta_error(mgr, session):
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    with pytest.raises(ValueError, match="no pertenece a esta empresa"):
        mgr.configurar_asignaciones(info["id"], [
            {"sucursal_id": "suc-zzz", "rango_desde": 1, "rango_hasta": 10},
        ])


def test_sucursal_inactiva_levanta_error(mgr, session, sucursal_inactiva):
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    with pytest.raises(ValueError, match="inactiva"):
        mgr.configurar_asignaciones(info["id"], [
            {
                "sucursal_id": sucursal_inactiva.id,
                "rango_desde": 1,
                "rango_hasta": 10,
            },
        ])


def test_caf_inexistente_levanta_error(mgr):
    with pytest.raises(ValueError, match="no encontrado"):
        mgr.configurar_asignaciones("caf-zzz", [])


# ══════════════════════════════════════════════════════════════════
# Tests: inmutabilidad de folios consumidos
# ══════════════════════════════════════════════════════════════════


def test_folio_consumido_no_puede_moverse_entre_sucursales(
    mgr, session, sucursal_a, sucursal_b,
):
    """Folio emitido por suc_a no puede reasignarse a suc_b."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    # Asignar 1-10 a suc_a y consumir 3 folios desde suc_a.
    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 10},
    ])
    session.commit()
    for _ in range(3):
        mgr.siguiente_folio(33, sucursal_id=sucursal_a.id)
    session.commit()

    # Intentar reasignar 1-10 a suc_b debe fallar (folios 1, 2 y 3 ya emitidos
    # por suc_a).
    with pytest.raises(ValueError, match="ya fue emitido"):
        mgr.configurar_asignaciones(info["id"], [
            {"sucursal_id": sucursal_b.id, "rango_desde": 1, "rango_hasta": 10},
        ])


def test_reduce_rango_dejando_folios_consumidos_en_misma_sucursal(
    mgr, session, sucursal_a,
):
    """suc_a 1-10 con 3 consumidos → reducir a suc_a 1-5 funciona, el resto cae al pool."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 10},
    ])
    session.commit()
    for _ in range(3):
        mgr.siguiente_folio(33, sucursal_id=sucursal_a.id)
    session.commit()

    # Reducir 1-10 → 1-5 en suc_a; 6-30 cae al pool.
    tramos = mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 5},
    ])
    session.commit()

    asig_a = next(t for t in tramos if t["sucursal_id"] == sucursal_a.id)
    # folio_actual debe quedar en 4 (los 3 consumidos: 1, 2, 3 → próximo 4)
    assert asig_a["folio_actual"] == 4
    assert asig_a["rango_desde"] == 1
    assert asig_a["rango_hasta"] == 5


def test_pool_consumido_puede_pasar_a_otro_pool_sin_restriccion(mgr, session):
    """Folios consumidos en pool no tienen dueño-sucursal, así que es ok subdividir."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    # Consumir 5 folios del pool.
    for _ in range(5):
        mgr.siguiente_folio(33)
    session.commit()

    # Reescribir como pool 1-15 + pool 16-30 (subdivisión sin sucursal):
    # debe permitir porque el dueño viejo y el nuevo es el mismo (server).
    tramos = mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": None, "rango_desde": 1, "rango_hasta": 15},
        {"sucursal_id": None, "rango_desde": 16, "rango_hasta": 30},
    ])
    session.commit()
    assert len(tramos) == 2


# ══════════════════════════════════════════════════════════════════
# Tests: siguiente_folio — aislamiento por slice
# ══════════════════════════════════════════════════════════════════


def test_siguiente_folio_pool_consume_solo_del_pool(
    mgr, session, sucursal_a,
):
    """Pool 1-10 + suc_a 11-30: siguiente_folio() trabaja sobre 1-10."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 11, "rango_hasta": 30},
    ])
    session.commit()

    folio, _caf = mgr.siguiente_folio(33)  # sin sucursal_id → pool
    assert folio == 1


def test_siguiente_folio_sucursal_consume_solo_del_slice(
    mgr, session, sucursal_a, sucursal_b,
):
    """Pool 1-5 + suc_a 6-10 + suc_b 11-15: cada slice consume independiente."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 6, "rango_hasta": 10},
        {"sucursal_id": sucursal_b.id, "rango_desde": 11, "rango_hasta": 15},
    ])
    session.commit()

    f_pool, _ = mgr.siguiente_folio(33)
    f_a, _ = mgr.siguiente_folio(33, sucursal_id=sucursal_a.id)
    f_b, _ = mgr.siguiente_folio(33, sucursal_id=sucursal_b.id)

    assert f_pool == 1
    assert f_a == 6
    assert f_b == 11


def test_siguiente_folio_sin_stock_levanta_folios_agotados_error(
    mgr, session, sucursal_a,
):
    """Sucursal con un solo folio disponible: el segundo intento levanta error."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 1},
    ])
    session.commit()

    folio, _ = mgr.siguiente_folio(33, sucursal_id=sucursal_a.id)
    assert folio == 1
    session.commit()

    with pytest.raises(FoliosAgotadosError) as exc_info:
        mgr.siguiente_folio(33, sucursal_id=sucursal_a.id)
    assert exc_info.value.tipo_dte == 33
    assert exc_info.value.sucursal_id == sucursal_a.id


def test_siguiente_folio_sin_fallback_automatico_a_sucursal(
    mgr, session, sucursal_a,
):
    """Pool sin stock + suc_a con stock: pool levanta error, no pasa al de A."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    # Todo a suc_a, pool vacío.
    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 30},
    ])
    session.commit()

    with pytest.raises(FoliosAgotadosError) as exc_info:
        mgr.siguiente_folio(33)  # pool, sin sucursal_id
    assert exc_info.value.sucursal_id is None


def test_siguiente_folio_salta_a_proximo_caf_dentro_del_mismo_slice(
    mgr, session,
):
    """Dos CAFs pool: cuando el primero se agota, salta al segundo."""
    info1 = mgr.registrar_caf(_caf_xml(33, 1, 2))
    session.commit()
    info2 = mgr.registrar_caf(_caf_xml(33, 100, 200))
    session.commit()

    f1, _ = mgr.siguiente_folio(33)
    f2, _ = mgr.siguiente_folio(33)
    f3, _ = mgr.siguiente_folio(33)
    assert f1 == 1
    assert f2 == 2
    assert f3 == 100  # CAF 1 agotado, salta al CAF 2


# ══════════════════════════════════════════════════════════════════
# Tests: devolver_folios_de_sucursal_al_pool
# ══════════════════════════════════════════════════════════════════


def test_devolver_folios_al_pool_mueve_tramos_y_emite_evento(
    mgr, session, sucursal_a,
):
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 10},
    ])
    session.commit()

    n = mgr.devolver_folios_de_sucursal_al_pool(sucursal_a.id)
    session.commit()

    assert n == 1  # 1 tramo movido al pool

    # Todos los tramos del CAF deben estar ahora en pool (sucursal_id = None)
    tramos = (
        session.query(CafAsignacion).filter_by(caf_id=info["id"]).all()
    )
    assert all(t.sucursal_id is None for t in tramos)

    # Debe haberse emitido un CafEventoSync por la sucursal afectada.
    eventos = session.query(CafEventoSync).filter_by(
        sucursal_id=sucursal_a.id,
    ).all()
    tipos = [e.tipo_evento for e in eventos]
    assert "asignacion_eliminada" in tipos


# ══════════════════════════════════════════════════════════════════
# Tests: set_folio respeta subdivisión
# ══════════════════════════════════════════════════════════════════


def test_set_folio_rechaza_caf_subdividido_en_sucursal(
    mgr, session, sucursal_a,
):
    """Mientras hay tramos en sucursal, set_folio se niega — exige reasignar primero."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 10},
    ])
    session.commit()

    with pytest.raises(ValueError, match="tramos asignados"):
        mgr.set_folio(33, 5)


def test_set_folio_permite_avance_sobre_caf_solo_pool(mgr, session):
    """CAF sin tramos en sucursal: set_folio funciona como antes."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    mgr.set_folio(33, 7)
    session.commit()

    a = session.query(CafAsignacion).filter_by(caf_id=info["id"]).one()
    assert a.folio_actual == 7


# ══════════════════════════════════════════════════════════════════
# Tests: configurar_asignaciones emite eventos sync
# ══════════════════════════════════════════════════════════════════


def test_configurar_asignaciones_emite_evento_para_sucursal_que_pierde(
    mgr, session, sucursal_a,
):
    """suc_a con tramo → quitarle el tramo emite evento de sync para suc_a."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 10},
    ])
    session.commit()

    # Limpiar eventos previos para aislar el test.
    session.query(CafEventoSync).delete()
    session.commit()

    # Reasignar todo al pool.
    mgr.configurar_asignaciones(info["id"], [])
    session.commit()

    eventos = session.query(CafEventoSync).filter_by(
        sucursal_id=sucursal_a.id,
    ).all()
    assert len(eventos) >= 1
    assert eventos[0].tipo_evento == "asignacion_modificada"
    assert eventos[0].caf_id == info["id"]


def test_configurar_asignaciones_emite_evento_para_sucursal_que_gana(
    mgr, session, sucursal_a,
):
    """Asignar un tramo a una sucursal nueva genera evento para esa sucursal."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    session.query(CafEventoSync).delete()
    session.commit()

    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 5, "rango_hasta": 10},
    ])
    session.commit()

    eventos = session.query(CafEventoSync).filter_by(
        sucursal_id=sucursal_a.id,
    ).all()
    assert len(eventos) == 1
    payload = eventos[0].payload
    assert payload["tipo_dte"] == 33
    assert any(
        t["rango_desde"] == 5 and t["rango_hasta"] == 10
        for t in payload["tramos"]
    )


# ══════════════════════════════════════════════════════════════════
# Tests: estado_folios incluye tramos
# ══════════════════════════════════════════════════════════════════


def test_estado_folios_incluye_tramos_y_subdividido(
    mgr, session, sucursal_a,
):
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    mgr.configurar_asignaciones(info["id"], [
        {"sucursal_id": sucursal_a.id, "rango_desde": 1, "rango_hasta": 10},
    ])
    session.commit()

    estado = mgr.estado_folios()
    assert len(estado) == 1
    tipo_33 = estado[0]
    assert tipo_33["tipo_dte"] == 33
    rango = tipo_33["rangos"][0]
    assert rango["subdividido"] is True
    assert len(rango["tramos"]) == 2  # suc_a + pool relleno
    nombres_tramos = [t["sucursal_id"] for t in rango["tramos"]]
    assert sucursal_a.id in nombres_tramos
    assert None in nombres_tramos


def test_estado_folios_caf_no_subdividido_marca_subdividido_false(
    mgr, session,
):
    info = mgr.registrar_caf(_caf_xml(33, 1, 30))
    session.commit()

    estado = mgr.estado_folios()
    rango = estado[0]["rangos"][0]
    assert rango["subdividido"] is False
    assert len(rango["tramos"]) == 1
    assert rango["tramos"][0]["sucursal_id"] is None
