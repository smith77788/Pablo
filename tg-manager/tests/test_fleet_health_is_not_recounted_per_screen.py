"""Пульс здоровья не пересчитывается по всему флоту ради одного экрана.

Замер 08.11.2026 на живой базе с 10 000 аккаунтов одного владельца:
`get_account_health` проходил весь флот с четырьмя подзапросами на строку,
и его звали страница списка аккаунтов (ради 100 строк), дашборд, итоги и
«требует внимания». Открытие главной стоило 4–6 полных расчётов подряд —
0,8 с одному владельцу, 4,4 с при десяти, цикл событий (а в нём живёт и бот)
стоял до 0,3 с. Страница списка отвечала 220 мс, из них ~200 — пульс.

Здесь стережём три двери:
  * страница списка и карточка считают пульс только для своих аккаунтов;
  * сводку для главной одновременные запросы считают ОДИН раз, и она
    живёт недолго;
  * действие владельца в мини-аппе сносит сводку — своё действие он видит
    сразу (тот же общий сброс, что у кэша ответов).
Эквивалентность нового запроса старому — на живой базе:
tests/test_account_health_is_one_pass_postgres.py.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from services import infra_memory

ROOT = Path(__file__).resolve().parents[1]


def _row(i, status="active"):
    return {"id": i, "phone": f"+1{i}", "acc_status": status, "trust_score": 0.9,
            "has_session": True, "cd_active": False, "restrictions": 0,
            "severe": 0, "severe_gate": 0, "floods": 0}


class _Pool:
    def __init__(self, rows, delay=0.0):
        self.rows, self.delay, self.calls, self.args = rows, delay, 0, []

    async def fetch(self, q, *a):
        self.calls += 1
        self.args.append(a)
        await asyncio.sleep(self.delay)
        return self.rows


def test_page_scope_reaches_the_query():
    pool = _Pool([_row(1), _row(2)])
    out = asyncio.run(infra_memory.get_account_health(pool, 7, account_ids=[2, 1, 2]))
    assert pool.args[0][-1] == [1, 2], "id страницы обязаны дойти до запроса"
    assert out["summary"]["total"] == 2


def test_empty_page_does_not_touch_the_database():
    pool = _Pool([_row(1)])
    out = asyncio.run(infra_memory.get_account_health(pool, 7, account_ids=[]))
    assert pool.calls == 0 and out["summary"]["total"] == 0


def test_whole_fleet_still_default():
    pool = _Pool([_row(1)])
    asyncio.run(infra_memory.get_account_health(pool, 7))
    assert pool.args[0][-1] is None, "без account_ids пульс считает весь флот"


def test_account_list_and_card_ask_only_for_their_accounts():
    src = (ROOT / "services" / "mini_app_accounts.py").read_text(encoding="utf-8")
    calls = re.findall(r"get_account_health\((.*?)\)\n", src, re.DOTALL)
    # Список и карточка — со своими id; ручка «Здоровье аккаунтов» — весь флот.
    scoped = [c for c in calls if "account_ids" in c]
    assert len(scoped) >= 2, (
        "страница списка и карточка аккаунта снова считают пульс всего флота — "
        f"на 10 000 аккаунтов это ~200 мс на каждый запрос: {calls}")


def test_concurrent_summaries_share_one_count():
    infra_memory.drop_health_summary()
    pool = _Pool([_row(1), _row(2, "banned")], delay=0.05)

    async def go():
        return await asyncio.gather(
            *(infra_memory.get_account_health_summary(pool, 7) for _ in range(6)))

    res = asyncio.run(go())
    assert pool.calls == 1, f"шесть одновременных запросов главной посчитали флот {pool.calls} раз"
    assert all(r == res[0] for r in res)
    assert res[0]["total"] == 2 and res[0]["quarantine"] == 1
    assert 0 < res[0]["avg_score"] < 1


def test_summary_is_cached_briefly_and_dropped_by_owner_action():
    infra_memory.drop_health_summary()
    pool = _Pool([_row(1)])
    asyncio.run(infra_memory.get_account_health_summary(pool, 7))
    asyncio.run(infra_memory.get_account_health_summary(pool, 7))
    assert pool.calls == 1, "сводка в пределах срока берётся из кэша"
    assert infra_memory.HEALTH_SUMMARY_TTL_S <= 15, "пульс — живой сигнал, срок короткий"

    from services import mini_app_api
    mini_app_api._cache_drop_user(7)
    asyncio.run(infra_memory.get_account_health_summary(pool, 7))
    assert pool.calls == 2, (
        "действие владельца в мини-аппе не сбросило сводку пульса — "
        "«снял риск» и не увидел этого на главной")


def test_failed_count_is_not_cached():
    infra_memory.drop_health_summary()

    class _Broken:
        calls = 0

        async def fetch(self, q, *a):
            _Broken.calls += 1
            raise RuntimeError("блип базы")

    p = _Broken()
    asyncio.run(infra_memory.get_account_health_summary(p, 7))
    asyncio.run(infra_memory.get_account_health_summary(p, 7))
    assert _Broken.calls == 2, "сбой расчёта запомнился как «0 аккаунтов» на весь срок кэша"


def test_dashboard_reads_the_shared_summary():
    src = (ROOT / "services" / "mini_app_dashboard.py").read_text(encoding="utf-8")
    assert "get_account_health_summary" in src and "avg_score" in src, (
        "дашборд снова считает весь флот сам, мимо общей сводки")
