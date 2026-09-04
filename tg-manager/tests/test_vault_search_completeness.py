"""Поиск по архиву не должен молча врать.

Разрыв: поиск брал ЖЁСТКОЕ окно из 4000 последних сообщений и фильтровал их
после расшифровки. Письмо годичной давности «не находилось», и понять, что
поиск до него просто не дошёл, было нельзя — пользователь делал вывод, что
сообщения нет. Плюс искали только по тексту: найти переписку по имени клиента
(самый частый способ) было невозможно.
"""
from __future__ import annotations

import asyncio
import pathlib

from services import vault_service as V

_ROOT = pathlib.Path(__file__).resolve().parent.parent


class _Row(dict):
    def keys(self):
        return super().keys()


def _msg(i, text=None, peer_name="Боб", peer_username="bob"):
    return _Row({
        "chat_id": 100 + (i % 3), "msg_id": i, "direction": "in",
        # В тестах шифрование сквозное: decrypt_token на «сырой» строке
        # возвращает её же, так что кладём текст как есть.
        "text_enc": text, "media_type": None, "media_name": None,
        "peer_name": peer_name, "peer_username": peer_username,
        "msg_date": None, "is_deleted": False, "is_edited": False,
    })


class _Pool:
    """Отдаёт постранично заданный архив."""

    def __init__(self, messages):
        self._m = messages
        self.pages = 0

    async def fetchval(self, q, *a):
        return len(self._m)

    async def fetch(self, q, *a):
        self.pages += 1
        limit, offset = a[1], a[2]
        return self._m[offset:offset + limit]


def test_finds_message_far_beyond_the_old_4000_window():
    """Ровно тот случай, который раньше молча не находился."""
    archive = [_msg(i) for i in range(5000)]
    archive[4800] = _msg(4800, text="договор на поставку")
    pool = _Pool(archive)
    res = asyncio.run(V.search_messages(pool, 1, "договор"))
    assert len(res["results"]) == 1
    assert res["results"][0]["msg_id"] == 4800
    assert pool.pages > 1, "поиск обязан идти страницами, а не одним окном"


def test_search_matches_peer_name():
    """Искать переписку по имени клиента — самый частый способ."""
    archive = [_msg(i, peer_name="Иван Петров", peer_username="ivanp") for i in range(3)]
    res = asyncio.run(V.search_messages(_Pool(archive), 1, "петров"))
    assert len(res["results"]) == 3


def test_search_matches_username():
    archive = [_msg(1, peer_name=None, peer_username="acme_corp")]
    res = asyncio.run(V.search_messages(_Pool(archive), 1, "acme"))
    assert len(res["results"]) == 1


def test_search_is_case_insensitive():
    archive = [_msg(1, text="Срочно НУЖЕН счёт")]
    assert len(asyncio.run(V.search_messages(_Pool(archive), 1, "нужен"))["results"]) == 1


def test_complete_flag_true_when_whole_archive_scanned():
    res = asyncio.run(V.search_messages(_Pool([_msg(i) for i in range(10)]), 1, "нет"))
    assert res["complete"] is True
    assert res["scanned"] == 10 and res["total"] == 10


def test_complete_flag_false_when_cap_reached():
    """Главное: «ничего не найдено» на неполном просмотре должно быть помечено."""
    archive = [_msg(i) for i in range(20000)]
    res = asyncio.run(V.search_messages(_Pool(archive), 1, "нетакого", max_scan=4000))
    assert res["results"] == []
    assert res["complete"] is False
    assert res["scanned"] == 4000 and res["total"] == 20000


def test_stops_early_once_limit_of_hits_reached():
    """Набрав лимит совпадений, дальше жечь CPU незачем."""
    archive = [_msg(i, text="счёт") for i in range(5000)]
    res = asyncio.run(V.search_messages(_Pool(archive), 1, "счёт", limit=10))
    assert len(res["results"]) == 10
    assert res["complete"] is True, "лимит набран — результат не вводит в заблуждение"
    assert res["scanned"] < 1000, "не должен сканировать весь архив ради 10 совпадений"


def test_empty_query_returns_nothing_without_scanning():
    pool = _Pool([_msg(i) for i in range(100)])
    res = asyncio.run(V.search_messages(pool, 1, "  "))
    assert res["results"] == [] and res["scanned"] == 0 and pool.pages == 0


def test_empty_archive():
    res = asyncio.run(V.search_messages(_Pool([]), 1, "что-то"))
    assert res["results"] == [] and res["complete"] is True


def test_messages_without_text_do_not_crash():
    """Вложение без подписи: text_enc = NULL."""
    archive = [_msg(1, text=None), _msg(2, text="есть")]
    res = asyncio.run(V.search_messages(_Pool(archive), 1, "есть"))
    assert len(res["results"]) == 1


# ── Доведено до пользователя ──────────────────────────────────────────────────

def test_endpoint_exposes_completeness():
    api = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    start = api.index("async def vault_search")
    body = api[start:start + 1600]
    for field in ('"complete"', '"scanned"', '"archive_total"'):
        assert field in body, field


def test_ui_says_when_search_was_partial():
    ui = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    seg = ui[ui.index("async function vaultDoSearch"):]
    seg = seg[:2200]
    assert "d.complete === false" in seg
    assert "Просмотрено" in seg, "неполный просмотр обязан быть виден пользователю"
