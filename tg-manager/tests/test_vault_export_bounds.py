"""Экспорт архива не должен ронять процесс и не должен молча урезаться.

Разрыв: export_data выбирал ВСЕ строки владельца без ограничения, расшифровывал
каждую и собирал из них одну HTML-страницу. Кнопка «Экспорт всего архива» стоит
прямо на экране — на большой переписке это предсказуемый способ исчерпать
память процесса. И если бы выборку когда-нибудь урезали, человек бы об этом не
узнал: он думал бы, что сохранил всю переписку.
"""
from __future__ import annotations

import asyncio
import pathlib

from services import vault_service as V

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")


class _Row(dict):
    def keys(self):
        return super().keys()


def _row(i):
    return _Row({"chat_id": 1, "peer_name": "Боб", "peer_username": "bob",
                 "msg_id": i, "direction": "in", "text_enc": f"m{i}",
                 "media_type": None, "media_name": None, "msg_date": None,
                 "is_deleted": False, "is_edited": False})


class _Pool:
    def __init__(self, n):
        self._all = [_row(i) for i in range(1, n + 1)]
        self.limits = []

    async def fetchval(self, q, *a):
        return len(self._all)

    async def fetch(self, q, *a):
        limit = a[-1]
        self.limits.append(limit)
        rows = list(reversed(self._all)) if "DESC" in q else self._all
        return rows[:limit]


def test_export_is_bounded():
    pool = _Pool(200000)
    res = asyncio.run(V.export_data(pool, 1, limit=1000))
    assert len(res["chats"][0]["messages"]) == 1000
    assert pool.limits and pool.limits[0] <= 1001, "запрос обязан нести LIMIT"


def test_truncation_is_reported():
    res = asyncio.run(V.export_data(_Pool(5000), 1, limit=1000))
    assert res["truncated"] is True
    assert res["exported"] == 1000 and res["total"] == 5000


def test_small_archive_is_not_truncated():
    res = asyncio.run(V.export_data(_Pool(10), 1, limit=1000))
    assert res["truncated"] is False and res["exported"] == 10


def test_exactly_at_limit_is_not_truncated():
    res = asyncio.run(V.export_data(_Pool(1000), 1, limit=1000))
    assert res["truncated"] is False and res["exported"] == 1000


def test_truncated_export_keeps_the_newest():
    """Если всё не влезло, нужнее свежее, а не переписка трёхлетней давности."""
    res = asyncio.run(V.export_data(_Pool(5000), 1, limit=100))
    ids = [m["msg_id"] for m in res["chats"][0]["messages"]]
    assert max(ids) == 5000


def test_export_stays_chronological_in_the_file():
    """Выбираем с конца, но читаться переписка должна сверху вниз."""
    res = asyncio.run(V.export_data(_Pool(50), 1, limit=1000))
    ids = [m["msg_id"] for m in res["chats"][0]["messages"]]
    assert ids == sorted(ids)


def test_empty_archive():
    res = asyncio.run(V.export_data(_Pool(0), 1))
    assert res["chats"] == [] and res["truncated"] is False


# ── Доведено до пользователя ──────────────────────────────────────────────────

def test_html_export_warns_inside_the_file():
    assert "Экспорт неполный" in _API, (
        "предупреждение обязано быть В САМОМ файле — его откроют без интерфейса"
    )


def test_response_headers_flag_truncation():
    assert "X-Export-Truncated" in _API


def test_renderer_reads_the_flag():
    start = _API.index("def _vault_export_html")
    body = _API[start:start + 2500]
    assert 'data.get("truncated")' in body
