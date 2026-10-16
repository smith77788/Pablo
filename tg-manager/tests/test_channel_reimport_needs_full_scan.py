"""Обрезанный скан каналов не должен стирать то, до чего не дочитал.

`upsert_managed_channels` — «полный ре-импорт»: она удаляет строки аккаунта,
которых нет в переданном списке. Это верно ровно до тех пор, пока список
действительно исчерпывающий.

Источник списка — `account_manager.scan_owned_assets`, а он читает
ограниченное число диалогов. Диалог — это не только канал: у рабочего
аккаунта каждая личная переписка тоже диалог, так что лимит упирается
быстро. Всё, что не попало в срез, считалось «канала больше нет» и удалялось
из базы: канал остаётся в Telegram, но продукт про него забывает — он
пропадает из списков, рассылок и операций, и заметно это становится на
следующей рассылке. Скан, упавший на полпути, давал то же самое: пустой
список воспринимался как «каналов нет».

Теперь скан честно помечает обрыв (`truncated`), а удалять строки можно
только по исчерпывающему списку — `complete=True` обязателен и не имеет
значения по умолчанию, так что забыть его нельзя.

Отдельно: раньше полный ре-импорт сносил строки и вписывал их заново, теряя
число участников (те самые «0 участников» у всех каналов). Теперь уцелевшие
строки обновляются, а не пересоздаются.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os

import pytest

from database import db

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakePool:
    def __init__(self):
        self.log: list[str] = []

    class _Conn:
        def __init__(self, log):
            self._log = log

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, *a):
            self._log.append(sql)
            return "DELETE 0"

        async def executemany(self, sql, rows):
            self._log.append(sql)
            self.rows = rows

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                pool._conn = _FakePool._Conn(pool.log)
                return pool._conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()

    async def executemany(self, sql, rows):
        self.log.append(sql)
        self.rows = rows


def _channels(*ids):
    return [{"id": i, "title": f"ch{i}", "members": 10 * i} for i in ids]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Неполный список ничего не удаляет ───────────────────────────────────────


def test_incomplete_list_deletes_nothing():
    pool = _FakePool()
    _run(db.upsert_managed_channels(
        pool, owner_id=1, acc_id=10, channels=_channels(1, 2), complete=False))

    assert not [q for q in pool.log if q.strip().upper().startswith("DELETE")], (
        "по обрезанному скану удалили строки — каналы, до которых не дочитали, "
        "пропадут из продукта"
    )
    assert any("INSERT INTO managed_channels" in q for q in pool.log), (
        "переданные каналы всё равно должны сохраниться"
    )


def test_complete_flag_is_mandatory():
    """Забыть ответ на «список полный?» нельзя — иначе удаление случайно."""
    sig = inspect.signature(db.upsert_managed_channels)
    p = sig.parameters["complete"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty, (
        "значение по умолчанию вернёт тихое удаление по неполному списку"
    )
    with pytest.raises(TypeError):
        _run(db.upsert_managed_channels(_FakePool(), 1, 10, _channels(1)))


def test_complete_list_spares_the_channels_it_carries():
    pool = _FakePool()
    _run(db.upsert_managed_channels(
        pool, owner_id=1, acc_id=10, channels=_channels(1, 2), complete=True))

    deletes = [q for q in pool.log if q.strip().upper().startswith("DELETE")]
    assert len(deletes) == 1
    assert "NOT (channel_id = ANY" in deletes[0], (
        "снос всех строк с последующей вставкой теряет накопленные поля строки"
    )


def test_reimport_does_not_reset_member_counts():
    """Число участников переживает полный ре-импорт."""
    pool = _FakePool()
    _run(db.upsert_managed_channels(
        pool, owner_id=1, acc_id=10, channels=_channels(1), complete=True))

    inserts = [q for q in pool.log if "INSERT INTO managed_channels" in q]
    assert inserts, "полный ре-импорт обязан записать каналы"
    assert "members_count" in inserts[0], (
        "ре-импорт не пишет members_count — у всех каналов станет 0 участников"
    )


# ── Скан сообщает об обрыве, вызывающие это учитывают ───────────────────────


def test_scan_reports_truncation():
    from services.account_manager import _OWNED_SCAN_DIALOG_LIMIT, scan_was_truncated

    assert scan_was_truncated(_OWNED_SCAN_DIALOG_LIMIT) is True
    assert scan_was_truncated(_OWNED_SCAN_DIALOG_LIMIT - 1) is False
    assert scan_was_truncated(None) is True, "неизвестность — не повод удалять"


def test_failed_scan_is_never_treated_as_complete():
    src = inspect.getsource(__import__("services.account_manager", fromlist=["x"]).scan_owned_assets)
    assert '"truncated": True' in src, (
        "упавший скан обязан помечаться обрывом: пустой список иначе прочтут "
        "как «каналов нет» и сотрут все строки аккаунта"
    )


def _calls_with_complete(path: str) -> list[ast.Call]:
    src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
    out = []
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name == "upsert_managed_channels":
            out.append(n)
    return out


@pytest.mark.parametrize("path", ["bot/handlers/channel_ops.py"])
def test_every_caller_answers_the_completeness_question(path):
    calls = _calls_with_complete(path)
    assert calls, f"{path}: вызовов не нашлось — тест устарел"
    for c in calls:
        kw = {k.arg for k in c.keywords}
        assert "complete" in kw, (
            f"{path}:{c.lineno} — полный ре-импорт без ответа «список полный?»"
        )
        arg = next(k.value for k in c.keywords if k.arg == "complete")
        text = ast.unparse(arg)
        assert "truncated" in text, (
            f"{path}:{c.lineno} — полнота объявлена без учёта обрыва скана"
        )
