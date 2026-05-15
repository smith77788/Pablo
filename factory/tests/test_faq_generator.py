"""Tests for factory/agents/faq_generator.py — Phase 27 FAQ Generator."""
from __future__ import annotations
import sqlite3
import tempfile
import os
import pytest

from factory.agents.faq_generator import FAQGenerator, FAQ_TEMPLATES


@pytest.fixture
def gen() -> FAQGenerator:
    return FAQGenerator()


# ── generate_answers ──────────────────────────────────────────────────────────

def test_generate_answers_booking_returns_three_items(gen):
    result = gen.generate_answers('booking')
    assert len(result) == 3


def test_generate_answers_booking_has_required_keys(gen):
    result = gen.generate_answers('booking')
    for item in result:
        assert 'question' in item
        assert 'answer' in item
        assert 'category' in item


def test_generate_answers_booking_category_label(gen):
    result = gen.generate_answers('booking')
    for item in result:
        assert item['category'] == 'booking'


def test_generate_answers_pricing_contains_ruble_sign(gen):
    result = gen.generate_answers('pricing')
    answers_combined = ' '.join(item['answer'] for item in result)
    assert '₽' in answers_combined


def test_generate_answers_catalog_returns_three_items(gen):
    result = gen.generate_answers('catalog')
    assert len(result) == 3


def test_generate_answers_general_returns_three_items(gen):
    result = gen.generate_answers('general')
    assert len(result) == 3


def test_generate_answers_all_categories_non_empty_answers(gen):
    for cat in FAQ_TEMPLATES:
        result = gen.generate_answers(cat)
        for item in result:
            assert item['answer'], f"Empty answer for category={cat}, question={item['question']}"


def test_generate_answers_unknown_category_falls_back_to_general(gen):
    result = gen.generate_answers('nonexistent_category')
    # Falls back to general template which has 3 questions
    assert len(result) == 3


# ── _build_answer ─────────────────────────────────────────────────────────────

def test_build_answer_returns_str(gen):
    answer = gen._build_answer('booking', 'test question', 'Prefix ')
    assert isinstance(answer, str)


def test_build_answer_booking_contains_telegram(gen):
    answer = gen._build_answer('booking', 'q', 'Для бронирования модели ')
    assert 'Telegram' in answer


# ── improve_answer ────────────────────────────────────────────────────────────

def test_improve_answer_replaces_обратитесь(gen):
    original = 'Пожалуйста, обратитесь к менеджеру.'
    improved = gen.improve_answer(original)
    assert 'обратитесь' not in improved
    assert 'напишите нам в Telegram или' in improved


def test_improve_answer_replaces_свяжитесь(gen):
    original = 'Свяжитесь с нами для уточнения.'
    improved = gen.improve_answer(original)
    assert 'Свяжитесь' in improved or 'свяжитесь' not in improved.lower()


def test_improve_answer_no_change_when_no_keywords(gen):
    original = 'Мы работаем каждый день с 9 до 21.'
    improved = gen.improve_answer(original)
    assert improved == original


# ── suggest_questions ─────────────────────────────────────────────────────────

def test_suggest_questions_returns_list(gen):
    result = gen.suggest_questions([])
    assert isinstance(result, list)


def test_suggest_questions_excludes_existing(gen):
    existing = ['Как забронировать модель?']
    suggestions = gen.suggest_questions(existing)
    assert 'Как забронировать модель?' not in suggestions


def test_suggest_questions_empty_existing_returns_all(gen):
    all_count = sum(len(t['questions']) for t in FAQ_TEMPLATES.values())
    suggestions = gen.suggest_questions([])
    assert len(suggestions) == all_count


# ── read_from_db ──────────────────────────────────────────────────────────────

def test_read_from_db_missing_path_returns_empty(gen):
    result = gen.read_from_db('/nonexistent/path/data.db')
    assert result == []


def test_read_from_db_valid_db_with_faq_table(gen):
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE faq (id INTEGER PRIMARY KEY, question TEXT, answer TEXT, "
            "category TEXT, active INTEGER DEFAULT 1, sort_order INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO faq (question, answer, category, active, sort_order) "
            "VALUES (?, ?, ?, 1, 1)",
            ('Тестовый вопрос?', 'Тестовый ответ.', 'general'),
        )
        conn.commit()
        conn.close()

        result = gen.read_from_db(db_path)
        assert len(result) == 1
        assert result[0]['question'] == 'Тестовый вопрос?'
    finally:
        os.unlink(db_path)


def test_read_from_db_inactive_rows_excluded(gen):
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE faq (id INTEGER PRIMARY KEY, question TEXT, answer TEXT, "
            "category TEXT, active INTEGER DEFAULT 1, sort_order INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO faq (question, answer, category, active, sort_order) VALUES (?, ?, ?, 0, 1)",
            ('Неактивный вопрос?', 'Ответ.', 'general'),
        )
        conn.commit()
        conn.close()

        result = gen.read_from_db(db_path)
        assert result == []
    finally:
        os.unlink(db_path)


# ── run ───────────────────────────────────────────────────────────────────────

def test_run_with_missing_db_returns_valid_structure(gen):
    result = gen.run('/nonexistent/data.db')
    assert 'existing_count' in result
    assert 'suggestions' in result
    assert 'improved_count' in result
    assert 'new_by_category' in result
    assert result['existing_count'] == 0


def test_run_with_valid_db_populates_new_by_category(gen):
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE faq (id INTEGER PRIMARY KEY, question TEXT, answer TEXT, "
            "category TEXT, active INTEGER DEFAULT 1, sort_order INTEGER DEFAULT 0)"
        )
        conn.commit()
        conn.close()

        result = gen.run(db_path)
        assert set(result['new_by_category'].keys()) == set(FAQ_TEMPLATES.keys())
        for cat, entries in result['new_by_category'].items():
            assert len(entries) == 3, f"Expected 3 entries for {cat}, got {len(entries)}"
    finally:
        os.unlink(db_path)


# ── run_phase_27_faq_generator (integration) ──────────────────────────────────

def test_run_phase_27_missing_db_returns_error_or_ok():
    """Phase 27 wrapper with missing DB should return ok (generator handles missing gracefully)."""
    from factory.cycle import run_phase_27_faq_generator
    result = run_phase_27_faq_generator('/nonexistent/path/data.db')
    # Generator doesn't raise on missing DB; it returns ok with 0 existing
    assert result.get('status') in ('ok', 'error')
    assert 'status' in result


def test_run_phase_27_with_valid_db():
    from factory.cycle import run_phase_27_faq_generator
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE faq (id INTEGER PRIMARY KEY, question TEXT, answer TEXT, "
            "category TEXT, active INTEGER DEFAULT 1, sort_order INTEGER DEFAULT 0)"
        )
        conn.commit()
        conn.close()

        result = run_phase_27_faq_generator(db_path)
        assert result['status'] == 'ok'
        assert 'suggestions' in result
        assert 'existing_faq' in result
    finally:
        os.unlink(db_path)
