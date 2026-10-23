"""Вирт.администратор: не льёт служебный текст в канал и не дублирует новости.

Две жалобы владельца по скриншотам канала «Свіжі Новини України»:
  1. в канал ушёл служебный текст модели: «Звісно, ось чорновик поста, який
     відповідає всім вимогам.» + `---` + сам пост;
  2. один сюжет вышел дважды — по-украински и по-русски (кросс-язычный дубль).
"""
from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from services import channel_admin as ca
from services import channel_admin_runner as runner


# ── 1. Чистка служебной преамбулы на двери публикации ─────────────────────────

def test_sanitize_strips_leaked_draft_preamble():
    leaked = ("Звісно, ось чорновик поста, який відповідає всім вимогам.\n\n"
              "---\n\nЗнову Харків. Знову вечір. Знову обстріл.")
    out = ca.sanitize_for_channel(leaked)
    assert out.startswith("Знову Харків"), f"преамбула не срезана: {out!r}"
    assert "чорновик" not in out and "вимогам" not in out
    assert "---" not in out.splitlines()[0]


def test_sanitize_strips_russian_preamble_and_spintax():
    leaked = "Конечно, вот черновик:\nПост с {вариантом|выбором} текста."
    out = ca.sanitize_for_channel(leaked)
    assert "черновик" not in out.lower()
    # spintax-символы обезврежены
    assert "{" not in out and "}" not in out and "|" not in out


def test_sanitize_is_idempotent_on_clean_text():
    clean = "Обычный пост без преамбулы. Вторая мысль."
    assert ca.sanitize_for_channel(clean) == clean
    assert ca.sanitize_for_channel(ca.sanitize_for_channel(clean)) == clean


def test_sanitize_keeps_normal_separator_inside_long_post():
    # `---` без мета-слов в «шапке» — смысловой разделитель, не преамбула.
    body = ("Первый большой абзац поста про важное событие и его детали тут.\n\n"
            "---\n\nВторой большой абзац с продолжением мысли и выводами тут.")
    out = ca.sanitize_for_channel(body)
    assert "Первый большой абзац" in out and "Второй большой абзац" in out


def test_publish_sanitizes_at_the_door():
    """publish() обязан чистить текст сам — это единственная дверь в канал."""
    src = inspect.getsource(ca.publish)
    assert "sanitize_for_channel(text)" in src, (
        "publish не чистит текст на двери — преамбула утечёт мимо write_post")


# ── 2. Кросс-язычный антидубль новостей ───────────────────────────────────────

class _DedupPool:
    def __init__(self, recent_bodies):
        self.recent = recent_bodies

    async def fetch(self, q, *a):
        if "va_channel_posts" in q:
            return [{"body": b} for b in self.recent]
        return []


@pytest.mark.asyncio
async def test_is_duplicate_true_when_model_says_so():
    async def fake_complete(system, user):
        return '{"duplicate": true}'
    pool = _DedupPool(["Путин хочет сделку, Трамп уверен — та же новость по-русски"])
    assert await ca.is_duplicate_of_recent(
        pool, 7, 8, "Путін дуже хоче укласти угоду — та сама новина українською",
        complete=fake_complete) is True


@pytest.mark.asyncio
async def test_is_not_duplicate_when_model_says_no():
    async def fake_complete(system, user):
        return '{"duplicate": false}'
    pool = _DedupPool(["Совсем другая новость про погоду"])
    assert await ca.is_duplicate_of_recent(
        pool, 7, 8, "Важный анонс нового меню кофейни", complete=fake_complete) is False


@pytest.mark.asyncio
async def test_is_duplicate_failopen_without_history():
    async def fake_complete(system, user):
        raise AssertionError("не должны звать модель без истории")
    pool = _DedupPool([])  # недавних постов нет
    assert await ca.is_duplicate_of_recent(
        pool, 7, 8, "Любой достаточно длинный текст поста тут", complete=fake_complete) is False


@pytest.mark.asyncio
async def test_is_duplicate_failopen_on_model_error():
    async def boom(system, user):
        raise RuntimeError("модель недоступна")
    pool = _DedupPool(["Недавний пост"])
    # Сбой модели → False (редкий дубль лучше, чем заглохший канал).
    assert await ca.is_duplicate_of_recent(
        pool, 7, 8, "Достаточно длинный новый текст поста тут", complete=boom) is False


# ── _draft_live_news гасит дубль, не публикуя второй пост ─────────────────────

class _Pool:
    def __init__(self, events):
        self.events = events
        self.fetchvals = [0, 0]  # pending=0, published=0
        self.executed = []

    async def fetchval(self, q, *a):
        return self.fetchvals.pop(0)

    async def fetch(self, q, *a):
        return self.events if "va_news_inbox" in q else []

    async def fetchrow(self, *a):
        return None

    async def execute(self, q, *a):
        self.executed.append((q, a))

    @asynccontextmanager
    async def acquire(self):
        yield None


def _events():
    now = datetime.now(UTC)
    return [{"id": 21, "source_username": "src", "source_message_id": 5,
             "published_at": now, "source_text": "Новость"}]


@pytest.mark.asyncio
async def test_draft_live_news_skips_duplicate(monkeypatch):
    calls = {}

    async def write_post(*a, **k):
        return ca.Draft("Новости", "Текст новости (дубль)", [], False, True)

    async def publish(*a, **k):
        calls["published"] = True
        return 777

    async def dup(*a, **k):
        return True  # модель сказала: дубль

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(ca, "write_post", write_post)
    monkeypatch.setattr(ca, "publish", publish)
    monkeypatch.setattr(ca, "is_duplicate_of_recent", dup)
    monkeypatch.setattr(ca, "log_event", noop)

    pool = _Pool(_events())
    created = await asyncio.wait_for(runner._draft_live_news(
        pool, None,
        {"owner_id": 7, "channel_id": 8, "posts_per_day": 3, "publish_mode": "auto"}),
        timeout=2)
    assert created is True
    assert "published" not in calls, "дубль всё равно опубликован"
    # Сигнал погашен (помечен published, чтобы не драфтиться снова).
    assert any("va_news_inbox" in q and "status='published'" in q
               for q, _ in pool.executed), "сигнал-дубль не погашен"


def test_runner_calls_dedup_before_publishing():
    src = inspect.getsource(runner._draft_live_news)
    assert "is_duplicate_of_recent" in src
    i_dedup = src.index("is_duplicate_of_recent")
    i_publish = src.index("ca.publish(")
    assert i_dedup < i_publish, "дедуп обязан идти ДО публикации"
