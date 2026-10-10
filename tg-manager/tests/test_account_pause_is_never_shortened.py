"""Пауза аккаунта продлевается, но не срезается.

ЧТО БЫЛО. Пауза аккаунта (`tg_accounts.cooldown_until`) — главная защита флота:
после FloodWait или PeerFlood аккаунт обязан отлежаться, иначе следующая
операция его добьёт и Telegram заберёт его совсем. Писали эту паузу семь разных
мест, и большинство — безусловно: `cooldown_until = NOW() + интервал`.

Безусловная запись СРЕЗАЕТ уже стоящую паузу, если новая короче. Это не теория:
операция получает список аккаунтов при постановке (`params["account_ids"]`),
поэтому операция, начатая до того, как аккаунту выписали суточную паузу за
PeerFlood, всё равно работает им, получает минутный FloodWait — и суточная
защита превращается в минутную. Следующая операция берёт аккаунт, помеченный
Telegram за спам.

Вторая половина: сбой самой записи уходил в лог (`log_exc_swallow`,
`log.debug`), и наружу это выглядело как поставленная пауза — аккаунт оставался
доступным сразу после флуда, а узнать об этом было негде.

ФИКС: одна дверь `flood_engine.apply_cooldown` — GREATEST (только продление),
одна повторная попытка, log.error и счётчик при потере, скоуп по владельцу.
Места, которые пишут паузу своим запросом, обязаны либо продлевать через
GREATEST, либо иметь в WHERE условие «активной паузы нет».
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _sql_constants(src: str):
    """Все строковые константы кода (AST, не комментарии и не докстринги)."""
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            first = (node.body or [None])[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docs.add(id(first.value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            yield node.value


def _shortens_the_pause(sql: str) -> bool:
    """Может ли этот запрос сделать стоящую паузу КОРОЧЕ."""
    flat = " ".join(sql.split())
    # Только пауза АККАУНТА. У предохранителя операций (op_circuit_breaker) своя
    # колонка с тем же именем и свой смысл — её сюда мерить нельзя.
    import re as _re0
    if not _re0.search(r"UPDATE\s+tg_accounts", flat, _re0.I):
        return False
    if "cooldown_until" not in flat:
        return False
    low = flat.lower()
    # снятие паузы — отдельное осознанное действие, не срезание
    if "cooldown_until = null" in low or "cooldown_until=null" in low:
        return False
    # присваивание вообще есть?
    import re as _re
    if not _re.search(r"cooldown_until\s*=\s*[^=]", flat):
        return False
    # продление
    if "greatest" in low:
        return False
    # запрос бьёт только по аккаунтам без активной паузы
    if _re.search(r"cooldown_until\s*(is\s+null|<)", low.split("where", 1)[-1]):
        return False
    # условная запись: CASE … WHEN cooldown_until IS NULL …
    if "case" in low and "cooldown_until is null" in low:
        return False
    return True


def _offenders():
    out = []
    for folder in ("services", "bot", "database"):
        for f in sorted((ROOT / folder).rglob("*.py")):
            src = f.read_text(encoding="utf-8")
            if "cooldown_until" not in src:
                continue
            for sql in _sql_constants(src):
                if _shortens_the_pause(sql):
                    out.append((f.relative_to(ROOT).as_posix(),
                                " ".join(sql.split())[:120]))
    return out


def test_the_detector_bites_on_a_known_sample():
    """Самопроверка измерителя — до того, как верить его пустому списку.

    Правило из CLAUDE.md: детектор, дающий пустой (или огромный) список,
    проверяется на заведомо больном и заведомо здоровом примере.
    """
    bad = ("UPDATE tg_accounts SET cooldown_until = NOW() + ($1 * INTERVAL "
           "'1 second') WHERE id=$2")
    good_greatest = ("UPDATE tg_accounts SET cooldown_until = GREATEST("
                     "COALESCE(cooldown_until, NOW()), NOW() + ($1 * INTERVAL "
                     "'1 second')) WHERE id=$2")
    good_guarded = ("UPDATE tg_accounts SET cooldown_until=NOW()+($1 * INTERVAL "
                    "'1 hour') WHERE id=$2 AND (cooldown_until IS NULL OR "
                    "cooldown_until<NOW())")
    good_clear = "UPDATE tg_accounts SET cooldown_until = NULL WHERE id=$1"
    assert _shortens_the_pause(bad)
    assert not _shortens_the_pause(good_greatest)
    assert not _shortens_the_pause(good_guarded)
    assert not _shortens_the_pause(good_clear)
    assert not _shortens_the_pause("SELECT cooldown_until FROM tg_accounts")
    # у предохранителя операций своя колонка cooldown_until — не наше измерение
    assert not _shortens_the_pause(
        "UPDATE op_circuit_breaker SET cooldown_until = NOW() + ($1 * INTERVAL "
        "'1 second') WHERE owner_id=$2")


def test_no_place_can_shorten_an_account_pause():
    bad = _offenders()
    assert not bad, (
        "эти запросы срезают уже стоящую паузу аккаунта: суточная пауза за "
        "PeerFlood превратится в минутную, и аккаунт уйдёт в работу помеченным "
        f"за спам — {bad}")


# ── Сама дверь ───────────────────────────────────────────────────────────────

class _Pool:
    def __init__(self, fail_first=0):
        self.fail_left = fail_first
        self.calls: list[tuple] = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        if self.fail_left > 0:
            self.fail_left -= 1
            raise RuntimeError("БД недоступна")
        return "UPDATE 1"


def test_the_door_only_extends_the_pause():
    from services import flood_engine

    pool = _Pool()
    assert asyncio.run(flood_engine.apply_cooldown(pool, 7, 60)) is True
    query = " ".join(pool.calls[0][0].split())
    assert "GREATEST" in query, (
        "дверь пишет паузу безусловно — она сможет срезать более длинную")
    assert not _shortens_the_pause(query)


def test_the_door_retries_once_and_then_reports():
    from services import flood_engine, metrics

    pool = _Pool(fail_first=1)
    assert asyncio.run(flood_engine.apply_cooldown(pool, 7, 60)) is True
    assert len(pool.calls) == 2, "повторной попытки нет"

    metrics.reset()
    lost = _Pool(fail_first=9)
    assert asyncio.run(flood_engine.apply_cooldown(lost, 7, 60)) is False, (
        "потерянная пауза возвращает «всё хорошо»: вызывающий оставит аккаунт "
        "в работе сразу после флуда")
    counters = metrics.snapshot().get("counters") or {}
    assert any("infragram_account_cooldown_write_failures_total" in str(k)
               for k in counters), (
        f"потеря паузы не видна снаружи: {counters}")
    assert ("infragram_account_cooldown_write_failures_total"
            in metrics._HELP), "у метрики нет описания — она не попадёт в выдачу"


def test_the_door_can_scope_by_owner():
    """Аккаунт чужого владельца нельзя трогать даже паузой."""
    from services import flood_engine

    pool = _Pool()
    asyncio.run(flood_engine.apply_cooldown(pool, 7, 60, owner_id=555))
    query = " ".join(pool.calls[0][0].split())
    assert "owner_id" in query, "скоуп по владельцу потерян"
    assert 555 in pool.calls[0][1]


def test_no_pool_is_not_a_failure():
    """Часть вызовов приходит без пула (account_manager) — это не потеря."""
    from services import flood_engine

    assert asyncio.run(flood_engine.apply_cooldown(None, 7, 60)) is True
