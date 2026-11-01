"""Пустой выбор целей — это отказ, а не «все».

Продолжение жалобы владельца «выбираю один бот, меняю описание — изменения
применяются ко всем ботам». Там пустой `bot_ids` значил «все боты». Тот же
шаблон стоял ещё в четырёх массовых ручках, и у трёх из них последствие видно
живым людям и необратимо:

* рассылка по сети ботов (`/network_broadcast`) — сообщение уходило аудиториям
  ВСЕХ ботов;
* публикация (`/mass_publish`) — пост уходил во ВСЕ каналы;
* клонирование контента (`/content_cloner/submit`) — пересылка во все каналы;
* пост от аккаунта (`/channels/bulk_post`) — во все каналы аккаунта.

Форма ошибки одна: `[int(x) for x in (body.get(key) or []) if …]`, затем
`if ids: <выбранные> else: <все>`. Пустой список и список из мусора (`["abc"]`)
одинаково становились пустым, а пустой — «всеми». Теперь «все» — это
ОТСУТСТВИЕ ключа (так экраны и шлют «во все»), а переданный пустой или битый
список отбивается 400 до постановки операции.

Плюс исполнитель профиля ботов: без выбора он больше не правит весь парк сам —
нужна явная отметка `all`, которую ставит ручка.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services.security import selection_ids

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── помощник ──────────────────────────────────────────────────────────────────

def test_absent_key_is_a_deliberate_all():
    assert selection_ids({}, "bot_ids") == (None, None)
    assert selection_ids({"bot_ids": None}, "bot_ids") == (None, None)


@pytest.mark.parametrize("raw", [[], ["abc"], [1, "x"], "1,2", {"a": 1}, [True]])
def test_a_sent_but_empty_or_broken_list_is_refused(raw):
    ids, err = selection_ids({"bot_ids": raw}, "bot_ids", what="бот")
    assert ids is None and err, f"{raw!r} прочитан как «все»"


def test_a_good_list_is_kept_in_order_without_repeats():
    assert selection_ids({"c": ["-1001", 5, 5, " 7 "]}, "c") == ([-1001, 5, 7], None)


def test_cap_is_a_refusal_not_a_silent_cut():
    ids, err = selection_ids({"c": list(range(6))}, "c", cap=5)
    assert ids is None and "5" in err


# ── ручки ─────────────────────────────────────────────────────────────────────

class _Pool:
    async def fetchval(self, *a, **k):
        return 3

    async def fetch(self, sql, *a, **k):
        if "managed_bots" in sql:
            return [{"bot_id": 1, "active_subs": 5}, {"bot_id": 2, "active_subs": 7}]
        if "FROM tg_accounts" in sql:
            return [{"id": 11}]
        if "managed_channels" in sql:
            return [{"username": "c1", "channel_id": -1001}]
        return []

    async def fetchrow(self, *a, **k):
        return None

    async def execute(self, *a, **k):
        return "OK"


def _post(monkeypatch, path, body):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api, operation_bus

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)
    submitted: list[tuple[str, dict]] = []

    async def _submit(pool, uid, op_type, params, **k):
        submitted.append((op_type, params))
        return 1

    monkeypatch.setattr(operation_bus, "submit", _submit)

    async def _go():
        app = web.Application()
        mini_app_api.setup_routes(app, _Pool())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post(path, json=body)
            return r.status, submitted
        finally:
            await client.close()

    return asyncio.run(_go())


_CASES = [
    ("/api/miniapp/network_broadcast", "bot_ids", {"text": "привет"}),
    ("/api/miniapp/mass_publish", "channel_ids", {"text": "пост"}),
    ("/api/miniapp/content_cloner/submit", "channel_ids", {"source": "@src"}),
    ("/api/miniapp/channels/bulk_post", "channel_ids", {"acc_id": 11, "text": "пост"}),
]


@pytest.mark.parametrize("path,key,base", _CASES)
@pytest.mark.parametrize("bad", [[], ["abc"]])
def test_empty_or_broken_selection_queues_nothing(monkeypatch, path, key, base, bad):
    status, sub = _post(monkeypatch, path, {**base, key: bad})
    assert status == 400 and sub == [], (
        f"{path}: выбор {bad!r} ушёл в операцию {sub!r} — то есть «во все»")


@pytest.mark.parametrize("path,key,base", _CASES)
def test_absent_selection_still_means_all(monkeypatch, path, key, base):
    # Контроль: «во все» по-прежнему работает — ровно так его шлют экраны.
    status, sub = _post(monkeypatch, path, base)
    assert status == 200 and len(sub) == 1, (path, status, sub)


def test_selected_bots_reach_the_operation(monkeypatch):
    status, sub = _post(monkeypatch, "/api/miniapp/network_broadcast",
                        {"text": "привет", "bot_ids": [1, 2]})
    assert status == 200 and sub[0][1].get("selected_bot_ids") == [1, 2]


def test_cloner_screen_sends_all_by_omitting_the_list():
    with open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8") as f:
        html = f.read()
    i = html.index("'/api/miniapp/content_cloner/submit'")
    near = html[i - 600:i + 200]
    assert "toAll ? [] : picked" not in near, "экран клонера снова шлёт [] как «во все»"
    assert "if (!toAll) _cl.channel_ids = picked" in near


# ── исполнитель профиля ботов ─────────────────────────────────────────────────

def test_profile_executor_without_selection_edits_nobody(monkeypatch):
    from services import op_worker
    from tests.test_bulk_bot_profile import _Pool as _BPool, _patch_common

    bots = [{"id": 3, "bot_id": 111, "token": "T1", "username": "a"},
            {"id": 4, "bot_id": 222, "token": "T2", "username": "b"}]
    calls = _patch_common(monkeypatch, bots, {"ok": True})
    res = asyncio.run(op_worker._exec_bulk_bot_profile(
        _BPool(), None, 1, 99, {"description": "новое"}))
    assert res["status"] == "failed"
    assert not [m for _, m, _ in calls if m.startswith("set")], "правка ушла на весь парк"


def test_profile_executor_edits_all_only_when_marked(monkeypatch):
    from services import op_worker
    from tests.test_bulk_bot_profile import _Pool as _BPool, _patch_common

    bots = [{"id": 3, "bot_id": 111, "token": "T1", "username": "a"},
            {"id": 4, "bot_id": 222, "token": "T2", "username": "b"}]
    _patch_common(monkeypatch, bots, {"ok": True})
    res = asyncio.run(op_worker._exec_bulk_bot_profile(
        _BPool(), None, 1, 99, {"description": "новое", "all": True}))
    assert res["ok"] == 2


def test_publish_executor_does_not_read_an_empty_list_as_all():
    from services import op_worker

    res = asyncio.run(op_worker._exec_mass_publish(
        _Pool(), None, 1, 99, {"text": "пост", "channel_ids": []}))
    assert res["status"] == "failed" and "Не выбран ни один канал" in res["summary"], (
        f"пустой список каналов пошёл публиковаться во все: {res!r}")


# ── храповик: шаблон «пусто ⇒ все» не возвращается ───────────────────────────

import re  # noqa: E402

_BAD = re.compile(
    r'\b(?:body|data|tgt|payload)\.get\(\s*["\'](?:bot_ids|channel_ids)["\']\s*\)\s*or\s*\[\]')


def _offenders() -> list[str]:
    out = []
    sdir = os.path.join(ROOT, "services")
    for name in sorted(os.listdir(sdir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(sdir, name), encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if _BAD.search(line):
                    out.append(f"services/{name}:{n}: {line.strip()}")
    return out


def test_no_handler_reads_a_target_list_with_or_empty():
    bad = _offenders()
    assert not bad, (
        "Выбор целей читается как `.get(key) or []`: пустой или битый список "
        "становится пустым, а пустой — «всеми». Читайте через "
        "security.selection_ids:\n" + "\n".join(bad))


def test_the_ratchet_sees_the_old_form():
    assert _BAD.search('ids = [int(x) for x in (body.get("channel_ids") or []) if x]')
    assert _BAD.search("sel = data.get('bot_ids') or []")
    assert not _BAD.search('ids, err = selection_ids(body, "channel_ids")')
