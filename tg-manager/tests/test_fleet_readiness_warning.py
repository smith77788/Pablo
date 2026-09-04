"""Предупреждение о непрогретых аккаунтах перед массовой операцией.

Два разрыва:
  • жёсткий гейт (is_ready_for_op) смотрит на давление инфраструктуры и наличие
    свободных аккаунтов. У свежеимпортированного флота давление НУЛЕВОЕ (ничего
    ещё не происходило) и аккаунты свободны — гейт пропускает, и массовая
    операция сжигает всю партию разом. Это самая дорогая ошибка в продукте:
    аккаунты стоят денег и восстановлению не подлежат. account_readiness при
    этом существовал, но его is_ready_for_action не вызывался НИГДЕ;
  • мягкое предупреждение о давлении показывалось ТОЛЬКО в боте — мини-апп,
    основной интерфейс, запускал массовые операции вслепую.
"""
from __future__ import annotations

import asyncio
import pathlib

from services import infra_orchestrator as IO

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


class _Pool:
    def __init__(self, available, fresh, boom=False):
        self._row = {"available": available, "fresh": fresh}
        self._boom = boom

    async def fetchrow(self, q, *a):
        if self._boom:
            raise RuntimeError("база недоступна")
        return self._row


def _warn(available, fresh, boom=False):
    return asyncio.run(IO.get_readiness_warning(_Pool(available, fresh, boom), 1))


def test_warns_when_most_of_the_fleet_is_unwarmed():
    w = _warn(available=20, fresh=20)
    assert w and "20" in w


def test_warns_on_a_meaningful_share():
    assert _warn(available=10, fresh=4) is not None


def test_silent_for_a_single_fresh_account_in_a_large_fleet():
    """Один свежий аккаунт среди полусотни обкатанных — не повод для тревоги."""
    assert _warn(available=50, fresh=1) is None


def test_silent_when_nothing_is_fresh():
    assert _warn(available=20, fresh=0) is None


def test_silent_when_there_are_no_accounts():
    assert _warn(available=0, fresh=0) is None


def test_absolute_threshold_triggers_even_in_a_big_fleet():
    """Три и больше непрогретых — уже стоит сказать."""
    assert _warn(available=100, fresh=3) is not None


def test_fails_open_on_db_error():
    """Сбой расчёта предупреждения не должен мешать работать."""
    assert _warn(available=1, fresh=1, boom=True) is None


def test_warning_explains_the_consequence_and_the_way_out():
    w = _warn(available=10, fresh=10)
    assert "блокировкой" in w and "прогрейте" in w.lower()


def test_it_is_a_warning_not_a_block():
    """Часть пользователей заводит уже отлежавшиеся купленные аккаунты —
    запрет сломал бы им работу. Жёсткий гейт трогать нельзя."""
    src = (_ROOT / "services" / "infra_orchestrator.py").read_text(encoding="utf-8")
    start = src.index("async def is_ready_for_op")
    body = src[start:src.index("async def get_readiness_warning")] if \
        src.index("async def get_readiness_warning") > start else src[start:start + 2000]
    assert "get_readiness_warning" not in body
    assert "last_used" not in body, "готовность не должна стать жёстким блоком"


# ── Доведено до пользователя ──────────────────────────────────────────────────

def test_endpoint_returns_both_warnings():
    start = _API.index("async def fleet_warnings")
    body = _API[start:start + 1600]
    assert "get_pressure_warning" in body and "get_readiness_warning" in body


def test_endpoint_is_fail_open():
    start = _API.index("async def fleet_warnings")
    body = _API[start:start + 1600]
    assert body.count("except Exception") >= 2


def test_route_registered():
    assert '"/api/miniapp/fleet/warnings"' in _API


def test_mass_ops_screen_loads_and_renders_them():
    assert 'id="massopsWarn"' in _UI
    assert "loadFleetWarnings()" in _UI
    seg = _UI[_UI.index("async function openMassOps"):]
    assert "loadFleetWarnings()" in seg[:600], "экран массовых операций обязан их запрашивать"


def test_warnings_are_escaped():
    seg = _UI[_UI.index("async function loadFleetWarnings"):]
    assert "esc(w)" in seg[:1200]
