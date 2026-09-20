"""Ручной сброс риска доводится до конца и работает одинаково из любой кнопки.

Жалоба владельца: «кулдаун не сбрасывается на рисковых аккаунтах». Первый заход
починил только гейт операций (is_account_quarantined учитывает risk_cleared_at).
Осталось три хвоста:

  1. get_account_health (риск-пульс, его мержит список и деталь аккаунта в
     Mini App) считал restriction_events и account_flood_log БЕЗ оглядки на
     risk_cleared_at — операции аккаунт уже брали, а UI светил «Карантин».
  2. Кнопка чистила cooldown_until, но оставляла acc_status='cooldown'. Его
     снимал только само-heal в account_monitor (раз в час), а до тех пор
     account_health.load_from_db считал аккаунт спамблоком (suitability
     dm/invite = False) и get_sorted_accounts молча выкидывал его из подбора.
  3. Кнопок было три (Mini App, бот — один аккаунт, бот — все сразу), каждая
     написана порознь и пропускала свой набор шагов: Mini App не чистил
     in-memory кулдаун flood_engine, бот не ставил risk_cleared_at и снимал
     статус даже при устойчивом конфликте сессии.

Теперь сброс — одна функция (services/account_reset.py), и кнопки только зовут
её. Живой Postgres — в tests/test_risk_cleared_at_postgres.py.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from services.infra_memory import get_account_health

ROOT = Path(__file__).resolve().parents[1]


class _RecordingPool:
    """Пул, который запоминает SQL и переданные параметры."""

    def __init__(self, rows=None, result="UPDATE 1"):
        self._rows = rows or []
        self._result = result
        self.queries: list[str] = []
        self.executed: list[tuple] = []

    async def fetch(self, q, *a):
        self.queries.append(q)
        return self._rows

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return self._result


# ── риск-пульс ────────────────────────────────────────────────────────────────

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
    for m in re.finditer(
        r"\(SELECT COUNT\(\*\) FROM (restriction_events|account_flood_log)\b"
        r"(.*?)\)\s+AS\s+(\w+)",
        sql,
        re.S,
    ):
        out[m.group(3)] = "risk_cleared_at" in m.group(2)
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
    n = len(re.findall(
        r"risk_cleared_at IS NULL\s+OR\s+\w+\.created_at > a\.risk_cleared_at",
        _pulse_sql()))
    assert n == 3, (
        f"ожидалось 3 фильтра вида «created_at > risk_cleared_at», найдено {n}: "
        "сброс риска не должен ослеплять пульс навсегда")


# ── единый сброс ──────────────────────────────────────────────────────────────

def _reset_one(acc_id=7, owner_id=99, result="UPDATE 1"):
    from services.account_reset import reset_account
    pool = _RecordingPool(result=result)
    ok = asyncio.run(reset_account(pool, acc_id, owner_id))
    assert pool.executed, "сброс обязан выполнить UPDATE"
    return pool, ok


def test_reset_clears_cooldown_risk_and_status():
    """Кнопка обязана снимать все тормоза разом, иначе жалоба повторится."""
    pool, ok = _reset_one()
    sql, args = pool.executed[0]
    assert ok is True
    assert "cooldown_until = NULL" in sql, "durable-кулдаун не чистится"
    assert "risk_cleared_at = NOW()" in sql, "риск-карантин не снимается"
    assert re.search(r"acc_status\s*=\s*CASE", sql), (
        "acc_status='cooldown' остаётся до часового само-heal — "
        "аккаунт не берётся в подбор (suitability dm/invite = False)")
    assert args == (7, 99), "id и владелец обязаны уходить параметрами"


def test_reset_does_not_clobber_hard_statuses():
    """Снимаем ТОЛЬКО транзиентный 'cooldown' — 'banned'/'warming' не трогаем."""
    sql = _reset_one()[0].executed[0][0]
    assert "'cooldown'" in sql and "ELSE COALESCE(acc_status,'active')" in sql, (
        "CASE обязан оставлять чужие статусы как есть")
    assert "session_conflict_at IS NULL" in sql, (
        "при устойчивом конфликте сессии статус снимать нельзя — "
        "сессию надо перезалить, иначе статус будет скакать туда-сюда")


def test_reset_is_owner_scoped():
    """Владелец сбрасывает только СВОЙ аккаунт."""
    sql = _reset_one()[0].executed[0][0]
    assert "owner_id=$2" in sql, "UPDATE обязан быть owner-scoped"


def test_reset_reports_missing_account():
    """Чужой или несуществующий аккаунт — честный False, а не «готово»."""
    assert _reset_one(result="UPDATE 0")[1] is False


def test_reset_clears_in_memory_brakes():
    """Кулдаун flood_engine и запрет suitability живут в памяти процесса.

    Очистка БД без них не помогает: подбор аккаунтов смотрит
    flood_engine.is_account_cooling() и account_health.suitability независимо,
    а перечитываются они лишь раз в час.
    """
    from services import account_health as _ah
    from services import flood_engine as _fe
    import time

    acc_id = 4242
    state = _fe.get_account_state(acc_id)
    state.cooldown_until = time.monotonic() + 3600
    health = _ah.get_health(acc_id)
    health.suitability["dm"] = False
    health.suitability["invite"] = False

    _reset_one(acc_id=acc_id)

    assert not _fe.is_account_cooling(acc_id), "in-memory кулдаун обязан сняться"
    assert health.suitability["dm"] and health.suitability["invite"], (
        "запрет на рассылку/инвайт обязан сняться сразу, а не через час")


def test_reset_in_memory_failure_is_not_fatal(monkeypatch):
    """Память — вспомогательная: её сбой не должен ронять сброс в БД."""
    from services import account_reset, flood_engine

    def _boom(_acc_id):
        raise RuntimeError("flood_engine недоступен")

    monkeypatch.setattr(flood_engine, "clear_account_cooldown", _boom)
    pool = _RecordingPool()
    assert asyncio.run(account_reset.reset_account(pool, 7, 99)) is True
    assert pool.executed, "сброс в БД обязан состояться несмотря на сбой памяти"


def test_reset_all_uses_same_steps_and_narrow_set():
    """Массовый сброс — та же логика, набор только с живым окном кулдауна."""
    from services.account_reset import reset_all_cooled

    pool = _RecordingPool(rows=[{"id": 1}, {"id": 2}], result="UPDATE 2")
    n = asyncio.run(reset_all_cooled(pool, 99))
    assert n == 2, "счётчик освобождённых аккаунтов обязан быть честным"
    sql = pool.executed[0][0]
    assert "risk_cleared_at = NOW()" in sql and "session_conflict_at IS NULL" in sql, (
        "массовый сброс не должен расходиться с одиночным")
    assert "cooldown_until > NOW()" in sql, (
        "массовое снятие риска не должно захватывать аккаунты, "
        "которых владелец не видит в списке остывающих")
    assert "owner_id=$1" in sql, "массовый сброс обязан быть owner-scoped"


# ── кнопки зовут общий сброс, а не свою копию ────────────────────────────────

def _call_sites() -> dict[str, str]:
    api = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    dash = (ROOT / "bot" / "handlers" / "health_dashboard.py").read_text(encoding="utf-8")
    m = re.search(r'elif act == "reset_cooldown":(.*?)\n        elif act == "', api, re.S)
    assert m, "ветка reset_cooldown в Mini App не найдена — тест устарел"
    one = re.search(r"async def cb_reset_cooldown_one\(.*?\n(?=@router)", dash, re.S)
    every = re.search(r"async def cb_reset_cooldown_all\(.*?\n(?=@router|\Z)", dash, re.S)
    assert one and every, "кнопки сброса в боте не найдены — тест устарел"
    return {
        "mini_app": m.group(1),
        "бот (один аккаунт)": one.group(0),
        "бот (все сразу)": every.group(0),
    }


def test_all_reset_buttons_go_through_one_implementation():
    """Три кнопки — одна реализация, иначе они снова разойдутся по шагам."""
    for name, body in _call_sites().items():
        assert "account_reset" in body, (
            f"кнопка «{name}» не зовёт общий сброс — шаги снова разъедутся")
        assert "UPDATE tg_accounts" not in body, (
            f"кнопка «{name}» снова правит tg_accounts своим запросом")
        assert "clear_account_cooldown" not in body and "clear_all_cooldowns" not in body, (
            f"кнопка «{name}» чистит память в обход общего сброса")
