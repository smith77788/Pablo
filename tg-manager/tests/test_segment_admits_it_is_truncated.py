"""Срез контактов, который больше потолка запуска, признаётся урезанным.

ЧТО БЫЛО. Резолвер среза отдавал только список, обрезанный потолком
`_SEG_MAX = 20000`, и превью печатало ЕГО ДЛИНУ как размер сегмента:

    🎯 В сегменте: 20000 · адресуемых в ЛС: 20000

— при пятидесяти тысячах подходящих контактов. Запуск брал те же первые
двадцать тысяч (`ORDER BY id`), тост говорил «Запущено: 20000 получателей», и
ни одна строка нигде не сообщала, что тридцать тысяч человек в рассылку не
попали. Владелец считал сегмент разосланным.

Тот же класс, что `tests/test_miniapp_lists_admit_truncation.py` (урезанный
список обязан признаваться урезанным), но дороже: не «экран соврал», а «треть
аудитории не получила сообщение, и об этом не узнали».

ОБРАТНЫЙ КОНТРОЛЬ. `test_the_old_behaviour_would_fail_here` считает размер
по-старому (длина выдачи) и показывает, что проверка ниже на этом падает, —
иначе она ничего не стережёт.
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import uuid

import pytest

from services.contacts_hub import segments

ROOT = pathlib.Path(__file__).resolve().parents[1]
UI = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

CAP = 50          # потолок в тестах — маленький, чтобы не плодить 20000 строк
TOTAL = 137       # сколько контактов в срезе ВСЕГО


def _rows(n: int) -> list[dict]:
    return [{"id": str(uuid.uuid4()), "username": f"u{i}", "telegram_user_id": 1000 + i,
             "first_name": "И", "last_name": None, "phones": "[]"} for i in range(n)]


class _Pool:
    """Пул, у которого срез БОЛЬШЕ потолка: fetch отдаёт ровно потолок."""

    def __init__(self, total: int, cap: int):
        self.total, self.cap = total, cap
        self.counted = 0

    async def fetch(self, query, *args):
        limit = args[-1] if args else self.cap
        return _rows(min(int(limit), self.total))

    async def fetchval(self, query, *args):
        self.counted += 1
        return self.total

    async def fetchrow(self, query, *args):
        return None


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Резолвер ─────────────────────────────────────────────────────────────────

def test_resolver_reports_the_real_segment_size_not_the_page():
    pool = _Pool(TOTAL, CAP)
    rows, total = _run(segments.resolve(pool, 7, {"favorite": "1"}, limit=CAP))
    assert len(rows) == CAP, "выдача обязана упираться в потолок"
    assert total == TOTAL, (
        f"размер среза взят из длины выдачи ({len(rows)}), а не посчитан в БД")
    assert pool.counted == 1, "потолок достигнут — размер среза обязан быть посчитан"


def test_the_old_behaviour_would_fail_here():
    """Обратный контроль: «total = длина выдачи» проверку выше не проходит."""
    pool = _Pool(TOTAL, CAP)
    rows, _ = _run(segments.resolve(pool, 7, {"favorite": "1"}, limit=CAP))
    old_total = len(rows)          # как считалось раньше
    assert old_total != TOTAL, "обратный контроль сломан: старое и новое совпали"


def test_a_segment_under_the_cap_costs_no_extra_query():
    """Срез кончился раньше потолка — второй запрос ни к чему."""
    pool = _Pool(10, CAP)
    rows, total = _run(segments.resolve(pool, 7, {"favorite": "1"}, limit=CAP))
    assert (len(rows), total) == (10, 10)
    assert pool.counted == 0, "лишний COUNT(*) на каждом маленьком сегменте"


def test_an_explicit_list_is_counted_by_what_was_found():
    ids = [str(uuid.uuid4()) for _ in range(5)]
    pool = _Pool(5, CAP)
    rows, total = _run(segments.resolve(pool, 7, {"contact_ids": ids}, limit=CAP))
    assert (len(rows), total) == (5, 5)
    assert pool.counted == 0


def test_a_broken_contact_id_is_a_request_error_not_an_empty_segment():
    """Мусор в contact_ids раньше уходил в `id = ANY($1::uuid[])`.

    Запрос падал на приведении типа, fail-soft `_safe_fetch` возвращал пустой
    список — и ручка отвечала «в сегменте нет адресуемых контактов», то есть
    ошибка запроса выглядела пустым сегментом.
    """
    pool = _Pool(5, CAP)
    with pytest.raises(ValueError) as exc:
        _run(segments.resolve(pool, 7, {"contact_ids": ["не-uuid"]}, limit=CAP))
    assert "идентификатор" in str(exc.value).lower(), "причина обязана быть по-русски"


def test_duplicate_ids_are_asked_for_once():
    one = str(uuid.uuid4())
    seen = {}

    class _P(_Pool):
        async def fetch(self, query, *args):
            seen["ids"] = args[1]
            return _rows(1)

    _run(segments.resolve(_P(1, CAP), 7, {"contact_ids": [one, one, one]}, limit=CAP))
    assert len(seen["ids"]) == 1, "один и тот же контакт уехал в запрос трижды"


# ── Ручки ────────────────────────────────────────────────────────────────────

def _handler(name: str) -> str:
    """Тело обработчика, в каком бы модуле API-слоя он ни лежал.

    Ручки контактов вынесены из mini_app_api.py в services/mini_app_uch.py;
    привязывать проверку к файлу — значит красить её на каждом переносе.
    """
    import ast

    from tests.miniapp_source import api_files

    for path in api_files():
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"обработчик {name} не найден ни в одном модуле API")


def test_preview_returns_the_segment_size_and_the_remainder():
    src = _handler("uch_segment_preview")
    assert '"total": total' in src, (
        "превью снова печатает длину выдачи как размер сегмента")
    assert '"omitted"' in src and '"resolved"' in src, (
        "превью не говорит, сколько контактов за этот запуск НЕ поедет")
    assert "len(contacts)" not in src.split('"total"')[1].split(",")[0]


@pytest.mark.parametrize("name", ["uch_segment_message", "uch_segment_media",
                                  "uch_segment_invite"])
def test_every_segment_launch_declares_the_remainder(name):
    src = _handler(name)
    assert "_segment_resolve(uid, body)" in src, (
        f"{name} берёт срез в обход резолвера, который знает его размер")
    assert '"omitted"' in src, f"{name} не объявляет, кто не попал в запуск"
    assert '"segment_total": seg_total' in src


def test_a_broken_id_answers_with_a_reason_not_a_five_hundred():
    for name in ("uch_segment_preview", "uch_segment_message",
                 "uch_segment_media", "uch_segment_invite"):
        src = _handler(name)
        assert "except ValueError as exc:" in src and "_err(str(exc), 400)" in src, (
            f"{name}: неверный id контакта отвечает пятисоткой вместо причины")


# ── Экран ────────────────────────────────────────────────────────────────────

def test_the_screen_shows_the_remainder_in_the_preview():
    i = UI.index("🎯 В сегменте:")
    block = UI[i:i + 900]
    assert "p.omitted" in block, "превью на экране молчит про остаток среза"
    assert "p.resolved" in block, "экран не говорит, сколько возьмёт за запуск"


def test_the_launch_toast_names_the_remainder():
    for anchor in ("✉️ Запущено:", "➕ Инвайт сегмента запущен"):
        i = UI.index(anchor)
        assert "r.omitted" in UI[i:i + 400], (
            f"тост «{anchor}» снова читается как «сегмент разослан целиком»")


def test_the_remainder_warning_is_russian():
    i = UI.index("p.omitted>0")
    block = UI[i:i + 400]
    assert re.search(r"[а-яА-Я]{4,}", block), "предупреждение не по-русски"
