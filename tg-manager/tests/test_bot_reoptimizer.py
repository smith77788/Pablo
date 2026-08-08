"""Фича (роадмап P2): авто-переоптимизация БОТОВ при падении позиции в поиске —
замыкает петлю анализ→рекомендация→применение для стороны ботов (у каналов она
уже есть: seo_ai_suggestions + /seo/apply).

Ранжирование ботов в Telegram-поиске идёт по токенам имени/краткого описания.
При падении позиции ниже порога (ranking_checker._check_visibility_alerts,
opt-in auto_reoptimize) генерируется рекомендация «имя/описание с просевшим
ключом», сохраняется в bot_seo_suggestions и применяется оператором в один клик
(/api/miniapp/seo/apply_bot). Фонового авто-переименования НЕТ.

Тест: (1) чистая логика build_bot_seo_suggestion; (2) generate_and_store owner-
scope + no-op guard; (3) apply_bot_seo owner-scope + правильные методы Bot API;
(4) проводка эндпоинтов/маршрутов; (5) хук в ranking_checker.
"""
from __future__ import annotations

import inspect
import re

import pytest

from services import bot_reoptimizer as br


# ── (1) чистая логика ──────────────────────────────────────────────────────

def test_dropped_keyword_forced_into_name():
    s = br.build_bot_seo_suggestion("Крутой Бот", None, "крипта", ["новости"])
    assert "крипта" in s["name"].lower(), "просевший ключ обязан попасть в имя"
    assert s["changed_name"] is True


def test_name_unchanged_when_keyword_already_present():
    s = br.build_bot_seo_suggestion("Крипта Новости", None, "крипта", [])
    assert s["changed_name"] is False, "если ключ уже в имени — имя не трогаем"


def test_name_respects_64_char_limit():
    long_name = "Б" * 60
    s = br.build_bot_seo_suggestion(long_name, None, "инвестиции", [])
    assert len(s["name"]) <= br.BOT_NAME_MAX


def test_short_desc_dedups_and_caps():
    s = br.build_bot_seo_suggestion(
        "Bot", None, "крипта",
        ["крипта", "новости", "аналитика", "трейдинг", "сигналы"],
    )
    assert len(s["short_desc"]) <= br.BOT_SHORT_DESC_MAX
    # дубль "крипта" не должен встречаться дважды
    assert s["short_desc"].lower().count("крипта") == 1


def test_noop_when_nothing_to_change():
    # ключ уже в имени, описание пустое и ключ туда попадёт → desc меняется.
    # Проверяем истинный no-op: ключ и в имени, и в описании.
    s = br.build_bot_seo_suggestion("крипта", "крипта", "крипта", [])
    assert s["changed_name"] is False and s["changed_desc"] is False


# ── фейковый пул ────────────────────────────────────────────────────────────

class _FakePool:
    def __init__(self, bot_row=None, keywords=None, sugg_row=None):
        self._bot_row = bot_row
        self._keywords = keywords or []
        self._sugg_row = sugg_row
        self.executed: list[tuple] = []

    async def fetchrow(self, q, *a):
        if "bot_seo_suggestions" in q:
            return self._sugg_row
        return self._bot_row

    async def fetch(self, q, *a):
        if "search_memory" in q:
            return [{"keyword": k} for k in self._keywords]
        return []

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "OK"


# ── (2) generate_and_store ─────────────────────────────────────────────────

async def test_generate_and_store_owner_scope_and_persist(monkeypatch):
    pool = _FakePool(
        bot_row={"bot_id": 5, "first_name": "Мой Бот", "username": "mybot"},
        keywords=["крипта", "новости"],
    )

    async def _fake_fetchrow_bot(p, q, *a):
        assert "added_by=$2" in q, "owner-scope через managed_bots.added_by"
        return pool._bot_row

    monkeypatch.setattr("database.db.fetchrow_bot", _fake_fetchrow_bot)
    res = await br.generate_and_store(pool, owner_id=42, bot_id=5, dropped_keyword="крипта")
    assert res is not None and "крипта" in res["name"].lower()
    assert any("INSERT INTO bot_seo_suggestions" in q for q, _ in pool.executed), (
        "рекомендация должна сохраняться в bot_seo_suggestions"
    )


async def test_generate_and_store_returns_none_for_missing_bot(monkeypatch):
    pool = _FakePool(bot_row=None)

    async def _none(p, q, *a):
        return None

    monkeypatch.setattr("database.db.fetchrow_bot", _none)
    res = await br.generate_and_store(pool, 42, 999, "крипта")
    assert res is None, "чужой/несуществующий бот → None (owner-scope)"


# ── (3) apply_bot_seo ──────────────────────────────────────────────────────

async def test_apply_bot_seo_calls_correct_api_methods(monkeypatch):
    pool = _FakePool(sugg_row={"name": "Мой Бот | Крипта", "short_desc": "крипта, новости"})
    calls: list[tuple] = []

    async def _fake_fetchrow_bot(p, q, *a):
        assert "added_by=$2" in q, "apply owner-scope"
        return {"token": "PLAINTOKEN"}

    async def _fake_api(token, method, payload):
        calls.append((method, payload))
        return {"ok": True}

    monkeypatch.setattr("database.db.fetchrow_bot", _fake_fetchrow_bot)
    monkeypatch.setattr(br, "_bot_api_call", _fake_api)
    res = await br.apply_bot_seo(pool, owner_id=42, bot_id=5)
    assert res["ok"] is True
    methods = {m for m, _ in calls}
    assert methods == {"setMyName", "setMyShortDescription"}, (
        "применение должно использовать те же методы Bot API, что и bulk_bot_edit"
    )
    assert any("UPDATE bot_seo_suggestions SET applied_at" in q for q, _ in pool.executed)


async def test_apply_bot_seo_no_suggestion(monkeypatch):
    pool = _FakePool(sugg_row=None)
    res = await br.apply_bot_seo(pool, 42, 5)
    assert res["ok"] is False and "рекоменд" in res["error"].lower()


async def test_apply_bot_seo_rejects_foreign_bot(monkeypatch):
    pool = _FakePool(sugg_row={"name": "X", "short_desc": ""})

    async def _none(p, q, *a):
        return None  # бот не принадлежит владельцу

    monkeypatch.setattr("database.db.fetchrow_bot", _none)
    res = await br.apply_bot_seo(pool, 42, 5)
    assert res["ok"] is False, "чужой бот → отказ (owner-scope)"


# ── (4) проводка эндпоинтов/маршрутов ──────────────────────────────────────

def test_endpoints_and_routes_registered():
    from services import mini_app_api
    src = inspect.getsource(mini_app_api)
    assert "async def seo_apply_bot(" in src
    assert "async def reopt_setting(" in src
    assert re.search(r'add_post\(\s*["\']/api/miniapp/seo/apply_bot["\']\s*,\s*seo_apply_bot', src)
    assert re.search(r'add_post\(\s*["\']/api/miniapp/ranking/reopt_setting["\']\s*,\s*reopt_setting', src)


def test_apply_bot_endpoint_reuses_service():
    from services import mini_app_api
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def seo_apply_bot\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m and "bot_reoptimizer.apply_bot_seo" in m.group(1), (
        "эндпоинт должен переиспользовать сервис (без дублей логики)"
    )


# ── (5) хук в ranking_checker ──────────────────────────────────────────────

def test_ranking_checker_hooks_reoptimize_on_drop():
    from services import ranking_checker
    src = inspect.getsource(ranking_checker)
    assert "auto_reoptimize" in src, "чекер должен читать opt-in флаг auto_reoptimize"
    assert "bot_reoptimizer" in src and "generate_and_store" in src, (
        "на падении позиции чекер должен генерировать рекомендацию"
    )
