"""Приглашение помечается ДО отправки, а не после.

РАЗРЫВ. Дедуп инвайта («кого уже звали в этот канал») писался ПОСЛЕ ответа
Telegram: сначала пачка целей уходила в сеть, и только когда движок возвращал
результат, цели ложились в `invite_target_log`. Между этими двумя моментами
проходят не миллисекунды: движок зовёт цели по одной с паузами 2–4 секунды
против банов, по номерам — дольше, то есть пятёрка занимает 10–20 секунд, а
широкий флот держит в воздухе по пачке на аккаунт.

SIGTERM приходит в это окно штатно: ветка едет на Railway, пуш в неё означает
прод, и каждый деплой обрывает идущие операции. Приглашённые в этот момент люди
в журнал не попадали, и следующий прогон звал их ВТОРОЙ раз — на самой
баноопасной операции продукта. Счёт за это приходит PEER_FLOOD'ом и банами
аккаунтов, а не сообщением об ошибке.

ФИКС. Движок зовёт крючок `on_attempt` прямо перед необратимым действием —
приглашением, промоут-трюком, ссылкой в ЛС, — и исполнитель метит цель в
журнале ДО него. С тех, кого Telegram так и не рассмотрел (флуд, таймаут,
приватность, «номера нет в Telegram»), метка снимается по ответу. Цена обрыва
развёрнута в другую сторону: до одной цели на аккаунт может остаться
помеченной, не получив приглашения. Для инвайта это дешевле повторного
приглашения, и это тот же выбор, что у остальных необратимых действий продукта.

ПОЧЕМУ ПО ОДНОЙ ЦЕЛИ, А НЕ ПАЧКОЙ. Пометка всей пачки перед сетью закрывала то
же окно, но в риске оказывалась вся пачка каждого аккаунта: на флоте из
четырёх аккаунтов и аудитории из двадцати человек обрыв пометил бы всех
двадцать, и повтор не позвал бы НИКОГО. Поимённая пометка держит в риске ровно
то, что действительно в полёте.
"""
from __future__ import annotations

import ast
import asyncio
import glob
import inspect
import os
import pathlib
import re

import pytest

from services import mass_inviter_engine as mie

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Ent:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.phone = None


class _Res:
    missing_invitees: list = []


class _Client:
    """Клиент-стенд: записывает порядок «пометили → позвали»."""

    def __init__(self, trace: list, die_on: int | None = None):
        self.trace = trace
        self.die_on = die_on
        self.calls = 0

    async def get_entity(self, ref):
        return _Ent(abs(hash(str(ref))) % 100000)

    async def __call__(self, request):
        self.calls += 1
        self.trace.append(("request", self.calls))
        if self.die_on is not None and self.calls >= self.die_on:
            # Именно BaseException: так приходит снятие процесса и отмена —
            # пути, на которых запись ПОСЛЕ действия не выполняется вовсе.
            raise asyncio.CancelledError()
        return _Res()

    async def disconnect(self):
        return None

    def is_connected(self):
        return True


# ОДИН цикл на весь модуль: соединения asyncpg привязаны к тому циклу, в котором
# создан пул, и новый цикл на каждый вызов даёт «Event loop is closed» на втором
# же обращении к базе. С заглушками этого не видно вовсе.
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture
def engine(monkeypatch):
    """Движок без сети и без пауз: клиент-стенд и мгновенный sleep."""
    from services import account_manager

    trace: list = []
    holder: dict = {}

    def _install(die_on=None):
        client = _Client(trace, die_on=die_on)
        holder["client"] = client

        async def _connect(*_a, **_k):
            return client

        async def _resolve(*_a, **_k):
            return object()

        async def _sleep(*_a, **_k):
            return None

        monkeypatch.setattr(account_manager, "connect_client", _connect)
        monkeypatch.setattr(mie, "_resolve_group_entity", _resolve)
        monkeypatch.setattr(mie.asyncio, "sleep", _sleep)
        return client

    _install.trace = trace          # type: ignore[attr-defined]
    _install.holder = holder        # type: ignore[attr-defined]
    return _install


def _marker(trace: list):
    async def _on_attempt(refs):
        for r in refs:
            trace.append(("mark", str(r)))
    return _on_attempt


# ── Поимённый путь: метка идёт раньше запроса ───────────────────────────────

def test_the_target_is_marked_before_the_invite_goes_out(engine):
    client = engine()
    trace = engine.trace
    _run(mie.invite_batch("s", {"id": 1}, "@g", ["u1", "u2"], 1.0,
                          bulk=False, on_attempt=_marker(trace)))
    assert client.calls == 2, "стенд не получил двух приглашений"
    # Для КАЖДОГО запроса метка этой цели уже была.
    assert trace == [("mark", "u1"), ("request", 1),
                     ("mark", "u2"), ("request", 2)], trace


def test_a_break_right_after_the_invite_leaves_the_target_marked(engine):
    """Обрыв на второй цели: она уже позвана и обязана остаться помеченной."""
    engine(die_on=2)
    trace = engine.trace
    with pytest.raises(asyncio.CancelledError):
        _run(mie.invite_batch("s", {"id": 1}, "@g", ["u1", "u2", "u3"], 1.0,
                              bulk=False, on_attempt=_marker(trace)))
    marked = [r for kind, r in trace if kind == "mark"]
    assert marked == ["u1", "u2"], (
        "второй цели нет в пометках — её позвали, а записи нет: следующий "
        f"прогон позовёт её второй раз ({trace})")
    assert "u3" not in marked, "третью цель не звали — метить её нельзя"


def test_the_whole_bulk_request_is_marked_before_sending(engine):
    """Пакетный путь: один запрос на всех — значит в риске все разом."""
    client = engine(die_on=1)
    trace = engine.trace
    with pytest.raises(asyncio.CancelledError):
        _run(mie._try_bulk_invite(client, object(), ["u1", "u2"], lambda *_a: None,
                                  on_attempt=_marker(trace)))
    assert [r for kind, r in trace if kind == "mark"] == ["u1", "u2"], trace


def test_the_promote_trick_marks_before_it_adds(engine):
    """Промоут-трюк добавляет человека в чат — то же необратимое действие.

    На цель здесь уходит ДВА запроса: выдать админку (человек уже в чате) и
    снять её. Обрываем на втором выданном праве — то есть на второй цели.
    """
    engine(die_on=3)
    trace = engine.trace
    with pytest.raises(asyncio.CancelledError):
        _run(mie.add_via_promote("s", {"id": 1}, "@g", ["u1", "u2", "u3"],
                                 on_attempt=_marker(trace)))
    marked = [r for kind, r in trace if kind == "mark"]
    assert marked[:2] == ["u1", "u2"] and "u3" not in marked, trace


def test_the_link_invite_marks_before_the_message_leaves(engine, monkeypatch):
    """«Ссылка в ЛС» — тоже один раз на человека: второе письмо это спам."""
    from services import account_manager

    trace = engine.trace
    sent: list = []

    async def _send_dm(_sess, ref, _text, **_k):
        sent.append(str(ref))
        if len(sent) >= 2:
            raise asyncio.CancelledError()
        return {"ok": True}

    monkeypatch.setattr(account_manager, "send_dm", _send_dm)
    with pytest.raises(asyncio.CancelledError):
        _run(mie.invite_via_link_batch("s", {"id": 1}, "https://t.me/+x",
                                       ["u1", "u2", "u3"], "привет {link}", 1.0,
                                       on_attempt=_marker(trace)))
    marked = [r for kind, r in trace if kind == "mark"]
    assert marked == ["u1", "u2"] and sent == ["u1", "u2"], trace


# ── Исполнитель: метка и её снятие по живой базе ────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        conn = await asyncpg.connect(DSN)
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    p = _run(_mk())
    yield p
    _run(p.close())


OWNER = 994411
GROUP = "@premark_test"


def _clean(pool):
    _run(pool.execute("DELETE FROM invite_target_log WHERE owner_id=$1", OWNER))


def test_only_the_untried_marks_are_lifted(pool):
    """Решённые цели остаются помеченными, нерассмотренные — освобождаются."""
    from services import op_worker as ow

    _clean(pool)
    _run(ow._premark_invite_targets(pool, OWNER, GROUP, 101, ["u1", "u2", "u3"]))
    assert _run(_targets(pool)) == {"u1", "u2", "u3"}, "предзапись не легла"

    # Telegram ответил только про u1 (добавлен); u2 поймал флуд, u3 не пробовали.
    _run(ow._unmark_untried_invites(pool, OWNER, GROUP, 101,
                                    ["u1", "u2", "u3"], ["u1"]))
    assert _run(_targets(pool)) == {"u1"}, (
        "с нерассмотренных метка не снята — человек, на котором моргнула сеть, "
        "больше никогда не позовётся")


def test_a_break_between_the_invite_and_the_answer_keeps_the_mark(pool):
    """Обрыв = снятия не было: цель остаётся помеченной и повтор её не позовёт."""
    from services import invite_dedup as idd
    from services import op_worker as ow

    _clean(pool)
    _run(ow._premark_invite_targets(pool, OWNER, GROUP, 102, ["u7"]))
    # Снятие НЕ вызываем — именно это делает с процессом SIGTERM.
    _keep, _already, _opt = _run(idd.filter_new(pool, OWNER, [GROUP], ["u7", "u8"]))
    assert [str(k) for k in _keep] == ["u8"], (
        "повтор снова зовёт u7, хотя приглашение могло уйти: "
        f"осталось {_keep}")
    assert _already == 1


def test_someone_elses_mark_is_never_lifted(pool):
    """Снимаем только свои метки: чужую строку удалить — это второй инвайт."""
    from services import op_worker as ow

    _clean(pool)
    _run(ow._premark_invite_targets(pool, OWNER, GROUP, 201, ["u9"]))
    # Другая операция по той же цели: её строка уже стоит, наша предзапись
    # (ON CONFLICT DO NOTHING) её не переписывает.
    _run(ow._premark_invite_targets(pool, OWNER, GROUP, 202, ["u9"]))
    _run(ow._unmark_untried_invites(pool, OWNER, GROUP, 202, ["u9"], []))
    assert _run(_targets(pool)) == {"u9"}, (
        "снятие удалило строку ЧУЖОЙ операции — её цель получит второе "
        "приглашение")


async def _targets(pool) -> set:
    rows = await pool.fetch(
        "SELECT target FROM invite_target_log WHERE owner_id=$1", OWNER)
    return {r["target"] for r in rows}


# ── Сторож проводки: исполнитель обязан передавать крючок ───────────────────

_ENGINE_CALLS = ("invite_batch", "invite_by_phones", "add_via_promote",
                 "invite_via_link_batch")


def _calls_without_hook(src: str) -> list[str]:
    """Вызовы движка инвайта из исполнителя, где крючок не передан."""
    bad = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not isinstance(f, ast.Attribute) or f.attr not in _ENGINE_CALLS:
            continue
        if not any(k.arg == "on_attempt" for k in node.keywords):
            bad.append(f"{f.attr} (строка {node.lineno})")
    return bad


def test_the_executor_passes_the_hook_everywhere():
    src = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    bad = _calls_without_hook(src)
    assert not bad, (
        "движок позван без on_attempt: " + ", ".join(bad) + ". Эти цели будут "
        "помечены только ПОСЛЕ ответа Telegram, и обрыв между приглашением и "
        "записью позовёт людей второй раз.")


def test_every_engine_entry_point_takes_the_hook():
    for name in _ENGINE_CALLS:
        sig = inspect.signature(getattr(mie, name))
        assert "on_attempt" in sig.parameters, (
            f"{name} не принимает on_attempt — исполнителю нечем пометить цель "
            "до действия")


def test_the_detector_notices_a_call_without_the_hook():
    assert _calls_without_hook("await inv.invite_batch(a, b, c, d)") == [
        "invite_batch (строка 1)"]
    assert _calls_without_hook("await inv.invite_batch(a, on_attempt=f)") == []
