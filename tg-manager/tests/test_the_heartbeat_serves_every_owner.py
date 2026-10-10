"""Сердцебиение организма жило не для всех и считало по остывшим фактам.

ПЕРВОЕ. Такт организма (`organism.runner._tick`) делает три разные работы:
нуджи мозга, каскады виртуального слоя и дозор роста. А список владельцев брал
ровно из `tg_accounts`. Владелец, который работает только ботами и контактами
и TG-аккаунтов не импортировал, в этот список не попадал ВООБЩЕ: его воронка
не остывала каскадом, «ходят по кругу» не находилось, аномалии роста не
замечались, проактивный нудж не приходил никогда. Для него организма просто
не существовало, и узнать об этом было неоткуда — ошибок в логах нет, работа
молча не делается.

ВТОРОЕ. Распад состояний идёт на каждом сердцебиении (раз в 15 минут) — его
специально вынесли из редкого прохода, потому что «готов купить» живёт сутки
и шести часов задержки хватало, чтобы экран считал по неверным состояниям. Но
ВЕРДИКТ каскада («аудитория горячая», «бот горячий») пересчитывался как раз
редким проходом — раз в шесть часов. Он лежит записанным в базе, и читают его
экран сводки, подсказки мозга и выбор «самого горячего». То есть распад уже
остудил людей, а продукт до шести часов показывал «горячо» ровно там, где по
этому вердикту принимают решение.

Теперь распад говорит, У КОГО остыло, и каскад таким владельцам
пересчитывается в том же такте.
"""
from __future__ import annotations

import asyncio

from services import virtual_layer
from services.organism import runner


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Кого обслуживает такт ──────────────────────────────────────────────────

class _OwnersPool:
    """Пул, отвечающий на запрос владельцев и запоминающий SQL."""

    def __init__(self, rows, fail_union: bool = False):
        self.rows = rows
        self.fail_union = fail_union
        self.queries: list[str] = []

    async def fetch(self, sql, *a):
        self.queries.append(sql)
        if self.fail_union and "UNION" in sql:
            raise RuntimeError("старая схема: таблицы нет")
        if "UNION" in sql:
            return [{"owner_id": o} for o in self.rows]
        return [{"owner_id": o} for o in self.rows if o < 100]


def test_the_tick_asks_about_bots_and_contacts_too():
    pool = _OwnersPool([1, 2])
    _run(runner._active_owners(pool))
    sql = pool.queries[0]
    assert "tg_accounts" in sql
    assert "managed_bots" in sql, (
        "владелец без TG-аккаунтов снова не получит ни каскада, ни нуджа")
    assert "virtual_states" in sql, (
        "владелец, у которого есть только состояния слоя, снова невидим")


def test_an_owner_without_tg_accounts_is_served():
    """Владелец 500 есть только в объединении — он обязан попасть в такт."""
    pool = _OwnersPool([7, 500])
    assert _run(runner._active_owners(pool)) == [7, 500]


def test_an_old_schema_falls_back_to_the_previous_list_not_to_silence():
    """Нет таблицы — обслуживаем хотя бы тех, кого обслуживали раньше."""
    pool = _OwnersPool([7, 500], fail_union=True)
    assert _run(runner._active_owners(pool)) == [7]
    assert len(pool.queries) == 2


# ── Распад сообщает, у кого остыло ─────────────────────────────────────────

class _DecayPool:
    """Пул со строками состояний: читает одну пачку, запись глотает."""

    def __init__(self, rows):
        self.rows = rows
        self.written: list[tuple] = []

    async def fetch(self, sql, *a):
        if "virtual_states" in sql and "expires_at" in sql:
            rows, self.rows = self.rows, []
            return rows
        return []

    async def execute(self, sql, *a):
        self.written.append((sql, a))
        return "OK"

    async def fetchrow(self, sql, *a):
        return None


def _expired(owner_id: int, entity_id: str, value: str = "ready"):
    from datetime import datetime, timedelta, timezone
    return {"owner_id": owner_id, "entity_type": virtual_layer.USER,
            "entity_id": entity_id, "state_key": "funnel", "value": value,
            "confidence": 0.8,
            "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "source": "bot_1"}


def test_decay_reports_whose_states_cooled():
    pool = _DecayPool([_expired(11, "a"), _expired(22, "b")])
    owners: set[int] = set()
    cooled = _run(virtual_layer.run_decay(pool, owners_out=owners))
    assert cooled == 2
    assert owners == {11, 22}


def test_decay_reports_nobody_when_nothing_cooled():
    """Самопроверка: пустое окно не выдумывает владельцев."""
    owners: set[int] = set()
    assert _run(virtual_layer.run_decay(_DecayPool([]), owners_out=owners)) == 0
    assert owners == set()


def test_decay_still_works_without_the_new_argument():
    """Прежний контракт цел: вызывающие, которым владельцы не нужны, целы."""
    assert _run(virtual_layer.run_decay(_DecayPool([_expired(33, "c")]))) == 1


# ── Каскад пересчитывается тем, у кого остыло ──────────────────────────────

def test_the_tick_recomputes_the_cascade_for_whoever_cooled(monkeypatch):
    """Не дожидаясь редкого прохода — иначе вердикт врёт до шести часов."""
    recomputed: list[int] = []

    async def _decay(pool, owner_id=None, *, owners_out=None, **kw):
        if owners_out is not None:
            owners_out.add(42)
        return 1

    async def _cascade(pool, oid, *a, **kw):
        recomputed.append(int(oid))
        return "warm"

    async def _owners(pool):
        return [42, 43]

    async def _tick_owner(pool, bot, oid, **kw):
        return False

    monkeypatch.setattr(virtual_layer, "run_decay", _decay)
    monkeypatch.setattr(virtual_layer, "recompute_cascade", _cascade)
    monkeypatch.setattr(virtual_layer, "recompute_bot_cascade",
                        lambda pool, oid, **kw: _cascade(pool, oid))
    monkeypatch.setattr(runner, "_active_owners", _owners)
    monkeypatch.setattr(runner, "_tick_owner", _tick_owner)

    # Время держим в руках: иначе два такта подряд укладываются в одну секунду
    # и оба считаются редким проходом — тест проверял бы не то.
    clock = {"t": 1_000_000.0}

    class _Clock:
        @staticmethod
        def time():
            return clock["t"]

    monkeypatch.setattr(runner, "time", _Clock)
    # Редкий проход только что был: сам по себе он каскад сегодня не запустит.
    monkeypatch.setattr(runner, "_last_prune", 0.0)
    _run(runner._tick(_DecayPool([]), None))
    first_pass = list(recomputed)

    recomputed.clear()
    clock["t"] += 15 * 60          # следующее сердцебиение, редкого прохода нет
    _run(runner._tick(_DecayPool([]), None))
    assert 42 in recomputed, (
        "у владельца остыли состояния, а вердикт каскада остался прежним")
    assert 43 not in recomputed, (
        "каскад пересчитали тому, у кого ничего не менялось — это лишняя работа "
        "на каждом сердцебиении")
    assert first_pass, "подготовка: первый проход обязан быть редким"
