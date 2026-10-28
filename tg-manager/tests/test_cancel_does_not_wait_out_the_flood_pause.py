"""Отмена не ждёт конца флуд-паузы — иначе флот стоит ещё четверть часа.

РАЗРЫВ. Отмена доходит до исполнителя опросом: каждый исполнитель спрашивает
`_is_cancelled` на каждой цели. А пауза, назначенную Telegram, пересиживалась
ОДНИМ `asyncio.sleep` длиной до `_FLOOD_INLINE_MAX_S` — по умолчанию четверть
часа. Всё это время отмена не замечалась: владелец нажал «Отменить», операция
показана работающей, арендованные аккаунты заняты, и ничего не происходит.

Это ровно то поведение, про которое предупреждает сам код флуд-пауз: молчание
обходится дороже сообщения, потому что владелец начинает отменять и запускать
заново, добирая новых ограничений.

ЧТО ПРОВЕРЯЕМ. Пауза дробится и опрашивает отмену; выход из неё ранний только
там, где это безопасно (следующий виток цикла начинается с проверки отмены),
а у единственного места, где следом идёт ПОВТОР реального действия, стоит
проверка на месте. Саму паузу Telegram это не укорачивает: штраф аккаунту уже
записан в flood_engine, и держат его пейсинг с карантином — короче становится
только простой флота по отменённой операции.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")


class _Pool:
    def __init__(self, status: str | None = "running"):
        self._status = status
        self.reads = 0

    async def fetchrow(self, sql, *a):
        self.reads += 1
        if self._status is None:
            return None            # операцию удалили
        return {"status": self._status}


@pytest.fixture
def ow(monkeypatch):
    from services import op_worker

    op_worker._cancel_cache.clear()
    # Шаг опроса — настоящий 15с; в проверке он не нужен.
    monkeypatch.setattr(op_worker, "_CANCEL_PAUSE_STEP_S", 0.01)
    yield op_worker
    op_worker._cancel_cache.clear()


# ── Поведение паузы ─────────────────────────────────────────────────────────

def test_a_cancelled_operation_leaves_the_pause_early(ow):
    """Иначе отменённая операция держит флот до конца паузы."""
    slept = asyncio.run(ow.bounded_flood_sleep(
        1.0, "проверка", pool=_Pool("cancelled"), op_id=42))
    assert slept < 1.0, "пауза не прервалась: флот занят отменённой операцией"
    assert slept > 0, "пауза вообще не состоялась — это уже обход лимитов"


def test_a_deleted_operation_leaves_the_pause_early(ow):
    """Удаление — такое же «стоп» (общая дверь op_status.stop_requested)."""
    slept = asyncio.run(ow.bounded_flood_sleep(
        1.0, "проверка", pool=_Pool(None), op_id=42))
    assert slept < 1.0


def test_a_live_operation_waits_the_whole_pause(ow):
    """Самопроверка: без отмены пауза выдерживается целиком."""
    slept = asyncio.run(ow.bounded_flood_sleep(
        0.05, "проверка", pool=_Pool("running"), op_id=42))
    assert slept == pytest.approx(0.05, abs=1e-6), (
        "пауза Telegram укорочена без отмены — это обход лимита платформы")


def test_a_caller_without_the_operation_id_still_sleeps(ow):
    """Старый вызов (без pool/op_id) обязан вести себя как раньше."""
    slept = asyncio.run(ow.bounded_flood_sleep(0.02, "проверка"))
    assert slept == pytest.approx(0.02, abs=1e-6)


def test_the_cancel_is_noticed_without_hammering_the_database(ow):
    """Опрос дробный, но кэш `_is_cancelled` не даёт ему стать потоком запросов."""
    pool = _Pool("running")
    asyncio.run(ow.bounded_flood_sleep(0.2, "проверка", pool=pool, op_id=7))
    assert pool.reads <= 3, (
        f"за паузу ушло {pool.reads} запросов — кэш отмены перестал работать")


# ── Закрытая перепись вызовов ───────────────────────────────────────────────

def _pause_calls() -> list[tuple[int, set[str]]]:
    """Все вызовы bounded_flood_sleep: строка и переданные имена аргументов."""
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "bounded_flood_sleep":
            continue
        out.append((node.lineno, {kw.arg for kw in node.keywords if kw.arg}))
    return out


def test_every_flood_pause_can_be_cancelled():
    """Вызов без pool/op_id — это снова четверть часа глухоты к отмене."""
    calls = _pause_calls()
    assert calls, "вызовов bounded_flood_sleep не найдено — разбор сломан"
    deaf = sorted(line for line, kws in calls
                  if not {"pool", "op_id"} <= kws)
    assert not deaf, (
        "эти флуд-паузы не слышат отмену владельца (нет pool/op_id): "
        f"строки {deaf}. Операция держит слот и арендованные аккаунты до конца "
        "паузы — до четверти часа после нажатия «Отменить». Передайте "
        "pool=pool, op_id=op_id, а если следом идёт повтор реального действия "
        "— поставьте там же проверку _is_cancelled")


def test_the_one_dangerous_caller_checks_cancellation_itself():
    """У gp_bot после паузы идёт ПОВТОР создания бота — там нужна проверка."""
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    at = src.index('bounded_flood_sleep(wait_s, "gp_bot"')
    after = src[at:at + 2000]
    assert "_is_cancelled" in after, (
        "после паузы gp_bot нет проверки отмены, а следом создаётся бот через "
        "BotFather — отменённая операция сделала бы ещё одно реальное действие")
    assert "create_bot_via_botfather" in after, (
        "повтор создания бота после паузы пропал из вида — проверка смотрит "
        "уже не туда, поправьте окно или ориентир")
    assert after.index("_is_cancelled") < after.index(
        "create_bot_via_botfather"), "проверка стоит ПОСЛЕ повтора действия"


def test_the_census_detector_bites():
    """Самопроверка: вызов без аргументов обязан считаться находкой."""
    sick = ast.parse('await bounded_flood_sleep(60, "где-то")\n')
    calls = []
    for node in ast.walk(sick):
        if isinstance(node, ast.Call):
            calls.append({kw.arg for kw in node.keywords if kw.arg})
    assert calls and not {"pool", "op_id"} <= calls[0]
