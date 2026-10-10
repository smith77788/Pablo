"""Выбор аккаунтов перестал брать тех, у кого прокси не отвечает.

Разрыв. `resource_selector.select_all_active` — единая дверь, через которую
операции получают аккаунты, — не смотрела на живость прокси ВООБЩЕ. Аккаунт,
чей прокси умер (кончился трафик, провайдер снял адрес, сменился пароль),
продолжал попадать в КАЖДУЮ операцию: каждая попытка падала сетевой ошибкой,
съедала слот и приходила в отчёт как ошибка аккаунта. Снаружи это выглядит как
«аккаунты сломались», и человек идёт чинить аккаунты — самое дорогое, что у
него есть, — вместо замены дешёвого прокси.

Ровно эту подмену и называет сторож прокси; здесь она чинится в действии.
"""
from __future__ import annotations

import asyncio

from services import infra_orchestrator as IO
from services import resource_selector as RS


def _acc(i, alive=True, streak=0, has_proxy=True):
    return {"id": i,
            "proxy_alive": (None if not has_proxy else alive),
            "proxy_fail_streak": (0 if not has_proxy else streak)}


def test_account_on_a_confirmed_dead_proxy_is_left_out():
    usable, blocked = RS.split_by_dead_proxy(
        [_acc(1), _acc(2, alive=False, streak=RS.PROXY_DEAD_STREAK)])
    assert [r["id"] for r in usable] == [1]
    assert [r["id"] for r in blocked] == [2]


def test_a_single_blip_is_not_a_dead_proxy():
    """Публичные прокси регулярно моргают: одна неудачная проверка не повод
    выбрасывать аккаунт из операции."""
    usable, blocked = RS.split_by_dead_proxy([_acc(1, alive=False, streak=1)])
    assert len(usable) == 1 and not blocked


def test_account_without_a_proxy_is_never_called_dead():
    """Работа с реального IP хоста — законный режим, а не поломка."""
    usable, blocked = RS.split_by_dead_proxy([_acc(1, has_proxy=False)])
    assert len(usable) == 1 and not blocked


def test_unchecked_proxy_keeps_its_account():
    """is_alive = NULL значит «ещё не проверяли», а не «мёртв»."""
    usable, _b = RS.split_by_dead_proxy([_acc(1, alive=None)])
    assert len(usable) == 1


def test_rows_from_an_older_query_do_not_break_the_split():
    usable, blocked = RS.split_by_dead_proxy([{"id": 9}])
    assert len(usable) == 1 and not blocked


def test_threshold_matches_the_watchdog():
    """Порог обязан совпадать с тем, при котором сторож объявляет прокси
    мёртвым, иначе продукт говорит одно, а делает другое."""
    from services import proxy_watchdog

    assert RS.PROXY_DEAD_STREAK == proxy_watchdog.FAIL_STREAK_TO_ALERT


def test_selector_asks_the_database_for_proxy_liveness():
    import inspect

    src = inspect.getsource(RS.select_all_active)
    assert "proxy_alive" in src and "proxy_fail_streak" in src
    assert "split_by_dead_proxy" in src
    # Если мёртвыми оказались ВСЕ — операцию не рушим: пустой список читался бы
    # как «аккаунтов нет» и увёл бы человека не туда.
    assert "not _usable" in src


# ── Предупреждение до запуска ──────────────────────────────────────────────

class _Pool:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, q, *a):
        if self._row is None:
            raise RuntimeError("база недоступна")
        return self._row


def _warn(with_proxy, on_dead):
    return asyncio.run(IO.get_dead_proxy_warning(
        _Pool({"with_proxy": with_proxy, "on_dead": on_dead}), 1))


def test_partial_outage_says_the_operation_will_cover_less():
    w = _warn(with_proxy=10, on_dead=4)
    assert w and "4" in w and "10" in w


def test_total_outage_is_worded_differently():
    w = _warn(with_proxy=5, on_dead=5)
    assert w and "ВСЕХ" in w


def test_silent_when_every_proxy_answers():
    assert _warn(with_proxy=10, on_dead=0) is None


def test_warning_is_fail_open():
    assert asyncio.run(IO.get_dead_proxy_warning(_Pool(None), 1)) is None


def test_warning_reaches_the_mass_operation_banner():
    import pathlib

    api = (pathlib.Path(__file__).resolve().parent.parent
           / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert "get_dead_proxy_warning" in api
