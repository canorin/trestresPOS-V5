"""Tests para A6 — RCOF backfill + reintento intra-día.

Cubre:
- _empresa_necesita_reintento_rcof: detección de casos pendientes
- reintentar_rcof_fallidos: itera empresas y delega correctamente
- ejecutar_rcof_backfill: revisa N días hacia atrás
- _loop_reintentos_rcof_intraday: termina sin colgar ante distintas condiciones
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from unittest.mock import MagicMock, call, patch

import pytest

from crumbpos.api.scheduler import (
    _empresa_necesita_reintento_rcof,
    ejecutar_rcof_backfill,
    reintentar_rcof_fallidos,
    _loop_reintentos_rcof_intraday,
)
from crumbpos.db.multi_tenant import EmpresaRegistro


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _reg(**kwargs) -> EmpresaRegistro:
    defaults = dict(
        rut="76354771-K",
        razon_social="Test SA",
        ambiente_activo="certificacion",
        activa=True,
    )
    defaults.update(kwargs)
    return EmpresaRegistro(**defaults)


FECHA = date(2026, 5, 10)


def _db_mock_boletas_y_rcof(n_boletas: int, estado_rcof: str | None):
    """Construye un db mock que devuelve n_boletas boletas y un RcofDiario con
    *estado_rcof* (o None si estado_rcof es None, simulando registro ausente)."""
    db = MagicMock()
    call_count = [0]

    def query_side(model):
        call_count[0] += 1
        q = MagicMock()
        if call_count[0] == 1:
            # Primera llamada → DteEmitido
            q.filter.return_value.limit.return_value.count.return_value = n_boletas
        else:
            # Segunda llamada → RcofDiario
            if estado_rcof is None:
                q.filter.return_value.first.return_value = None
            else:
                rcof = MagicMock()
                rcof.estado_sii = estado_rcof
                q.filter.return_value.first.return_value = rcof
        return q

    db.query.side_effect = query_side
    return db


# ──────────────────────────────────────────────────────────────────────────────
# _empresa_necesita_reintento_rcof
# ──────────────────────────────────────────────────────────────────────────────

def test_sin_boletas_no_necesita_reintento():
    """Sin boletas del día → False sin consultar RcofDiario."""
    db = _db_mock_boletas_y_rcof(n_boletas=0, estado_rcof=None)
    with patch("crumbpos.api.scheduler.get_empresa_db_session", return_value=db):
        assert _empresa_necesita_reintento_rcof(_reg(), FECHA) is False
    # No debe haber consultado RcofDiario
    assert db.query.call_count == 1


def test_boletas_con_rcof_enviado_no_necesita_reintento():
    """Boletas + RcofDiario estado='enviado' → False."""
    db = _db_mock_boletas_y_rcof(n_boletas=3, estado_rcof="enviado")
    with patch("crumbpos.api.scheduler.get_empresa_db_session", return_value=db):
        assert _empresa_necesita_reintento_rcof(_reg(), FECHA) is False


def test_boletas_con_rcof_error_necesita_reintento():
    """Boletas + RcofDiario estado='error_envio' → True (sin registro enviado)."""
    # El query filtra por estado='enviado', así que si el registro tiene
    # error_envio, la query devuelve None → necesita reintento.
    db = _db_mock_boletas_y_rcof(n_boletas=2, estado_rcof=None)
    with patch("crumbpos.api.scheduler.get_empresa_db_session", return_value=db):
        assert _empresa_necesita_reintento_rcof(_reg(), FECHA) is True


def test_boletas_sin_ningún_rcof_necesita_reintento():
    """Boletas pero nunca se guardó RcofDiario (crash) → True."""
    db = _db_mock_boletas_y_rcof(n_boletas=5, estado_rcof=None)
    with patch("crumbpos.api.scheduler.get_empresa_db_session", return_value=db):
        assert _empresa_necesita_reintento_rcof(_reg(), FECHA) is True


def test_excepcion_en_db_devuelve_false():
    """Si la BD lanza excepción, devuelve False (fail-safe, no reintentar)."""
    db = MagicMock()
    db.query.side_effect = Exception("DB bloqueada")
    with patch("crumbpos.api.scheduler.get_empresa_db_session", return_value=db):
        assert _empresa_necesita_reintento_rcof(_reg(), FECHA) is False


# ──────────────────────────────────────────────────────────────────────────────
# reintentar_rcof_fallidos
# ──────────────────────────────────────────────────────────────────────────────

def test_reintentar_omite_empresas_inactivas():
    """Empresas con activa=False se ignoran, no se llama a _generar_rcof_empresa."""
    reg = _reg(activa=False)
    with (
        patch("crumbpos.api.scheduler.listar_empresas", return_value=[reg]),
        patch("crumbpos.api.scheduler._empresa_necesita_reintento_rcof") as check,
        patch("crumbpos.api.scheduler._generar_rcof_empresa") as gen,
    ):
        res = reintentar_rcof_fallidos(FECHA)
    assert res == []
    check.assert_not_called()
    gen.assert_not_called()


def test_reintentar_omite_empresas_sin_pendiente():
    """Si _empresa_necesita_reintento_rcof devuelve False, no se reintenta."""
    regs = [_reg(rut="11111111-1"), _reg(rut="22222222-2")]
    with (
        patch("crumbpos.api.scheduler.listar_empresas", return_value=regs),
        patch("crumbpos.api.scheduler._empresa_necesita_reintento_rcof", return_value=False),
        patch("crumbpos.api.scheduler._generar_rcof_empresa") as gen,
    ):
        res = reintentar_rcof_fallidos(FECHA)
    assert res == []
    gen.assert_not_called()


def test_reintentar_llama_generar_para_empresa_con_error():
    """Empresas con pendiente=True generan y devuelven resultado con es_reintento."""
    reg1 = _reg(rut="11111111-1")
    reg2 = _reg(rut="22222222-2")

    def necesita(reg, fecha):
        return reg.rut == "11111111-1"

    with (
        patch("crumbpos.api.scheduler.listar_empresas", return_value=[reg1, reg2]),
        patch("crumbpos.api.scheduler._empresa_necesita_reintento_rcof", side_effect=necesita),
        patch(
            "crumbpos.api.scheduler._generar_rcof_empresa",
            return_value={"ok": True, "track_id": "8888", "estado_sii": "enviado"},
        ) as gen,
    ):
        res = reintentar_rcof_fallidos(FECHA)

    assert len(res) == 1
    assert res[0]["empresa_rut"] == "11111111-1"
    assert res[0]["es_reintento"] is True
    assert res[0]["ok"] is True
    gen.assert_called_once_with(reg1, FECHA)


def test_reintentar_marca_error_si_generar_falla():
    """Si _generar_rcof_empresa devuelve ok=False, se incluye en resultados como error."""
    reg = _reg()
    with (
        patch("crumbpos.api.scheduler.listar_empresas", return_value=[reg]),
        patch("crumbpos.api.scheduler._empresa_necesita_reintento_rcof", return_value=True),
        patch(
            "crumbpos.api.scheduler._generar_rcof_empresa",
            return_value={"ok": False, "error": "SII caído"},
        ),
    ):
        res = reintentar_rcof_fallidos(FECHA)

    assert len(res) == 1
    assert res[0]["ok"] is False
    assert res[0]["es_reintento"] is True


# ──────────────────────────────────────────────────────────────────────────────
# ejecutar_rcof_backfill
# ──────────────────────────────────────────────────────────────────────────────

def test_backfill_revisa_exactamente_n_dias():
    """Llama a reintentar_rcof_fallidos una vez por cada día, desde ayer hacia atrás."""
    hoy = date(2026, 5, 15)
    fechas_llamadas: list[date] = []

    def reint_mock(fecha):
        fechas_llamadas.append(fecha)
        return []

    with (
        patch("crumbpos.api.scheduler.reintentar_rcof_fallidos", side_effect=reint_mock),
        patch(
            "crumbpos.api.scheduler.datetime",
            **{"now.return_value.date.return_value": hoy},
        ),
    ):
        ejecutar_rcof_backfill(dias=5)

    assert len(fechas_llamadas) == 5
    esperadas = [hoy - timedelta(days=i) for i in range(1, 6)]
    assert fechas_llamadas == esperadas


def test_backfill_no_incluye_dia_actual():
    """El día actual NO se incluye en el backfill (responsabilidad del loop diario)."""
    hoy = date(2026, 5, 15)
    fechas_llamadas: list[date] = []

    with (
        patch("crumbpos.api.scheduler.reintentar_rcof_fallidos", side_effect=lambda f: fechas_llamadas.append(f) or []),
        patch("crumbpos.api.scheduler.datetime", **{"now.return_value.date.return_value": hoy}),
    ):
        ejecutar_rcof_backfill(dias=3)

    assert hoy not in fechas_llamadas


def test_backfill_agrega_clave_backfill_fecha():
    """Cada resultado del backfill lleva backfill_fecha con la fecha del reintento."""
    hoy = date(2026, 5, 15)
    ayer = date(2026, 5, 14)

    def reint_mock(fecha):
        if fecha == ayer:
            return [{"ok": True, "empresa_rut": "11111111-1"}]
        return []

    with (
        patch("crumbpos.api.scheduler.reintentar_rcof_fallidos", side_effect=reint_mock),
        patch("crumbpos.api.scheduler.datetime", **{"now.return_value.date.return_value": hoy}),
    ):
        res = ejecutar_rcof_backfill(dias=3)

    assert len(res) == 1
    assert res[0]["backfill_fecha"] == "2026-05-14"


def test_backfill_devuelve_lista_vacia_si_sin_pendientes():
    """Si no hay nada pendiente, devuelve lista vacía sin lanzar excepciones."""
    hoy = date(2026, 5, 15)
    with (
        patch("crumbpos.api.scheduler.reintentar_rcof_fallidos", return_value=[]),
        patch("crumbpos.api.scheduler.datetime", **{"now.return_value.date.return_value": hoy}),
    ):
        res = ejecutar_rcof_backfill(dias=7)
    assert res == []


# ──────────────────────────────────────────────────────────────────────────────
# _loop_reintentos_rcof_intraday (async, via asyncio.run)
# ──────────────────────────────────────────────────────────────────────────────

def test_loop_intraday_termina_si_hora_pasada_limite():
    """Si la hora actual ya pasó 23:55, el loop no llama sleep ni reintentar."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("America/Santiago")
    # Simular que son las 23:58 → ya pasó el límite de 23:55
    ahora_tarde = datetime(2026, 5, 10, 23, 58, 0, tzinfo=TZ)

    with (
        patch("crumbpos.api.scheduler.datetime", **{"now.return_value": ahora_tarde}),
        patch("crumbpos.api.scheduler.reintentar_rcof_fallidos") as reint,
    ):
        asyncio.run(_loop_reintentos_rcof_intraday(FECHA))

    reint.assert_not_called()


def test_loop_intraday_termina_si_no_hay_fallidos():
    """Con reintentar devolviendo lista vacía, el loop termina tras el primer sleep."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("America/Santiago")
    # 22:31 → dentro del rango
    ahora = datetime(2026, 5, 10, 22, 31, 0, tzinfo=TZ)
    limite = datetime(2026, 5, 10, 23, 55, 0, tzinfo=TZ)

    llamadas_sleep = []

    async def fake_sleep(segundos):
        llamadas_sleep.append(segundos)
        # No dormir de verdad en el test

    with (
        patch("crumbpos.api.scheduler.datetime", **{"now.return_value": ahora}),
        patch("asyncio.sleep", side_effect=fake_sleep),
        patch("crumbpos.api.scheduler.reintentar_rcof_fallidos", return_value=[]),
    ):
        asyncio.run(_loop_reintentos_rcof_intraday(FECHA))

    # Durmió al menos una vez (el sleep de espera antes del primer reintento)
    assert len(llamadas_sleep) >= 1
    # Después del primer reintento sin fallidos, no debe iterar más (una sola llamada sleep)
    assert len(llamadas_sleep) == 1


def test_loop_intraday_termina_si_todos_resueltos():
    """Si todos los reintentos resultan ok=True, el loop no sigue iterando."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("America/Santiago")
    ahora = datetime(2026, 5, 10, 22, 40, 0, tzinfo=TZ)

    async def fake_sleep(_):
        pass  # inmediato

    with (
        patch("crumbpos.api.scheduler.datetime", **{"now.return_value": ahora}),
        patch("asyncio.sleep", side_effect=fake_sleep),
        patch(
            "crumbpos.api.scheduler.reintentar_rcof_fallidos",
            return_value=[{"ok": True, "empresa_rut": "11111111-1"}],
        ) as reint,
    ):
        asyncio.run(_loop_reintentos_rcof_intraday(FECHA))

    # Con 0 errores, el loop sale en el primer ciclo
    reint.assert_called_once()
