"""Два реестра, которые при сбое чтения переставали защищать, и молча.

РАЗРЫВ. Обе защиты читаются запросом, а сбой запроса трактовался как «пусто»:

* журнал приглашений (`invite_dedup`) — пусто значит «никого не приглашали», и
  операция звала ВСЕХ заново. Повторные приглашения тем же людям — прямой путь
  к PEER_FLOOD и бану, то есть самый дорогой исход продукта;
* реестр «не писать» (`contact_opt_out`) — пусто значит «никто не отказывался»,
  и сообщение уходило тем, кто прямо просил больше не писать. Это уже не риск
  аккаунта, а нарушение обещания человеку.

Fail-open у обоих записан осознанно: сорвать операцию целиком дороже, чем не
отфильтровать. Но беззвучным он быть не имеет права — раньше о сбое говорили
`log.warning` и `log_exc_swallow`, то есть никто: логи владелец не читает.

ЧТО ПРОВЕРЯЕМ: одна повторная попытка (сетевой блип больше не выключает защиту
вовсе), сохранённый fail-open — проверяется прямо, чтобы читатель видел в нём
решение, — и счётчик, который сторож защитных записей доносит до владельца
словами.
"""
from __future__ import annotations

import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEDUP_COUNTER = "infragram_invite_dedup_read_failures_total"
OPT_OUT_COUNTER = "infragram_opt_out_read_failures_total"


def _total(name: str) -> float:
    from services import metrics

    return sum(float(l.rsplit(" ", 1)[-1])
               for l in metrics.render().splitlines() if l.startswith(name))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from services import contact_opt_out, invite_dedup, metrics

    metrics.reset()

    async def _no_sleep(*a, **k):
        return None

    # Паузу между попытками убираем через сам модуль asyncio: так проверка не
    # зависит от того, как именно двери его импортируют.
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(contact_opt_out, "load_opted_out", _opted_none,
                        raising=False)
    yield
    metrics.reset()


async def _opted_none(pool, owner_id):
    return set()


class _Pool:
    """Падает на первых `fail_times` чтениях, дальше отвечает честно."""

    def __init__(self, fail_times: int, rows=None):
        self.fail_times = fail_times
        self.reads = 0
        self._rows = rows if rows is not None else []

    async def execute(self, *a, **k):
        return "CREATE TABLE"

    async def fetch(self, *a, **k):
        self.reads += 1
        if self.reads <= self.fail_times:
            raise RuntimeError("соединение с базой потеряно")
        return self._rows


# ── Журнал приглашений ──────────────────────────────────────────────────────

def test_a_blip_does_not_switch_the_invite_dedup_off(monkeypatch):
    """Повтор чтения: иначе одна сетевая заминка звала всех по второму разу."""
    from services import invite_dedup

    calls = {"n": 0}

    async def _keys(pool, owner_id, keys):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("соединение с базой потеряно")
        # Ключ сравнения — `compare_key` целиком, вместе с «@»
        # (см. services/contact_opt_out.compare_key).
        return {"@ivan"}

    monkeypatch.setattr(invite_dedup, "invited_keys", _keys)

    out, dup, opt = asyncio.run(invite_dedup.filter_new(
        _Pool(0), 500, ["ключ-канала"], ["@ivan", "@petr"]))

    assert calls["n"] == 2, "журнал не перечитали"
    assert [str(x).lower() for x in out] == ["@petr"], (
        f"уже приглашённого позвали второй раз: {out}")
    assert dup == 1
    assert _total(DEDUP_COUNTER) == 0, "переживший блип — не сбой защиты"


def test_an_unreadable_invite_journal_is_loud(monkeypatch):
    """Fail-open остаётся, но сбой перестаёт быть тихим."""
    from services import invite_dedup

    async def _always_fails(pool, owner_id, keys):
        raise RuntimeError("журнал недоступен")

    monkeypatch.setattr(invite_dedup, "invited_keys", _always_fails)

    out, dup, opt = asyncio.run(invite_dedup.filter_new(
        _Pool(0), 500, ["ключ-канала"], ["@ivan", "@petr"]))

    assert len(out) == 2, (
        "решение менять не планировали: сорвать приглашение дороже, чем "
        "никого не позвать (так написано в докстринге filter_new)")
    assert _total(DEDUP_COUNTER) >= 1, (
        "дедуп перестал работать молча — это повторные приглашения без следов")


# ── Реестр «не писать» ──────────────────────────────────────────────────────

def test_a_blip_does_not_switch_the_do_not_contact_registry_off(monkeypatch):
    from services import contact_opt_out, metrics

    monkeypatch.undo()                      # нужен НАСТОЯЩИЙ load_opted_out
    metrics.reset()

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    pool = _Pool(fail_times=1, rows=[{"target": "ivan"}])

    got = asyncio.run(contact_opt_out.load_opted_out(pool, 500))

    assert got == {"ivan"}, "реестр не перечитали — фильтр выключился от блипа"
    assert _total(OPT_OUT_COUNTER) == 0


def test_an_unreadable_do_not_contact_registry_is_loud(monkeypatch):
    from services import contact_opt_out, metrics

    monkeypatch.undo()
    metrics.reset()

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    pool = _Pool(fail_times=9)

    got = asyncio.run(contact_opt_out.load_opted_out(pool, 500))

    assert got == set(), "fail-open у реестра менять не планировали"
    assert _total(OPT_OUT_COUNTER) >= 1, (
        "реестр «не писать» выключился молча — человек получит сообщение, от "
        "которого отказался, и следов не останется")


# ── Сбой доходит до человека ────────────────────────────────────────────────

@pytest.mark.parametrize("counter,words", [
    (DEDUP_COUNTER, "журнал приглашений"),
    (OPT_OUT_COUNTER, "не писать"),
])
def test_the_failure_reaches_the_owner(monkeypatch, counter, words):
    from services import metrics, op_worker

    metrics.reset()
    metrics.inc(counter)

    sent: list[str] = []

    class _Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(str(text))

    class _P:
        async def execute(self, *a, **k):
            return "UPDATE 1"

        async def fetchrow(self, *a, **k):
            return None

        async def fetch(self, *a, **k):
            return []

    async def _true():
        return True

    monkeypatch.setattr(op_worker.db, "notify_dedup_ok",
                        lambda *a, **k: _true(), raising=False)
    import bot.utils.subscription as subs
    monkeypatch.setattr(subs, "_admin_ids", lambda: {777}, raising=False)

    asyncio.run(op_worker._watchdog_protective_failures(_P(), _Bot(), {}))

    assert sent, f"{counter}: сбой не дошёл ни до кого"
    assert words in sent[0], sent[0]


# ── Перепись дверей ─────────────────────────────────────────────────────────

DOORS = {
    ("services/invite_dedup.py", "already = set()"): DEDUP_COUNTER,
    ("services/contact_opt_out.py", "return set()"): OPT_OUT_COUNTER,
}


def test_both_doors_shout_before_falling_back_to_empty():
    """Дверь, которая падает в «пусто» молча, — это защита, которой нет."""
    silent = []
    for (rel, fallback), counter in sorted(DOORS.items()):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            src = fh.read()
        assert fallback in src, f"{rel}: ориентир «{fallback}» пропал"
        if counter not in src:
            silent.append(rel)
    assert not silent, (
        "эти двери при сбое чтения возвращают пустой набор и не увеличивают "
        f"счётчик: {silent}. Защита выключается, и узнать об этом негде")
