"""Вкладка «Языки бота» модуля «Гео-сеть».

Что держим:
* один список языков — экран, сервер и бот видят одни и те же (раньше профиль
  на польском/турецком, заданный в боте, мини-апп не показывал вовсе);
* сброс языка и сброс отдельного поля реально уходят в Telegram пустой строкой
  (раньше пустое поле молча пропускалось — убрать описание было нельзя);
* перевод ничего не пишет в Telegram, а сбой одного языка не валит остальные;
* перенос на другие боты идёт операцией bulk_bot_profile с кодом языка, и
  снимок/откат относятся к ТОМУ ЖЕ языку, а не к профилю по умолчанию.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from services import bot_api, bot_localization as bl
from tests.miniapp_source import miniapp_html, miniapp_source

ROOT = Path(__file__).resolve().parents[1]


# ── Список языков ──────────────────────────────────────────────────────────

def test_catalog_covers_every_language_the_bot_offers():
    from bot.keyboards import LANGUAGES as BOT_LANGS
    missing = [c for c, _, _ in BOT_LANGS if c not in bl.LANG_CODES]
    assert not missing, f"бот предлагает языки, которых не видит мини-апп: {missing}"


def test_catalog_has_no_duplicates_and_russian_names():
    assert len(bl.LANG_CODES) == len(set(bl.LANG_CODES))
    for code, _flag, name in bl.LANGUAGES:
        assert re.fullmatch(r"[a-z]{2}", code)
        assert re.search(r"[А-Яа-яЁё]", name), f"название языка не по-русски: {name}"
    for g in bl.LANGUAGE_GROUPS:
        assert set(g["codes"]) <= set(bl.LANG_CODES), g["key"]


def test_miniapp_screen_keeps_no_own_language_list():
    html = miniapp_html()
    start = html.index('<div class="screen" id="s-multigeo">')
    end = html.index("CONTENT CLONER", start)
    block = html[start:end]
    assert 'value="de"' not in block and "Deutsch" not in block, \
        "экран снова держит свой список языков — он разойдётся с сервером"
    js = html[html.index("// ── Multigeo"):html.index("// ── Content Cloner")]
    assert "MG_DATA.languages" in js


def test_normalize_lang():
    assert bl.normalize_lang("default") == ""
    assert bl.normalize_lang("") == ""
    assert bl.normalize_lang(None) == ""
    assert bl.normalize_lang("DE") == "de"
    assert bl.normalize_lang("vi") == "vi", "язык вне списка Telegram понимает — не теряем"
    assert bl.normalize_lang("ru-RU") is None
    assert bl.normalize_lang("../x") is None


def test_clean_fields_distinguishes_absent_from_empty():
    assert bl.clean_fields({"name": "Бот"}) == {"name": "Бот"}
    got = bl.clean_fields({"name": "", "description": "x" * 600})
    assert got == {"name": "", "description": "x" * 512}


# ── Запись ─────────────────────────────────────────────────────────────────

class _Calls:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}

    async def __call__(self, session, token, method, **params):
        self.calls.append((method, params))
        if method in self.fail:
            return {"ok": False, "error_code": 400, "description": self.fail[method]}
        return {"ok": True, "result": True}


async def test_reset_sends_empty_strings_with_language():
    fake = _Calls()
    with patch.object(bot_api, "_call", fake):
        res = await bl.apply_profile(None, "t", "de", {f: "" for f in bl.FIELDS})
    assert res["ok"]
    assert sorted(m for m, _ in fake.calls) == sorted(
        ["setMyName", "setMyDescription", "setMyShortDescription"])
    for _m, params in fake.calls:
        assert params["language_code"] == "de"
        assert set(params.values()) == {"", "de"}


async def test_default_name_cannot_be_emptied():
    fake = _Calls()
    with patch.object(bot_api, "_call", fake):
        res = await bl.apply_profile(None, "t", "", {"name": "", "description": ""})
    assert not res["ok"] and "name" in res["errors"]
    assert [m for m, _ in fake.calls] == ["setMyDescription"], \
        "имя по умолчанию не должно стираться, описание — должно"


async def test_untouched_fields_are_not_sent_and_reason_is_russian():
    fake = _Calls(fail={"setMyDescription": "Bad Request: description is too long"})
    with patch.object(bot_api, "_call", fake):
        res = await bl.apply_profile(None, "t", "fr", {"description": "x"})
    assert [m for m, _ in fake.calls] == ["setMyDescription"]
    assert res["errors"]["description"] == "текст длиннее, чем разрешает Telegram"


# ── Перевод ────────────────────────────────────────────────────────────────

async def test_translate_writes_nothing_and_survives_one_bad_language():
    from services import ai_gateway

    async def fake_complete(prompt, **kw):
        if "Целевой язык: de" in prompt:
            return "не JSON"
        assert "name" not in json.loads(prompt.split("(JSON):\n", 1)[1]), \
            "имя по умолчанию не переводится"
        return '```json\n{"description": "Desc", "short_description": "Short"}\n```'

    async def no_tg(*a, **k):  # pragma: no cover - не должен вызываться
        raise AssertionError("перевод не должен писать в Telegram")

    with patch.object(ai_gateway, "complete", fake_complete), \
         patch.object(bot_api, "_call", no_tg):
        res = await bl.translate_profile(
            {"name": "Бренд", "description": "Описание", "short_description": "Кратко"},
            ["en", "de", "fr"])
    assert list(res["drafts"]) == ["en", "fr"]
    assert res["failed"] == ["de"]
    assert res["drafts"]["en"] == {"name": "Бренд", "description": "Desc",
                                   "short_description": "Short"}


async def test_translate_trims_to_telegram_limits():
    from services import ai_gateway

    async def fake_complete(prompt, **kw):
        return json.dumps({"short_description": "y" * 500})

    with patch.object(ai_gateway, "complete", fake_complete):
        res = await bl.translate_profile({"short_description": "к"}, ["en"])
    assert len(res["drafts"]["en"]["short_description"]) == 120


# ── Перенос на другие боты: операция с языком ──────────────────────────────

def test_bulk_bot_profile_carries_language_into_set_and_snapshot():
    from services import op_worker
    src = inspect.getsource(op_worker._exec_bulk_bot_profile)
    assert 'params.get("language_code")' in src
    assert "**_lc_kw" in src, "запись поля обязана нести язык"
    for getter in ("get_my_name", "get_my_description", "get_my_short_description"):
        assert re.search(getter + r"\(\s*sess, b\[\"token\"\], language_code\)", src), \
            f"снимок «как было» читает {getter} не того языка"


def test_language_snapshot_rolls_back_the_same_language():
    """Снимок профиля языка пишется ключами «name:de»: общий откат вернёт
    именно немецкий профиль, а не перезапишет профиль по умолчанию."""
    from services.clone_adapt_engine import restore_calls
    calls = restore_calls({"name:de": "Alt", "description:de": ""})
    assert {m for _l, m, _p in calls} == {"setMyName", "setMyDescription"}
    assert all(p.get("language_code") == "de" for _l, _m, p in calls)


class _Pool:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"

    async def fetchval(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []

    async def fetchrow(self, *a, **k):
        return None


async def test_bulk_bot_profile_with_language_sets_that_language():
    from services import op_worker
    import database.db as dbmod

    calls = []

    async def fake_call(session, token, method, **params):
        calls.append((method, params))
        return {"ok": True, "result": True}

    async def fake_get(session, token, language_code=""):
        calls.append(("get", {"language_code": language_code}))
        return "было"

    async def fake_fetch_bots(pool, sql, *args):
        return [{"id": 1, "bot_id": 101, "token": "t", "username": "b1"}]

    async def no_done(*a, **k):
        return set()

    async def nothing(*a, **k):
        return None

    async def not_cancelled(*a, **k):
        return False

    async def no_sleep(*a, **k):
        return None

    with patch.object(bot_api, "_call", fake_call), \
         patch.object(bot_api, "get_my_name", fake_get), \
         patch.object(bot_api, "get_my_description", fake_get), \
         patch.object(bot_api, "get_my_short_description", fake_get), \
         patch.object(dbmod, "fetch_bots", fake_fetch_bots), \
         patch.object(op_worker, "completed_targets", no_done), \
         patch.object(op_worker, "_is_cancelled", not_cancelled), \
         patch.object(op_worker, "_mark_progress", nothing), \
         patch.object(op_worker, "_journal_done", nothing), \
         patch.object(op_worker, "_safe_execute", nothing), \
         patch.object(op_worker.asyncio, "sleep", no_sleep):
        pool = _Pool()
        res = await op_worker._exec_bulk_bot_profile(
            pool, None, 1, 7, {"name": "Name", "description": "Desc",
                               "language_code": "de", "bot_ids": [101],
                               "greeting": "не должно уйти"})
    assert res["status"] == "done" and res["ok"] == 1, res
    sets = [(m, p) for m, p in calls if m.startswith("setMy")]
    assert {m for m, _ in sets} == {"setMyName", "setMyDescription"}
    assert all(p.get("language_code") == "de" for _, p in sets)
    assert all(p["language_code"] == "de" for m, p in calls if m == "get")
    snap = [a for sql, a in pool.executed if "bulk_edit_snapshots" in sql]
    before = json.loads(snap[0][3]) if snap else {}
    assert set(before) == {"name:de", "description:de"}, before
    assert not any("auto_replies" in sql for sql, _ in pool.executed), \
        "приветствие — правило без языка, при переносе языка его не трогаем"
    assert "Немецкий" in res["summary"]


async def test_bulk_bot_profile_rejects_bad_language_code():
    from services import op_worker
    res = await op_worker._exec_bulk_bot_profile(
        _Pool(), None, 1, 7, {"name": "x", "language_code": "../", "bot_ids": [1]})
    assert res["status"] == "failed"


@pytest.mark.parametrize("route", [
    "/api/miniapp/bot/{bot_id}/multigeo/{lang}",
    "/api/miniapp/bot/{bot_id}/multigeo/translate",
    "/api/miniapp/bot/{bot_id}/multigeo/apply",
    "/api/miniapp/bot/{bot_id}/multigeo/copy",
])
def test_routes_are_registered_and_called_by_the_screen(route):
    src = (ROOT / "services" / "mini_app_bots.py").read_text(encoding="utf-8")
    assert route in src
    html = miniapp_source()
    tail = route.rsplit("/", 1)[1]
    if tail != "{lang}":
        assert f"/multigeo/{tail}`" in html, f"экран не зовёт {route}"
    else:
        assert "/multigeo/${encodeURIComponent(code)}`" in html


def test_copy_goes_through_operation_bus_not_direct_writes():
    from services import mini_app_bots
    src = inspect.getsource(mini_app_bots)
    body = src.split("async def multigeo_copy", 1)[1].split("\n    async def ", 1)[0]
    body = body.split("\n    app.router", 1)[0]
    assert '_obus.submit(' in body and '"bulk_bot_profile"' in body
    assert "setMy" not in body and "apply_profile" not in body, \
        "перенос на чужие боты обязан идти операцией с журналом и откатом"
