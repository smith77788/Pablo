"""Tests for Phase 26: Model Bio Generator."""
import pytest
import sqlite3
import os
import tempfile
from factory.cycle import run_phase_26_model_bios, _generate_heuristic_bio


class TestGenerateHeuristicBio:
    def test_returns_string(self):
        result = _generate_heuristic_bio({'name': 'Анна', 'city': 'Москва'})
        assert isinstance(result, str)

    def test_contains_name(self):
        result = _generate_heuristic_bio({'name': 'Мария'})
        assert 'Мария' in result

    def test_contains_city(self):
        result = _generate_heuristic_bio({'name': 'Тест', 'city': 'Казань'})
        assert 'Казань' in result

    def test_fashion_category(self):
        result = _generate_heuristic_bio({'name': 'X', 'category': 'fashion'})
        assert 'fashion' in result.lower() or 'подиум' in result.lower()

    def test_commercial_category(self):
        result = _generate_heuristic_bio({'name': 'X', 'category': 'commercial'})
        assert 'коммерч' in result.lower() or 'реклам' in result.lower()

    def test_height_included(self):
        result = _generate_heuristic_bio({'name': 'X', 'height': 175})
        assert '175' in result

    def test_empty_dict_doesnt_crash(self):
        result = _generate_heuristic_bio({})
        assert isinstance(result, str)
        assert len(result) > 10

    def test_min_length(self):
        result = _generate_heuristic_bio({'name': 'A', 'city': 'B'})
        assert len(result) >= 50


class TestRunPhase26:
    def _make_db(self, models_data=None):
        """Create a temp SQLite DB for testing."""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE models (
                id INTEGER PRIMARY KEY,
                name TEXT, age INTEGER, height INTEGER,
                city TEXT, category TEXT, hair_color TEXT, eye_color TEXT,
                bio TEXT, available INTEGER DEFAULT 1, archived INTEGER DEFAULT 0,
                updated_at TEXT
            )
        """)
        if models_data:
            conn.executemany(
                "INSERT INTO models (id, name, city, category, bio, available, archived) VALUES (?,?,?,?,?,?,?)",
                models_data
            )
        conn.commit()
        conn.close()
        return path

    def test_skips_when_db_not_found(self):
        result = run_phase_26_model_bios('/nonexistent/path.db')
        assert result['status'] == 'skipped'

    def test_ok_when_all_bios_present(self):
        path = self._make_db([(1, 'Анна', 'Москва', 'fashion', 'This is a long enough bio text that is over fifty chars for sure.', 1, 0)])
        result = run_phase_26_model_bios(path)
        assert result['status'] == 'ok'
        assert result['updated'] == 0
        os.unlink(path)

    def test_updates_empty_bio(self):
        path = self._make_db([(1, 'Мария', 'Москва', 'fashion', '', 1, 0)])
        result = run_phase_26_model_bios(path)
        assert result['status'] == 'ok'
        assert result['updated'] >= 1
        os.unlink(path)

    def test_updates_short_bio(self):
        path = self._make_db([(1, 'Ольга', 'Питер', 'events', 'Краткое', 1, 0)])
        result = run_phase_26_model_bios(path)
        assert result['status'] == 'ok'
        assert result['updated'] >= 1
        os.unlink(path)

    def test_returns_dict(self):
        path = self._make_db()
        result = run_phase_26_model_bios(path)
        assert isinstance(result, dict)
        assert 'status' in result
        os.unlink(path)

    def test_skips_unavailable_models(self):
        path = self._make_db([(1, 'Недост.', 'Москва', 'fashion', '', 0, 0)])
        result = run_phase_26_model_bios(path)
        assert result.get('updated', 0) == 0
        os.unlink(path)

    def test_skips_archived_models(self):
        path = self._make_db([(1, 'Архив', 'Москва', 'fashion', '', 1, 1)])
        result = run_phase_26_model_bios(path)
        assert result.get('updated', 0) == 0
        os.unlink(path)
