"""Tests para A7 — IECV catch-up al boot.

Cubre:
- get_scheduler_estado / set_scheduler_estado: lectura/escritura en master.db
- recordar_iecv_mensual persiste el estado tras ejecutarse
- _iecv_catch_up_al_boot: dispara solo cuando corresponde
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from datetime import datetime as real_datetime

from crumbpos.api.scheduler import (
    IECV_HORA,
    IECV_MINUTO,
    _iecv_catch_up_al_boot,
    _mes_anterior,
    recordar_iecv_mensual,
)

TZ = ZoneInfo("America/Santiago")


# ──────────────────────────────────────────────────────────────────────────────
# get_scheduler_estado / set_scheduler_estado (in-memory SQLite)
# ──────────────────────────────────────────────────────────────────────────────

def _make_in_memory_master():
    """Crea un master.db en memoria y devuelve (engine, SessionFactory)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from crumbpos.db.multi_tenant import BaseMaster

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    BaseMaster.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return engine, factory


def test_get_scheduler_estado_devuelve_none_si_no_existe():
    """Clave inexistente → None."""
    _, factory = _make_in_memory_master()
    with (
        patch("crumbpos.db.multi_tenant._ensure_master"),
        patch("crumbpos.db.multi_tenant._MasterSessionFactory", factory),
    ):
        from crumbpos.db.multi_tenant import get_scheduler_estado
        assert get_scheduler_estado("clave_inexistente") is None


def test_set_y_get_scheduler_estado_round_trip():
    """set seguido de get devuelve el mismo valor."""
    _, factory = _make_in_memory_master()
    with (
        patch("crumbpos.db.multi_tenant._ensure_master"),
        patch("crumbpos.db.multi_tenant._MasterSessionFactory", factory),
    ):
        from crumbpos.db.multi_tenant import get_scheduler_estado, set_scheduler_estado
        set_scheduler_estado("iecv_ultimo_periodo_recordado", "2026-04")
        assert get_scheduler_estado("iecv_ultimo_periodo_recordado") == "2026-04"


def test_set_scheduler_estado_upsert():
    """set actualiza el valor si la clave ya existe."""
    _, factory = _make_in_memory_master()
    with (
        patch("crumbpos.db.multi_tenant._ensure_master"),
        patch("crumbpos.db.multi_tenant._MasterSessionFactory", factory),
    ):
        from crumbpos.db.multi_tenant import get_scheduler_estado, set_scheduler_estado
        set_scheduler_estado("mi_clave", "v1")
        set_scheduler_estado("mi_clave", "v2")
        assert get_scheduler_estado("mi_clave") == "v2"


# ──────────────────────────────────────────────────────────────────────────────
# recordar_iecv_mensual guarda el estado
# ──────────────────────────────────────────────────────────────────────────────

def test_recordar_iecv_mensual_persiste_periodo():
    """Tras ejecutarse, el estado 'iecv_ultimo_periodo_recordado' refleja el período."""
    ahora = datetime(2026, 5, 3, 10, 0, tzinfo=TZ)  # 3 de mayo → periodo = 2026-04

    guardado = {}

    def fake_set(clave, valor):
        guardado[clave] = valor

    with (
        patch("crumbpos.api.scheduler.datetime", **{"now.return_value": ahora}),
        patch("crumbpos.api.scheduler.listar_empresas", return_value=[]),
        patch("crumbpos.api.scheduler.set_scheduler_estado", side_effect=fake_set),
    ):
        recordar_iecv_mensual()

    assert guardado.get("iecv_ultimo_periodo_recordado") == "2026-04"


def test_recordar_iecv_mensual_persiste_aunque_no_hay_empresas():
    """Incluso sin empresas, el estado se guarda (para que el catch-up no refuerce)."""
    ahora = datetime(2026, 5, 5, 9, 30, tzinfo=TZ)
    guardado = {}

    with (
        patch("crumbpos.api.scheduler.datetime", **{"now.return_value": ahora}),
        patch("crumbpos.api.scheduler.listar_empresas", return_value=[]),
        patch("crumbpos.api.scheduler.set_scheduler_estado", side_effect=lambda k, v: guardado.update({k: v})),
    ):
        recordar_iecv_mensual()

    assert guardado["iecv_ultimo_periodo_recordado"] == "2026-04"


# ──────────────────────────────────────────────────────────────────────────────
# _iecv_catch_up_al_boot
# ──────────────────────────────────────────────────────────────────────────────

def _run(coro):
    """Ejecuta una coroutine sin pytest-asyncio."""
    return asyncio.run(coro)


def _mock_dt(ahora: datetime):
    """Crea un mock de 'datetime' que:
    - Devuelve *ahora* en datetime.now(...)
    - Sigue usando el constructor real para datetime(year, month, ...)

    Esto evita que datetime(...) devuelva un MagicMock no-comparable.
    """
    mock = MagicMock(spec=real_datetime)
    mock.now.return_value = ahora
    # Al usar datetime(...) como constructor → delegar al real
    mock.side_effect = real_datetime
    return mock


def test_catch_up_no_dispara_antes_del_candidato():
    """Si la hora actual es antes del candidato (día 1 09:00), no llama recordar."""
    # 1 de mayo a las 08:30 → antes del candidato (09:00)
    ahora = real_datetime(2026, 5, 1, 8, 30, tzinfo=TZ)

    async def fake_sleep(_):
        pass

    with (
        patch("crumbpos.api.scheduler.datetime", _mock_dt(ahora)),
        patch("asyncio.sleep", side_effect=fake_sleep),
        patch("crumbpos.api.scheduler.recordar_iecv_mensual") as recordar_mock,
    ):
        _run(_iecv_catch_up_al_boot())

    recordar_mock.assert_not_called()


def test_catch_up_no_dispara_si_periodo_ya_recordado():
    """Si el estado ya tiene el período actual, no vuelve a disparar."""
    # 3 de mayo a las 10:00 → ya pasó candidato. Período pendiente: "2026-04"
    ahora = real_datetime(2026, 5, 3, 10, 0, tzinfo=TZ)

    async def fake_sleep(_):
        pass

    with (
        patch("crumbpos.api.scheduler.datetime", _mock_dt(ahora)),
        patch("asyncio.sleep", side_effect=fake_sleep),
        patch("crumbpos.api.scheduler.get_scheduler_estado", return_value="2026-04"),
        patch("crumbpos.api.scheduler.recordar_iecv_mensual") as recordar_mock,
    ):
        _run(_iecv_catch_up_al_boot())

    recordar_mock.assert_not_called()


def test_catch_up_dispara_si_servidor_perdio_el_dia_1():
    """Server reiniciado el día 5 sin que se haya recordado el período."""
    # 5 de mayo a las 14:00 → ya pasó candidato. Último recordado: "2026-03" (viejo)
    ahora = real_datetime(2026, 5, 5, 14, 0, tzinfo=TZ)

    async def fake_sleep(_):
        pass

    with (
        patch("crumbpos.api.scheduler.datetime", _mock_dt(ahora)),
        patch("asyncio.sleep", side_effect=fake_sleep),
        patch("crumbpos.api.scheduler.get_scheduler_estado", return_value="2026-03"),
        patch("crumbpos.api.scheduler.recordar_iecv_mensual") as recordar_mock,
    ):
        _run(_iecv_catch_up_al_boot())

    recordar_mock.assert_called_once()


def test_catch_up_dispara_si_nunca_hubo_estado():
    """Primera ejecución ever (estado=None): dispara si pasó el candidato."""
    ahora = real_datetime(2026, 5, 10, 9, 30, tzinfo=TZ)  # día 10

    async def fake_sleep(_):
        pass

    with (
        patch("crumbpos.api.scheduler.datetime", _mock_dt(ahora)),
        patch("asyncio.sleep", side_effect=fake_sleep),
        patch("crumbpos.api.scheduler.get_scheduler_estado", return_value=None),
        patch("crumbpos.api.scheduler.recordar_iecv_mensual") as recordar_mock,
    ):
        _run(_iecv_catch_up_al_boot())

    recordar_mock.assert_called_once()


def test_catch_up_exactamente_en_el_candidato_dispara():
    """Exactamente en la frontera del candidato (día 1 09:00): dispara (idempotente)."""
    ahora = real_datetime(2026, 5, 1, IECV_HORA, IECV_MINUTO, tzinfo=TZ)

    async def fake_sleep(_):
        pass

    with (
        patch("crumbpos.api.scheduler.datetime", _mock_dt(ahora)),
        patch("asyncio.sleep", side_effect=fake_sleep),
        patch("crumbpos.api.scheduler.get_scheduler_estado", return_value=None),
        patch("crumbpos.api.scheduler.recordar_iecv_mensual") as recordar_mock,
    ):
        _run(_iecv_catch_up_al_boot())

    # En la frontera (ahora >= candidato) dispara; aceptable pues es idempotente
    recordar_mock.assert_called_once()
