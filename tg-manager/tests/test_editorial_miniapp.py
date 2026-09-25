"""Редактор канала в Mini App: правила задаются и реально применяются.

Раньше правила редактора можно было завести только кодом (save_profile), а совет
перед публикацией жил лишь в боте: в Mini App — главном интерфейсе — пост уходил
во все каналы без проверки, а массовая публикация (без channel_key) не читала
политику владельца вообще. Тесты гоняют настоящие маршруты на aiohttp-сервере с
пулом, который хранит va_channel_brain в памяти.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from aiohttp import ClientSession, web

from services import channel_brain as cb
from services import channel_brain_store as store
from services import editorial_review as er
from services import mini_app_api as M

UID = 770011
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _BrainPool:
    """va_channel_brain в памяти: upsert и чтение по (owner_id, channel_key)."""

    def __init__(self, posts=None):
        self.rows: dict = {}
        self.posts = posts or []
        self.fail_save = False
        self.pillar_history: list = []   # старые в начале

    async def fetchrow(self, q, *a):
        if "INSERT INTO va_channel_brain" in q:
            if self.fail_save:
                raise RuntimeError("db down")
            owner, ch, rules, pillars, mix, mode, streak, dup = a
            self.rows[(owner, ch)] = {
                "owner_id": owner, "channel_key": ch, "brand_rules": rules,
                "pillars": pillars, "mix_weights": mix, "autonomy_mode": mode,
                "max_streak": streak, "dup_threshold": dup,
            }
            return {"id": 1}
        if "FROM va_channel_brain" in q:
            return self.rows.get((a[0], a[1]))
        return None

    async def fetch(self, q, *a):
        if "SELECT pillar" in q:
            # свежие сверху, как отдаёт ORDER BY t DESC
            return [{"pillar": p} for p in reversed(self.pillar_history)]
        if "FROM va_channel_posts" in q:
            return [{"body": b} for b in self.posts]
        return []

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        return "OK"


async def _call(pool, monkeypatch, method, path, payload=None):
    monkeypatch.setattr(M, "_get_uid", lambda request: UID)
    # Лимитер частоты один на процесс и считает по 127.0.0.1: в полном прогоне
    # соседние тесты выбирают окно, и сюда приходил 429 вместо ответа.
    from services import security as _sec
    _sec._rate_limiter._requests.clear()
    app = web.Application()
    M.setup_routes(app, pool)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as s:
            kw = {}
            if payload is not None:
                kw = {"data": json.dumps(payload),
                      "headers": {"Content-Type": "application/json"}}
            async with s.request(method, f"http://127.0.0.1:{port}{path}", **kw) as r:
                return r.status, await r.json()
    finally:
        await runner.cleanup()


def test_policy_roundtrip_and_applies_to_mass_publish_review(monkeypatch):
    pool = _BrainPool()

    async def go():
        st, d = await _call(pool, monkeypatch, "GET", "/api/miniapp/editorial/policy")
        assert st == 200 and d["policy"]["configured"] is False
        st, d = await _call(pool, monkeypatch, "PUT", "/api/miniapp/editorial/policy", {
            "max_emoji": "1", "max_cta": None, "forbidden_words": ["Халява", "халява", " "],
            "banned_openings": ["друзья"], "dup_threshold": 0.7,
        })
        assert st == 200, d
        p = d["policy"]
        assert p["configured"] is True and p["max_emoji"] == 1 and p["max_cta"] is None
        assert p["forbidden_words"] == ["халява"] and p["dup_threshold"] == pytest.approx(0.7)
        # Совет перед массовой публикацией (без channel_key) теперь видит правила.
        st, r = await _call(pool, monkeypatch, "POST", "/api/miniapp/editorial/review",
                            {"text": "Друзья, халява 🔥🔥"})
        assert st == 200 and r["needs_review"] is True
        text = " ".join(r["reasons"])
        assert "эмодзи" in text and "«халява»" in text and "«друзья»" in text

    asyncio.run(go())
    assert (UID, store.OWNER_DEFAULT_KEY) in pool.rows


def test_policy_save_refuses_bad_input_in_russian(monkeypatch):
    pool = _BrainPool()

    async def go():
        return await _call(pool, monkeypatch, "PUT", "/api/miniapp/editorial/policy",
                           {"min_chars": 500, "max_chars": 100, "max_emoji": "много"})

    st, d = asyncio.run(go())
    assert st == 400
    assert "Минимальная длина больше максимальной" in d["error"]
    assert "Эмодзи" in d["error"]
    assert not pool.rows, "полуправильная политика не должна сохраняться"


def test_policy_save_db_failure_is_honest(monkeypatch):
    pool = _BrainPool()
    pool.fail_save = True

    async def go():
        return await _call(pool, monkeypatch, "PUT", "/api/miniapp/editorial/policy",
                           {"max_emoji": 3})

    st, d = asyncio.run(go())
    assert st == 500 and "Не удалось сохранить" in d["error"]


def test_policy_save_keeps_pillars_it_does_not_edit(monkeypatch):
    pool = _BrainPool()
    pool.rows[(UID, "*")] = {
        "owner_id": UID, "channel_key": "*", "brand_rules": "{}",
        "pillars": '["новости", "реклама"]', "mix_weights": '{"новости": 3}',
        "autonomy_mode": "semi", "max_streak": 3, "dup_threshold": 0.6,
    }

    async def go():
        return await _call(pool, monkeypatch, "PUT", "/api/miniapp/editorial/policy",
                           {"max_cta": 2})

    st, _ = asyncio.run(go())
    assert st == 200
    row = pool.rows[(UID, "*")]
    assert json.loads(row["pillars"]) == ["новости", "реклама"]
    assert row["autonomy_mode"] == "semi" and row["max_streak"] == 3


def test_review_endpoint_flags_repeat_of_recent_post(monkeypatch):
    recent = "Новый курс по налогам стартует в понедельник, успейте записаться сегодня"
    pool = _BrainPool(posts=[recent])

    async def go():
        return await _call(pool, monkeypatch, "POST", "/api/miniapp/editorial/review",
                           {"text": recent})

    st, r = asyncio.run(go())
    assert st == 200 and r["needs_review"] is True and r["similarity"] == 100


def test_review_endpoint_requires_text(monkeypatch):
    async def go():
        return await _call(_BrainPool(), monkeypatch, "POST",
                           "/api/miniapp/editorial/review", {"text": "   "})

    st, d = asyncio.run(go())
    assert st == 400 and d["error"] == "Нужен текст"


# ── validate_policy ─────────────────────────────────────────────────────────


def test_validate_policy_cleans_and_bounds():
    clean, errors = store.validate_policy({
        "forbidden_words": "Скидка,  скидка\nКЭШБЭК", "dup_threshold": 80,
        "max_emoji": "", "min_chars": "10", "max_chars": 200,
    })
    assert errors == []
    r = clean["brand_rules"]
    assert r["forbidden_words"] == ["скидка", "кэшбэк"]
    assert r["max_emoji"] is None and r["min_chars"] == 10 and r["max_chars"] == 200
    assert clean["dup_threshold"] == pytest.approx(0.8)


@pytest.mark.parametrize("payload,needle", [
    ({"max_emoji": -1}, "Эмодзи"),
    ({"max_cta": 2.5}, "Призывов"),
    ({"max_chars": 5000}, "Максимальная длина"),
    ({"dup_threshold": 0.1}, "Порог повтора"),
    ({"forbidden_words": ["слово%d" % i for i in range(101)]}, "не больше 100"),
    ({"banned_openings": ["я" * 61]}, "длиннее 60"),
    ([], "Ожидался объект"),
])
def test_validate_policy_rejects(payload, needle):
    _, errors = store.validate_policy(payload)
    assert any(needle in e for e in errors), errors


@pytest.mark.asyncio
async def test_get_profile_survives_garbage_jsonb():
    pool = _BrainPool()
    pool.rows[(UID, "*")] = {
        "owner_id": UID, "channel_key": "*", "brand_rules": '{"max_chars": "много"}',
        "pillars": "[]", "mix_weights": "{}", "autonomy_mode": "manual",
        "max_streak": 2, "dup_threshold": 0.6,
    }
    assert await store.get_profile(pool, UID, "*") is None


# ── spintax в совете редактора ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_spintax_compares_a_real_variant():
    recent = "Новый курс по налогам стартует в понедельник, успейте записаться сегодня"
    pool = _BrainPool(posts=[recent])
    tpl = "{Новый|Новый} курс по налогам стартует в понедельник, успейте записаться сегодня"
    v = await er.review_draft(pool, UID, tpl)
    assert v.needs_review is True and v.repetition["is_duplicate"] is True


@pytest.mark.asyncio
async def test_review_spintax_forbidden_word_in_any_branch():
    pool = _BrainPool()
    await store.save_profile(pool, UID, "*", brand_rules={"forbidden_words": ["халява"]})
    hits = 0
    for _ in range(6):
        v = await er.review_draft(pool, UID, "Сегодня {скидка|халява} на курс")
        hits += any("«халява»" in r for r in v.reasons)
    assert hits == 6, "запрещённое слово в любой ветке должно ловиться всегда"


def test_miniapp_publish_asks_editor_before_confirm():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "mini_app", "screens", "editorial.js"), encoding="utf-8").read()
    assert '<script src="screens/editorial.js"></script>' in html
    assert "editorialReviewForConfirm(text)" in html
    assert "askConfirm(edNote+'Опубликовать '" in html
    # пост во все каналы аккаунта — тот же совет на своём подтверждении
    assert "askConfirm(bpEdNote + 'Опубликовать во все каналы этого аккаунта'" in html
    assert "/api/miniapp/editorial/review" in js and "/api/miniapp/editorial/policy" in js


def test_verdict_to_public_shape():
    v = cb.EditorialVerdict(ok=False, needs_review=True, reasons=["x"] * 12,
                            repetition={"max_similarity": 0.734})
    out = er.verdict_to_public(v)
    assert out == {"needs_review": True, "reasons": ["x"] * 8, "similarity": 73}


# ── рубрики (контент-микс) ──────────────────────────────────────────────────


def test_validate_pillars_lines_and_weights():
    names, w, errors = store.validate_pillars("Новости: 3\nИтоги: неделя\n\nРеклама")
    assert errors == []
    assert names == ["Новости", "Итоги: неделя", "Реклама"]
    assert w == {"Новости": 3.0, "Итоги: неделя": 1.0, "Реклама": 1.0}


@pytest.mark.parametrize("raw,needle", [
    ("Новости: 0", "доля от 1 до 10"),
    ("Новости: 11", "доля от 1 до 10"),
    ("Новости\nновости", "указана дважды"),
    ("\n".join("Р%d" % i for i in range(13)), "не больше 12"),
    ("я" * 41, "длиннее 40"),
    (42, "нужен список"),
])
def test_validate_pillars_rejects(raw, needle):
    _, _, errors = store.validate_pillars(raw)
    assert any(needle in e for e in errors), errors


def test_pillars_saved_and_next_pillar_follows_mix(monkeypatch):
    pool = _BrainPool()
    # две рекламы подряд — третьей редактор не советует
    pool.pillar_history = ["Новости", "Реклама", "Реклама"]

    async def go():
        st, d = await _call(pool, monkeypatch, "PUT", "/api/miniapp/editorial/policy",
                            {"pillars": "Реклама: 5\nНовости: 1"})
        assert st == 200, d
        assert d["policy"]["pillars"] == [{"name": "Реклама", "weight": 5},
                                          {"name": "Новости", "weight": 1}]
        assert d["next_pillar"] == "Новости"
        # сохранение без поля pillars не стирает рубрики
        st, d = await _call(pool, monkeypatch, "PUT", "/api/miniapp/editorial/policy",
                            {"max_emoji": 2})
        assert [p["name"] for p in d["policy"]["pillars"]] == ["Реклама", "Новости"]

    asyncio.run(go())


def test_mass_publish_rejects_unknown_pillar(monkeypatch):
    pool = _BrainPool()

    async def _count(*a, **k):
        return 3
    monkeypatch.setattr(M, "_safe_count", _count)

    async def go():
        return await _call(pool, monkeypatch, "POST", "/api/miniapp/mass_publish",
                           {"text": "пост", "pillar": "Опечатка"})

    st, d = asyncio.run(go())
    assert st == 400 and "рубрики нет" in d["error"]


@pytest.mark.asyncio
async def test_recent_pillars_oldest_first():
    from services import content_memory as cm
    pool = _BrainPool()
    pool.pillar_history = ["А", "Б", "В"]
    assert await cm.recent_pillars_for_owner(pool, UID) == ["А", "Б", "В"]


def test_mass_publish_records_pillar():
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    assert 'pillar=params.get("pillar")' in src
