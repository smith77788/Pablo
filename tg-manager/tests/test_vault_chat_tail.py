"""Экран чата должен показывать КОНЕЦ переписки, а не её начало.

Дефект: выборка всегда шла с начала архива по возрастанию с limit=200, а
интерфейс прокручивал результат вниз. В переписке из 500 сообщений человек
видел 200 самых СТАРЫХ, прокрученных в конец, — выглядело как весь чат,
свежие сообщения были недостижимы вовсе, и подгрузки не существовало.
"""
from __future__ import annotations

import asyncio
import pathlib

from services import vault_service as V

_ROOT = pathlib.Path(__file__).resolve().parent.parent


class _Row(dict):
    def keys(self):
        return super().keys()


def _row(i):
    return _Row({"msg_id": i, "direction": "in", "text_enc": f"m{i}",
                 "media_type": None, "media_file_id": None, "media_name": None,
                 "media_size": None, "msg_date": None,
                 "is_deleted": False, "is_edited": False})


class _Pool:
    """Хранит «архив» в хронологическом порядке и уважает ORDER BY/LIMIT/OFFSET."""

    def __init__(self, n):
        self._all = [_row(i) for i in range(1, n + 1)]
        self.last_query = ""

    async def fetch(self, q, *a):
        self.last_query = q
        limit, offset = a[-2], a[-1]
        rows = self._all if "ASC" in q else list(reversed(self._all))
        return rows[offset:offset + limit]


def test_default_returns_the_newest_messages():
    pool = _Pool(500)
    res = asyncio.run(V.list_messages(pool, 1, 7, limit=200, newest_first=True))
    ids = [m["msg_id"] for m in res["messages"]]
    assert ids[-1] == 500, "последним обязано идти самое свежее сообщение"
    assert ids[0] == 301, "показываем последние 200, а не первые"


def test_result_is_always_chronological():
    """Вызывающий не должен думать о направлении выборки."""
    res = asyncio.run(V.list_messages(_Pool(50), 1, 7, limit=10, newest_first=True))
    ids = [m["msg_id"] for m in res["messages"]]
    assert ids == sorted(ids)


def test_offset_steps_back_through_history():
    pool = _Pool(500)
    first = asyncio.run(V.list_messages(pool, 1, 7, limit=200, newest_first=True))
    older = asyncio.run(V.list_messages(pool, 1, 7, limit=200, offset=200, newest_first=True))
    assert [m["msg_id"] for m in older["messages"]][-1] == 300
    ids_new = {m["msg_id"] for m in first["messages"]}
    ids_old = {m["msg_id"] for m in older["messages"]}
    assert not (ids_new & ids_old), "страницы не должны пересекаться"


def test_has_more_flag():
    assert asyncio.run(V.list_messages(_Pool(500), 1, 7, limit=200, newest_first=True))["has_more"]
    assert not asyncio.run(V.list_messages(_Pool(50), 1, 7, limit=200, newest_first=True))["has_more"]


def test_has_more_does_not_leak_the_probe_row():
    """Берём limit+1 строку, чтобы узнать про «есть ещё» — лишняя не должна
    попасть в выдачу."""
    res = asyncio.run(V.list_messages(_Pool(500), 1, 7, limit=200, newest_first=True))
    assert len(res["messages"]) == 200


def test_exactly_full_page_is_not_more():
    res = asyncio.run(V.list_messages(_Pool(200), 1, 7, limit=200, newest_first=True))
    assert len(res["messages"]) == 200 and res["has_more"] is False


def test_ascending_mode_still_available():
    """Экспорт и прочие места вправе читать архив с начала."""
    res = asyncio.run(V.list_messages(_Pool(50), 1, 7, limit=10, newest_first=False))
    assert [m["msg_id"] for m in res["messages"]] == list(range(1, 11))


def test_empty_chat():
    res = asyncio.run(V.list_messages(_Pool(0), 1, 7, newest_first=True))
    assert res["messages"] == [] and res["has_more"] is False


def test_limit_is_bounded():
    pool = _Pool(5000)
    asyncio.run(V.list_messages(pool, 1, 7, limit=999999, newest_first=True))
    assert "LIMIT" in pool.last_query


# ── Доведено до пользователя ──────────────────────────────────────────────────

def test_endpoint_requests_the_tail():
    api = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    start = api.index("async def vault_messages")
    body = api[start:start + 2200]
    assert "newest_first=True" in body
    assert '"has_more"' in body


def test_ui_offers_loading_older_messages():
    ui = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "loadOlderVaultMessages" in ui
    assert "Показать более ранние" in ui
    assert "Начало переписки" in ui, "конец истории должен быть обозначен"


def test_ui_keeps_scroll_position_when_prepending():
    """Дозагрузка не должна «прыгать» под пальцем."""
    ui = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    seg = ui[ui.index("async function loadOlderVaultMessages"):]
    assert "scrollHeight - before" in seg[:1800]
