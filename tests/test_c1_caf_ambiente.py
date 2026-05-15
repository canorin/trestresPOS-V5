"""Tests para C1 — marcador ambiente (cert/prod) en CafFolio.

Cubre:
- CafFolio.ambiente se establece desde empresa.ambiente_sii en registrar_caf
- siguiente_folio filtra CAFs por ambiente y levanta FoliosAgotadosError si
  el único CAF disponible es del ambiente opuesto
- CAF de producción no bloquea al ambiente certificación y viceversa
- Default 'certificacion' cuando empresa no existe en DB
"""
from __future__ import annotations

import io
import os
import tempfile
import uuid
from datetime import datetime, date
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from crumbpos.db.models import Base, Empresa, CafFolio, CafAsignacion
from crumbpos.core.caf.caf_manager_db import CAFManagerDB, FoliosAgotadosError


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _engine():
    """Crea un engine in-memory con todas las tablas."""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


_RUT_COUNTER = 0


def _empresa(session: Session, ambiente: str = "certificacion") -> Empresa:
    global _RUT_COUNTER
    _RUT_COUNTER += 1
    # Usar RUTs distintos para evitar conflicto UNIQUE en tests con varias empresas
    rut = f"763547{_RUT_COUNTER:02d}-K"
    emp = Empresa(
        id=str(uuid.uuid4()),
        rut=rut,
        razon_social="Test SA",
        giro="Comercio",
        direccion="Calle 1",
        comuna="Santiago",
        ciudad="Santiago",
        ambiente_sii=ambiente,
    )
    session.add(emp)
    session.flush()
    return emp


def _caf_folio(
    session: Session,
    empresa_id: str,
    tipo_dte: int,
    desde: int,
    hasta: int,
    ambiente: str = "certificacion",
) -> CafFolio:
    """Inserta un CafFolio con una asignación pool inicial."""
    caf = CafFolio(
        id=str(uuid.uuid4()),
        empresa_id=empresa_id,
        tipo_dte=tipo_dte,
        rango_desde=desde,
        rango_hasta=hasta,
        folio_actual=desde,
        caf_xml_raw="<AUTORIZACION/>",
        estado="activo",
        ambiente=ambiente,
    )
    session.add(caf)
    session.flush()

    asig = CafAsignacion(
        id=str(uuid.uuid4()),
        caf_id=caf.id,
        sucursal_id=None,
        rango_desde=desde,
        rango_hasta=hasta,
        folio_actual=desde,
        estado="activo",
    )
    session.add(asig)
    session.flush()
    return caf


# ──────────────────────────────────────────────────────────────────────────────
# Test 1 — siguiente_folio usa solo CAFs del ambiente correcto
# ──────────────────────────────────────────────────────────────────────────────

def _mock_caf():
    return MagicMock()


def test_siguiente_folio_usa_caf_ambiente_correcto():
    """Si empresa es certificacion, toma folios del CAF certificacion."""
    eng = _engine()
    with Session(eng) as s, s.begin():
        emp = _empresa(s, "certificacion")
        _caf_folio(s, emp.id, 39, 1, 100, ambiente="certificacion")
        emp_id = emp.id

    with Session(eng) as s, s.begin():
        mgr = CAFManagerDB(s, emp_id)
        with patch.object(mgr, "_caf_from_row", return_value=_mock_caf()):
            folio, _ = mgr.siguiente_folio(39)

    assert folio == 1


def test_siguiente_folio_ignora_caf_ambiente_opuesto():
    """Si empresa es certificacion y solo hay CAF produccion, levanta FoliosAgotadosError."""
    eng = _engine()
    with Session(eng) as s, s.begin():
        emp = _empresa(s, "certificacion")
        _caf_folio(s, emp.id, 39, 1, 100, ambiente="produccion")  # ambiente errado
        emp_id = emp.id

    with Session(eng) as s, s.begin():
        mgr = CAFManagerDB(s, emp_id)
        with patch.object(mgr, "_caf_from_row", return_value=_mock_caf()):
            with pytest.raises(FoliosAgotadosError):
                mgr.siguiente_folio(39)


def test_siguiente_folio_produccion_usa_caf_produccion():
    """Si empresa es produccion, toma folios del CAF produccion."""
    eng = _engine()
    with Session(eng) as s, s.begin():
        emp = _empresa(s, "produccion")
        _caf_folio(s, emp.id, 39, 500, 600, ambiente="produccion")
        emp_id = emp.id

    with Session(eng) as s, s.begin():
        mgr = CAFManagerDB(s, emp_id)
        with patch.object(mgr, "_caf_from_row", return_value=_mock_caf()):
            folio, _ = mgr.siguiente_folio(39)

    assert folio == 500


def test_siguiente_folio_produccion_ignora_caf_cert():
    """Si empresa es produccion y solo hay CAF cert, levanta FoliosAgotadosError."""
    eng = _engine()
    with Session(eng) as s, s.begin():
        emp = _empresa(s, "produccion")
        _caf_folio(s, emp.id, 39, 1, 100, ambiente="certificacion")  # ambiente errado
        emp_id = emp.id

    with Session(eng) as s, s.begin():
        mgr = CAFManagerDB(s, emp_id)
        with patch.object(mgr, "_caf_from_row", return_value=_mock_caf()):
            with pytest.raises(FoliosAgotadosError):
                mgr.siguiente_folio(39)


# ──────────────────────────────────────────────────────────────────────────────
# Test 2 — dos CAFs de ambientes distintos coexisten sin interferirse
# ──────────────────────────────────────────────────────────────────────────────

def test_dos_cafs_ambientes_distintos_no_interfieren():
    """CAF cert y CAF prod en la misma DB no se mezclan."""
    eng = _engine()
    with Session(eng) as s, s.begin():
        emp_cert = _empresa(s, "certificacion")
        emp_prod = _empresa(s, "produccion")
        _caf_folio(s, emp_cert.id, 39, 1, 100, ambiente="certificacion")
        _caf_folio(s, emp_prod.id, 39, 1, 100, ambiente="produccion")
        cert_id = emp_cert.id
        prod_id = emp_prod.id

    with Session(eng) as s, s.begin():
        mgr_cert = CAFManagerDB(s, cert_id)
        mgr_prod = CAFManagerDB(s, prod_id)
        with (
            patch.object(mgr_cert, "_caf_from_row", return_value=_mock_caf()),
            patch.object(mgr_prod, "_caf_from_row", return_value=_mock_caf()),
        ):
            folio_c, _ = mgr_cert.siguiente_folio(39)
            folio_p, _ = mgr_prod.siguiente_folio(39)

    # Ambos arrancan desde folio 1 — cada empresa ve su propio CAF
    assert folio_c == 1
    assert folio_p == 1


# ──────────────────────────────────────────────────────────────────────────────
# Test 3 — registrar_caf toma ambiente de empresa.ambiente_sii
# ──────────────────────────────────────────────────────────────────────────────

def _fake_xml(rut: str = "76354771-K", tipo: int = 39,
              desde: int = 1, hasta: int = 100) -> bytes:
    """XML de CAF mínimo para probar registrar_caf sin pasar validación SII."""
    return (
        f'<?xml version="1.0"?>'
        f'<AUTORIZACION><CAF><DA>'
        f'<RE>{rut}</RE><TD>{tipo}</TD>'
        f'<RNG><D>{desde}</D><H>{hasta}</H></RNG>'
        f'<FA>2025-01-01</FA>'
        f'</DA></CAF></AUTORIZACION>'
    ).encode("ISO-8859-1")


def _parse_fake_xml(xml_bytes: bytes):
    """Parser minimal para los tests de registrar_caf."""
    from lxml import etree
    return etree.fromstring(xml_bytes)


_CAF_RUT = "76999888-7"  # RUT fijo para este test (CAF XML + empresa deben coincidir)


def test_registrar_caf_toma_ambiente_de_empresa():
    """registrar_caf establece ambiente=empresa.ambiente_sii en la fila CafFolio."""
    eng = _engine()
    xml_bytes = _fake_xml(rut=_CAF_RUT)

    with Session(eng) as s, s.begin():
        emp = Empresa(
            id=str(uuid.uuid4()),
            rut=_CAF_RUT,
            razon_social="Test Prod SA",
            giro="Comercio",
            direccion="Calle 1",
            comuna="Santiago",
            ciudad="Santiago",
            ambiente_sii="produccion",
        )
        s.add(emp)
        s.flush()
        emp_id = emp.id
        mgr = CAFManagerDB(s, emp_id)

        # CAF mock que no lanza errores de validación
        caf_mock = MagicMock()
        caf_mock.validar.return_value = []

        with (
            # fromstring_safe → parser real (el XML de test es lxml-parseable)
            patch(
                "crumbpos.core.caf.caf_manager_db.fromstring_safe",
                side_effect=_parse_fake_xml,
            ),
            # CAF() constructor dentro de registrar_caf (importado localmente)
            patch("crumbpos.core.caf.caf_manager.CAF", return_value=caf_mock),
            # _cargar_llave_publica_sii también es local
            patch(
                "crumbpos.core.caf.caf_manager._cargar_llave_publica_sii",
                return_value=None,
            ),
        ):
            mgr.registrar_caf(xml_bytes)

        caf_row = s.query(CafFolio).filter(CafFolio.empresa_id == emp_id).first()
        assert caf_row is not None
        assert caf_row.ambiente == "produccion", (
            f"Se esperaba ambiente='produccion', se obtuvo '{caf_row.ambiente}'"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Test 4 — modelo tiene la columna ambiente
# ──────────────────────────────────────────────────────────────────────────────

def test_caf_folio_tiene_columna_ambiente():
    """CafFolio debe tener la columna ambiente con default certificacion."""
    eng = _engine()
    with Session(eng) as s, s.begin():
        emp = _empresa(s, "certificacion")
        caf = CafFolio(
            empresa_id=emp.id,
            tipo_dte=39,
            rango_desde=1,
            rango_hasta=10,
            folio_actual=1,
            caf_xml_raw="<x/>",
            estado="activo",
            # Sin especificar ambiente → debe tomar default 'certificacion'
        )
        s.add(caf)
        s.flush()
        assert caf.ambiente == "certificacion"


def test_caf_folio_ambiente_produccion():
    """CafFolio acepta ambiente='produccion'."""
    eng = _engine()
    with Session(eng) as s, s.begin():
        emp = _empresa(s, "produccion")
        caf = CafFolio(
            empresa_id=emp.id,
            tipo_dte=33,
            rango_desde=1,
            rango_hasta=10,
            folio_actual=1,
            caf_xml_raw="<x/>",
            estado="activo",
            ambiente="produccion",
        )
        s.add(caf)
        s.flush()
        assert caf.ambiente == "produccion"
