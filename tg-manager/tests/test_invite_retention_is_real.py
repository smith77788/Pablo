"""Удержание приглашённых: отток наконец записывается и виден.

Продукт считал удержание как «вступило минус ушло», но «ушло» не записывал
никто: событие organism_events kind='left' не испускал ни один модуль. Значит
удержание выходило ровно 100% при любом оттоке, а подсказка мозга «отток —
welcome не удерживает» не могла сработать ни при каких условиях.

Плюс эндпоинт /api/miniapp/invite/retention существовал без экрана.
"""
from __future__ import annotations

import asyncio
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
SCREEN = os.path.join(ROOT, "mini_app", "screens", "retention.js")


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8").read()


# ── Отток записывается ───────────────────────────────────────────────────────

def test_leave_is_recorded_by_chat_guard():
    src = _src("bot/handlers/chat_guard.py")
    assert "async def _record_leave" in src, "уход из чата по-прежнему никто не пишет"
    assert "ct in cg.LEAVE_TYPES" in src, "обработчик системных сообщений не ловит уход"
    body = src[src.index("async def _record_leave"):]
    assert 'spine.emit(pool, owner_id, "left"' in body
    assert '"chat_id": message.chat.id' in body
    assert 'is_bot' in body, "уход бота не отличается от ухода человека"
    # Запись идёт ДО удаления системного сообщения.
    handler = src[src.index("async def on_service_message"):src.index("async def _record_leave")]
    assert handler.index("_record_leave") < handler.index("should_delete_service")


def test_leave_event_is_attributed_to_the_owner():
    """Чат, подхваченный «по факту прав», владельца не имеет — приписывать некому."""
    src = _src("bot/handlers/chat_guard.py")
    body = src[src.index("async def _record_leave"):]
    assert "owner_id <= 0" in body
    assert "return" in body


def test_left_events_actually_feed_the_metric():
    """Метрика читает именно тот вид события, который теперь пишется."""
    world = _src("services/organism/world.py")
    assert "kind='left'" in world
    assert "kind='left'" in API


def test_retention_metric_reacts_to_churn():
    from services.invite_retention import summarize, health
    assert summarize(100, 0)["retention_pct"] == 100.0
    assert summarize(100, 40)["retention_pct"] == 60.0
    assert health(summarize(100, 60)["retention_pct"]) == "red"


# ── Где уходят ───────────────────────────────────────────────────────────────

def test_endpoint_says_where_people_leave():
    ep = API[API.index("async def invite_retention_overview"):]
    ep = ep[:ep.index("async def boost_submit")]
    assert '"by_chat"' in ep, "одна общая цифра оттока не говорит, что делать"
    assert "payload->>'chat_id'" in ep
    assert "kind='left'" in ep
    assert "owner_id=$1" in ep       # скоуп по владельцу


# ── Экран ────────────────────────────────────────────────────────────────────

def test_retention_screen_exists_and_is_wired():
    assert os.path.exists(SCREEN), "эндпоинт удержания по-прежнему без экрана"
    js = open(SCREEN, encoding="utf-8").read()
    assert "screens/retention.js" in HTML, "экран не подключён в index.html"
    assert "function openRetention" in js
    assert "/api/miniapp/invite/retention?days=" in js
    # Периоды выбираются, а не захардкожены одним значением.
    assert "RT_PERIODS" in js and "[7," in js and "[90," in js


def test_screen_names_the_honest_limitation():
    """Отток виден только там, где бот админ — экран говорит это вслух."""
    js = open(SCREEN, encoding="utf-8").read()
    assert "администратор" in js


def test_screen_shows_where_people_leave_and_offers_next_step():
    js = open(SCREEN, encoding="utf-8").read()
    body = js[js.index("async function openRetention"):]
    assert "d.by_chat" in body
    assert "Откуда уходят" in body
    assert "/guard" in body, "не сказано, чем настраивается приветствие чата"
    assert "openMassInvite()" in body


def test_brain_hint_leads_to_retention_not_invite():
    """Подсказка «отток» вела на масс-инвайт — то есть лить ещё людей в дырку."""
    assert "if (k==='retention') return openRetention(30);" in HTML


def test_digest_shows_retention_row():
    from services.organism.digest import compose_digest
    snap = {"retention": {"retained": 31}, "fleet": {"accounts": 1}}
    d = compose_digest(snap, suggestions=[])
    ids = {st["id"] for sec in d["sections"] for st in sec["stats"]}
    assert "retained" in ids
    go = re.search(r"const DIG_GO = \{(.*?)\n\};", HTML, re.S).group(1)
    assert "retained:" in go
