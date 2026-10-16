"""Одно и то же про операцию владельцу не присылают дважды.

ЧТО БЫЛО. Анти-повтор в `db.notify_if_enabled` живёт В ПАМЯТИ ПРОЦЕССА, и окно у
него одна минута. Для судьбы операции это не работает сразу по двум причинам:

  * Railway перезапускает процесс на каждом деплое — и операция возвращается в
    очередь ровно по этой причине. После рестарта память пуста, и владелец
    получает то же сообщение заново;
  * между двумя откладываниями одной операции проходят десятки минут, то есть
    минутное окно к этому моменту давно закрылось.

Это ровно та жалоба, из-за которой в db.py появился ПЕРСИСТЕНТНЫЙ анти-спам
(`notify_dedup_ok`, schema_v184): «одинаковые уведомления каждые несколько
минут». Пути операций его не использовали.

Хуже всего с вехами прогресса. Веха — событие ОПЕРАЦИИ, а `done_items`
обнуляется на каждом возврате в очередь, поэтому возобновлённый прогон заново
проходит 25%, 50% и 75% по уже взятым целям. Владелец получал три вехи по
второму и третьему разу за одну операцию, и выглядело это как работа, начатая с
нуля.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

from services import op_worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class _Pool:
    async def fetchrow(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []

    async def execute(self, *a, **k):
        return "UPDATE 1"


class _Bot:
    pass


@pytest.fixture
def wires(monkeypatch):
    seen: dict = {"sent": [], "asked": []}

    async def _notify(pool, bot, owner_id, pref, text, **kw):
        seen["sent"].append((owner_id, text, kw.get("dedup_key")))
    monkeypatch.setattr(op_worker.db, "notify_if_enabled", _notify)

    def _gate(allow: bool):
        async def _ok(pool, user_id, key, cooldown_s):
            seen["asked"].append((user_id, key, cooldown_s))
            return allow
        monkeypatch.setattr(op_worker.db, "notify_dedup_ok", _ok)

    seen["gate"] = _gate
    return seen


def _tell(**kw):
    async def _go():
        await op_worker._notify_owner_about_op(
            _Pool(), _Bot(), 555, "текст про операцию", **kw)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


# ── Повтор не уходит ─────────────────────────────────────────────────────────

def test_a_message_already_sent_is_not_sent_again(wires):
    wires["gate"](False)
    _tell(dedup_key="no-accounts:77")
    assert not wires["sent"], (
        "владелец получил то же сообщение об операции по второму разу — после "
        "рестарта процесса или на следующем круге откладывания"
    )


def test_a_first_message_goes_through(wires):
    wires["gate"](True)
    _tell(dedup_key="no-accounts:77")
    assert len(wires["sent"]) == 1


def test_the_gate_is_persistent_and_scoped_to_the_operation(wires):
    wires["gate"](True)
    _tell(dedup_key="no-accounts:77")
    assert wires["asked"], "спрошен был только анти-повтор в памяти процесса"
    user_id, key, cooldown = wires["asked"][0]
    assert user_id == 555
    assert "77" in key, "ключ не привязан к операции — заглушит чужие сообщения"
    assert cooldown >= 24 * 3600, (
        f"окно {cooldown}с короче жизни операции: многочасовая рассылка "
        f"переживает несколько деплоев"
    )


def test_a_message_without_a_key_still_goes_out(wires):
    """Без ключа глушить нечего — такое сообщение уникально по смыслу."""
    wires["gate"](False)
    _tell()
    assert len(wires["sent"]) == 1


def test_a_broken_gate_does_not_silence_the_news(wires):
    """Сбой анти-спама не повод скрыть от владельца судьбу его операции."""
    async def _boom(pool, user_id, key, cooldown_s):
        raise RuntimeError("БД недоступна")

    import pytest as _pt
    monkey = _pt.MonkeyPatch()
    monkey.setattr(op_worker.db, "notify_dedup_ok", _boom)
    try:
        _tell(dedup_key="poisoned:9")
    finally:
        monkey.undo()
    assert len(wires["sent"]) == 1


# ── Вехи прогресса — один раз на операцию ────────────────────────────────────

def test_milestones_are_once_per_operation_not_once_per_run():
    body = _read("services/op_worker.py")
    start = body.index("if milestone is not None:")
    seg = body[start:start + 1200]
    assert "notify_dedup_ok(" in seg, (
        "веха прогресса объявляется из памяти процесса: возобновлённый прогон "
        "заново пройдёт 25/50/75% по уже взятым целям, и владелец получит их "
        "второй раз"
    )
    assert re.search(r'f"op-milestone:\{op_id\}:\{milestone\}"', seg), (
        "ключ вехи не различает операцию и саму веху"
    )


def test_every_outcome_path_passes_a_dedup_key():
    """Иначе персистентный анти-повтор для этого пути просто не включится."""
    body = _read("services/op_worker.py")
    calls = [m.start() for m in re.finditer(r"_notify_owner_about_op\(", body)]
    # Первое вхождение — определение функции, его не считаем.
    calls = [c for c in calls if not body[:c].rstrip().endswith("async def")]
    assert len(calls) >= 4, f"путей уведомления найдено всего {len(calls)}"
    missing = []
    for c in calls:
        seg = body[c:c + 900]
        seg = seg[:seg.index(")\n") + 2] if ")\n" in seg else seg
        if "dedup_key" not in seg:
            missing.append(body[:c].count("\n") + 1)
    assert not missing, (
        "эти пути говорят владельцу о судьбе операции без ключа анти-повтора — "
        f"после рестарта сообщение уйдёт заново: строки {missing}"
    )
