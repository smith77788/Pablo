"""Ручной сброс риска доводится до конца: пульс и acc_status, а не только кулдаун.

Жалоба владельца: «кулдаун не сбрасывается на рисковых аккаунтах». Первый заход
починил только гейт операций (is_account_quarantined учитывает risk_cleared_at),
и осталось два хвоста, из-за которых жалоба воспроизводилась дальше:

  1. get_account_health (риск-пульс, его мержит список и деталь аккаунта в
     Mini App) считал restriction_events и account_flood_log БЕЗ оглядки на
     risk_cleared_at — операции аккаунт уже брали, а UI продолжал светить
     «Карантин».
  2. Кнопка чистила cooldown_until, но оставляла acc_status='cooldown'. Его
     снимал только пассивный само-heal в account_monitor (цикл раз в час), а до
     тех пор account_health.load_from_db считал такой аккаунт спамблоком
     (−40 к health_score, suitability dm/invite = False) и get_sorted_accounts
     молча выкидывал его из подбора.

Тесты без БД: SQL пульса снимаем с фейкового пула, ветку кнопки — из исходника
(хендлер вложенный и не импортируется отдельно, как и в остальных тестах
mini_app_api). Живой Postgres — в tests/test_risk_cleared_at_postgres.py.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from services.infra_memory import get_account_health

ROOT = Path(__file__).resolve().parents[1]


class _RecordingPool:
    """Пул, который запоминает SQL и не возвращает строк."""

    def __init__(self):
        self.queries: list[str] = []

    async def fetch(self, q, *a):
        self.queries.append(q)
        return []


def _pulse_sql() -> str:
    pool = _RecordingPool()
    asyncio.run(get_account_health(pool, 42))
    assert pool.queries, "get_account_health обязан сходить в БД"
    return pool.queries[0]


def _guarded_subqueries(sql: str) -> dict[str, bool]:
    """Для каждого подзапроса по иммунной таблице — есть ли фильтр risk_cleared_at.

    Режем SQL по началам подзапросов, чтобы проверять КАЖДЫЙ из них, а не факт
    того, что колонка вообще где-то упомянута.
    """
    out: dict[str, bool] = {}
    # (SELECT COUNT(*) FROM <table> <alias> ... ) AS <name>
    for m in re.finditer(
        r"\(SELECT COUNT\(\*\) FROM (restriction_events|account_flood_log)\b"
        r"(.*?)\)\s+AS\s+(\w+)",
        sql,
        re.S,
    ):
        body, name = m.group(2), m.group(3)
        out[name] = "risk_cleared_at" in body
    return out


def test_pulse_subqueries_honour_risk_cleared_at():
    """Каждый счётчик риска в пульсе обязан пропускать историю до ручного сброса."""
    guarded = _guarded_subqueries(_pulse_sql())
    assert set(guarded) == {"restrictions", "severe", "floods"}, (
        f"состав счётчиков пульса изменился: {sorted(guarded)} — "
        "проверь, что новый счётчик тоже учитывает risk_cleared_at")
    unguarded = sorted(n for n, ok in guarded.items() if not ok)
    assert not unguarded, (
        f"счётчики {unguarded} считают события старше risk_cleared_at — "
        "после ручного сброса UI снова будет врать «Карантин»")


def test_pulse_guard_compares_event_time_not_now():
    """Фильтр сравнивает ВРЕМЯ СОБЫТИЯ с отметкой, а не просто «отметка есть».

    Иначе сброс риска навсегда ослепил бы пульс: новое ограничение после сброса
    обязано снова уводить аккаунт в карантин.
    """
    sql = _pulse_sql()
    n = len(re.findall(r"risk_cleared_at IS NULL\s+OR\s+\w+\.created_at > a\.risk_cleared_at",
                       sql))
    assert n == 3, (
        f"ожидалось 3 фильтра вида «created_at > risk_cleared_at», найдено {n}: "
        "сброс риска не должен ослеплять пульс навсегда")


def _reset_cooldown_branch() -> str:
    body = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    m = re.search(r'elif act == "reset_cooldown":(.*?)\n        elif act == "', body, re.S)
    assert m, "ветка reset_cooldown не найдена — тест устарел вместе с хендлером"
    return m.group(1)


def test_reset_cooldown_clears_cooldown_risk_and_status():
    """Кнопка обязана снимать все три тормоза разом, иначе жалоба повторится."""
    seg = _reset_cooldown_branch()
    assert "cooldown_until=NULL" in seg, "durable-кулдаун не чистится"
    assert "risk_cleared_at=NOW()" in seg, "риск-карантин не снимается"
    assert re.search(r"acc_status\s*=\s*CASE", seg), (
        "acc_status='cooldown' остаётся до часового само-heal — "
        "аккаунт не берётся в подбор (suitability dm/invite = False)")


def test_reset_cooldown_does_not_clobber_hard_statuses():
    """Снимаем ТОЛЬКО транзиентный 'cooldown' — 'banned'/'warming' не трогаем."""
    seg = _reset_cooldown_branch()
    assert "'cooldown'" in seg and "ELSE acc_status" in seg, (
        "CASE обязан оставлять чужие статусы как есть")
    assert "session_conflict_at IS NULL" in seg, (
        "при устойчивом конфликте сессии статус снимать нельзя — "
        "сессию надо перезалить, иначе статус будет скакать")


def test_reset_cooldown_scoped_to_owner():
    """Владелец сбрасывает только СВОЙ аккаунт."""
    seg = _reset_cooldown_branch()
    assert "owner_id=$2" in seg, "UPDATE обязан быть owner-scoped"


def test_reset_cooldown_unblocks_in_memory_pulse():
    """process-local suitability снимается сразу: load_from_db читает раз в час."""
    seg = _reset_cooldown_branch()
    assert "account_health" in seg and "suitability" in seg, (
        "без сброса in-memory suitability get_sorted_accounts продолжит "
        "выкидывать аккаунт из подбора до следующего цикла здоровья")


def test_reset_cooldown_in_memory_failure_is_not_fatal():
    """Пульс в памяти — вспомогательный: его сбой не должен ронять кнопку."""
    seg = _reset_cooldown_branch()
    m = re.search(r"from services import account_health.*?except Exception:", seg, re.S)
    assert m, "правка in-memory пульса обязана быть в try/except"
