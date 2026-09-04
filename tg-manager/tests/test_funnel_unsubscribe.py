"""Отписка от drip-воронки по слову «стоп».

Что было сломано: выйти из цепочки было НЕЧЕМ. Единственный способ перестать
получать шаги — заблокировать бота (403 → funnel_runner помечает dropped).
Человек, написавший «стоп», продолжал получать цепочку: это и жалобы на спам,
и безвозвратная потеря контакта для владельца бота.

Ключевое требование к распознаванию — точность: срабатывание по вхождению
подстроки отписывало бы людей, которые об этом не просили («стоп-кран»,
«остановка», «стоп, а расскажите подробнее»).
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

from database import db
from services.auto_responder import _is_stop_word

_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── Распознавание ─────────────────────────────────────────────────────────────

def test_plain_stop_words_recognised():
    for s in ("стоп", "СТОП", "Стоп!", "/stop", "stop", "STOP",
              "отписаться", "отписка", "unsubscribe", "не писать"):
        assert _is_stop_word(s), s


def test_trailing_punctuation_and_spaces_tolerated():
    for s in ("  стоп  ", "стоп.", "стоп!!!", "стоп…"):
        assert _is_stop_word(s), s


def test_substring_does_not_trigger_unsubscribe():
    """Главная ловушка: поиск подстроки отписал бы непричастных."""
    for s in ("стоп-кран сломался", "остановка автобуса", "стопудово",
              "где стоп-лист?", "перестаньте", "хочу застопить"):
        assert not _is_stop_word(s), s


def test_long_message_is_conversation_not_command():
    assert not _is_stop_word("стоп, а расскажите подробнее про тарифы пожалуйста")


def test_empty_and_garbage():
    assert not _is_stop_word("")
    assert not _is_stop_word("   ")
    assert not _is_stop_word(None)
    assert not _is_stop_word("привет")


# ── Запрос отписки ────────────────────────────────────────────────────────────

class _FakePool:
    def __init__(self, rows):
        self._rows = rows
        self.fetched = []
        self.executed = []

    async def fetch(self, query, *args):
        self.fetched.append((query, args))
        return self._rows

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


def test_unsubscribe_marks_dropped_not_completed():
    """Воронка НЕ пройдена — статистика завершений не должна врать."""
    pool = _FakePool([{"funnel_id": 7}])
    n = asyncio.run(db.unsubscribe_user_from_funnels(pool, bot_id=1, user_id=2))
    assert n == 1
    q = pool.fetched[0][0]
    assert "dropped = true" in q
    assert "completed = true" not in q


def test_unsubscribe_is_scoped_to_bot_and_user():
    """Отписка не должна задевать другие боты владельца или других людей."""
    pool = _FakePool([])
    asyncio.run(db.unsubscribe_user_from_funnels(pool, bot_id=1, user_id=2))
    q, args = pool.fetched[0]
    assert "f.bot_id = $1" in q and "fs.user_id = $2" in q
    assert args == (1, 2)


def test_unsubscribe_skips_already_finished_subscriptions():
    pool = _FakePool([])
    asyncio.run(db.unsubscribe_user_from_funnels(pool, bot_id=1, user_id=2))
    q = pool.fetched[0][0]
    assert "fs.completed = false" in q and "fs.dropped = false" in q


def test_unsubscribe_counts_every_funnel():
    pool = _FakePool([{"funnel_id": 1}, {"funnel_id": 2}, {"funnel_id": 3}])
    assert asyncio.run(db.unsubscribe_user_from_funnels(pool, 1, 2)) == 3
    assert len(pool.executed) == 3, "счётчик dropped_count по каждой воронке"


def test_unsubscribe_returns_zero_when_nothing_active():
    pool = _FakePool([])
    assert asyncio.run(db.unsubscribe_user_from_funnels(pool, 1, 2)) == 0


def test_counter_failure_does_not_break_unsubscribe():
    """Попытка сломать: счётчик — наблюдаемость, отписка важнее."""
    class _BrokenPool(_FakePool):
        async def execute(self, query, *args):
            raise RuntimeError("нет колонки dropped_count")

    pool = _BrokenPool([{"funnel_id": 7}])
    assert asyncio.run(db.unsubscribe_user_from_funnels(pool, 1, 2)) == 1


# ── Подключение к пайплайну входящих ──────────────────────────────────────────

def test_stop_handled_before_autoresponder_rules():
    """На просьбу прекратить нельзя отвечать очередным маркетинговым сообщением."""
    src = (_ROOT / "services" / "auto_responder.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process_bot":
            body = ast.get_source_segment(src, node)
    assert body and "_is_stop_word(text)" in body
    assert "unsubscribe_user_from_funnels" in body
    # Проверка стоп-слова должна стоять раньше подписки на воронки.
    assert body.index("_is_stop_word(text)") < body.index("subscribe_to_funnel"), (
        "стоп обязан обрабатываться до правил и подписок, иначе человек в тот же "
        "заход снова попадёт в воронку"
    )
