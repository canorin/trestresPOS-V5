"""Tests para ``CAFManagerDB.registrar_caf(folio_inicial_override=...)``.

El override permite subir un CAF con algunos folios ya consumidos fuera
del sistema (por ejemplo, folios quemados en intentos previos de
certificación o folios usados en otro software antes de migrar). Debe
cumplirse que:

- Sin override, el CAF arranca consumiendo desde ``folio_desde`` (el ``D``
  del XML) — comportamiento original, no debe cambiar.
- Con override válido dentro del rango ``[D, H]``, el ``folio_actual``
  queda seteado al override y ``siguiente_folio()`` retorna ese valor.
- Override menor a ``D`` o mayor a ``H`` levanta ``ValueError`` con glosa
  clara que cita el rango real del CAF (lo que el frontend muestra al
  usuario).
- Override no-entero también levanta ``ValueError``.

Los tests no validan la firma FRMA (no se configura ``SII_PUBLIC_KEY_PATH``),
pero sí ejercitan la vigencia: el CAF sintético tiene FA = hoy para pasar
los 730 / 365 días del check.

Los tests corren sobre SQLite in-memory con ``Base.metadata.create_all``
igual que ``test_envio_sobre_cert.py``.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crumbpos.core.caf.caf_manager_db import CAFManagerDB
from crumbpos.db.models import Base, CafFolio, Empresa


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
    """Construye un CAF XML mínimo pero completo para tests.

    Incluye todos los elementos que ``CAF._parse`` espera encontrar
    (RE, RS, TD, RNG, FA, RSAPK, IDK, FRMA, RSASK root, RSAPUBK root).
    La firma FRMA es basura — no se valida porque los tests no configuran
    ``SII_PUBLIC_KEY_PATH``. La fecha ``FA`` por defecto es hoy para que
    pase el check de vigencia de 730 días.
    """
    fa = fecha_autorizacion or date.today().isoformat()
    # PEMs sintéticos — solo deben existir como texto, no se cargan como llaves
    # porque ``registrar_caf`` no invoca ``get_private_key()``.
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
        id="emp-override",
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
def mgr(session, empresa):
    return CAFManagerDB(session, empresa.id)


# ══════════════════════════════════════════════════════════════════
# Tests: comportamiento sin override (regresión)
# ══════════════════════════════════════════════════════════════════


def test_sin_override_folio_actual_igual_a_folio_desde(mgr, session):
    """Regresión: sin override, el CAF arranca en folio_desde (comportamiento previo)."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 50))
    session.commit()

    assert info["folio_desde"] == 1
    assert info["folio_hasta"] == 50
    assert info["folio_inicial"] == 1

    row = session.query(CafFolio).filter_by(tipo_dte=33).one()
    assert row.folio_actual == 1
    assert row.rango_desde == 1
    assert row.rango_hasta == 50
    assert row.estado == "activo"


def test_sin_override_siguiente_folio_arranca_en_desde(mgr, session):
    """siguiente_folio() retorna folio_desde y avanza a folio_desde+1."""
    mgr.registrar_caf(_caf_xml(33, 10, 20))
    session.commit()

    folio, _caf = mgr.siguiente_folio(33)
    assert folio == 10

    folio2, _ = mgr.siguiente_folio(33)
    assert folio2 == 11


# ══════════════════════════════════════════════════════════════════
# Tests: override válido
# ══════════════════════════════════════════════════════════════════


def test_override_valido_dentro_del_rango(mgr, session):
    """Override=17 en rango 1-50 → folio_actual = 17."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 50), folio_inicial_override=17)
    session.commit()

    assert info["folio_inicial"] == 17
    assert info["folio_desde"] == 1  # no mentimos sobre el rango real del CAF
    assert info["folio_hasta"] == 50

    row = session.query(CafFolio).filter_by(tipo_dte=33).one()
    assert row.folio_actual == 17
    assert row.rango_desde == 1
    assert row.rango_hasta == 50


def test_override_igual_a_folio_desde_es_equivalente_a_no_pasarlo(mgr, session):
    """Override == folio_desde debe funcionar (edge case, no levantar error)."""
    info = mgr.registrar_caf(_caf_xml(33, 10, 20), folio_inicial_override=10)
    session.commit()

    assert info["folio_inicial"] == 10
    row = session.query(CafFolio).filter_by(tipo_dte=33).one()
    assert row.folio_actual == 10


def test_override_igual_a_folio_hasta_queda_con_un_solo_folio(mgr, session):
    """Override == folio_hasta es válido: queda 1 folio disponible."""
    info = mgr.registrar_caf(_caf_xml(33, 1, 50), folio_inicial_override=50)
    session.commit()

    assert info["folio_inicial"] == 50
    row = session.query(CafFolio).filter_by(tipo_dte=33).one()
    assert row.folio_actual == 50
    assert row.estado == "activo"


def test_override_afecta_siguiente_folio(mgr, session):
    """Con override=17, siguiente_folio() retorna 17 (no folio_desde=1)."""
    mgr.registrar_caf(_caf_xml(33, 1, 50), folio_inicial_override=17)
    session.commit()

    folio, _caf = mgr.siguiente_folio(33)
    assert folio == 17

    folio2, _ = mgr.siguiente_folio(33)
    assert folio2 == 18


def test_override_permite_agotar_el_caf_rapido(mgr, session):
    """Override=folio_hasta, emitir una vez, el CAF queda agotado."""
    mgr.registrar_caf(_caf_xml(33, 1, 50), folio_inicial_override=50)
    session.commit()

    folio, _ = mgr.siguiente_folio(33)
    assert folio == 50

    # El siguiente intento debe fallar porque el CAF se agotó
    with pytest.raises(ValueError, match="No hay folios disponibles"):
        mgr.siguiente_folio(33)

    row = session.query(CafFolio).filter_by(tipo_dte=33).one()
    assert row.estado == "agotado"


# ══════════════════════════════════════════════════════════════════
# Tests: override inválido
# ══════════════════════════════════════════════════════════════════


def test_override_menor_a_folio_desde_rechaza(mgr):
    """Override=0 con rango 1-50 → ValueError citando el rango real."""
    with pytest.raises(ValueError, match=r"fuera del rango del CAF \(1 a 50\)"):
        mgr.registrar_caf(_caf_xml(33, 1, 50), folio_inicial_override=0)


def test_override_mayor_a_folio_hasta_rechaza(mgr):
    """Override=51 con rango 1-50 → ValueError citando el rango real."""
    with pytest.raises(ValueError, match=r"fuera del rango del CAF \(1 a 50\)"):
        mgr.registrar_caf(_caf_xml(33, 1, 50), folio_inicial_override=51)


def test_override_negativo_rechaza(mgr):
    """Override negativo → ValueError."""
    with pytest.raises(ValueError, match=r"fuera del rango"):
        mgr.registrar_caf(_caf_xml(33, 1, 50), folio_inicial_override=-5)


def test_override_no_entero_rechaza(mgr):
    """Override no-entero (float, str) → ValueError explícito."""
    with pytest.raises(ValueError, match="número entero"):
        mgr.registrar_caf(_caf_xml(33, 1, 50), folio_inicial_override=3.5)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="número entero"):
        mgr.registrar_caf(_caf_xml(33, 1, 50), folio_inicial_override="17")  # type: ignore[arg-type]


def test_override_fuera_de_rango_no_persiste_nada(mgr, session):
    """Si el override es inválido, no debe quedar basura en la DB."""
    with pytest.raises(ValueError):
        mgr.registrar_caf(_caf_xml(33, 1, 50), folio_inicial_override=100)

    # Ningún CafFolio registrado
    count = session.query(CafFolio).count()
    assert count == 0


# ══════════════════════════════════════════════════════════════════
# Tests: override no interfiere con validaciones previas
# ══════════════════════════════════════════════════════════════════


def test_override_con_rut_no_coincidente_aun_rechaza(mgr):
    """G5 (RUT del CAF debe coincidir con empresa) se chequea aunque venga override."""
    # CAF con otro RUT, override válido para el rango — debe rechazarse por G5,
    # no llegar a la validación de override.
    xml = _caf_xml(33, 1, 50, rut="11111111-1")
    with pytest.raises(ValueError, match="RUT"):
        mgr.registrar_caf(xml, folio_inicial_override=17)


def test_override_en_segundo_caf_funciona(mgr, session):
    """Subir dos CAFs del mismo tipo, cada uno con su override, sin colisionar."""
    # Primer CAF: rango 1-50, sin override → arranca en 1
    mgr.registrar_caf(_caf_xml(33, 1, 50))
    session.commit()

    # Segundo CAF: rango 51-100, override=75
    mgr.registrar_caf(_caf_xml(33, 51, 100), folio_inicial_override=75)
    session.commit()

    rows = (
        session.query(CafFolio)
        .filter_by(tipo_dte=33)
        .order_by(CafFolio.rango_desde)
        .all()
    )
    assert len(rows) == 2
    assert rows[0].rango_desde == 1 and rows[0].folio_actual == 1
    assert rows[1].rango_desde == 51 and rows[1].folio_actual == 75
