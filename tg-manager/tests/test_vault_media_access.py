"""Вложения из архива должны открываться, а не быть подписью.

Разрыв: media_file_id писался в vault_messages с самого начала и не отдавался
наружу НИ ОДНИМ эндпоинтом. В архиве была видна подпись «📷 Фото», а самого
файла не существовало ни в одном экране. Для сообщений, которые собеседник
УДАЛИЛ, это ломало главное обещание хранилища — «сохраняем то, что стёрли».
"""
from __future__ import annotations

import asyncio
import io
import pathlib

from services import vault_service as V

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


class _Row(dict):
    def keys(self):
        return super().keys()


class _Pool:
    def __init__(self, row):
        self._row = row
        self.args = None

    async def fetchrow(self, q, *a):
        self.args = a
        return self._row


class _File:
    file_path = "photos/x.jpg"


class _Bot:
    def __init__(self, data=b"binary", fail=False):
        self._data, self._fail = data, fail

    async def get_file(self, file_id):
        if self._fail:
            raise RuntimeError("file is temporarily unavailable")
        return _File()

    async def download_file(self, path):
        return io.BytesIO(self._data)


def _row(**kw):
    base = {"media_file_id": "AgACfileid", "media_type": "photo",
            "media_name": None, "media_size": 1024}
    base.update(kw)
    return _Row(base)


# ── Сервис ────────────────────────────────────────────────────────────────────

def test_returns_bytes_and_mime():
    res = asyncio.run(V.fetch_media(_Pool(_row()), _Bot(b"JPEGDATA"), 1, 5, 7))
    assert res["ok"] and res["data"] == b"JPEGDATA"
    assert res["mime"] == "image/jpeg"
    assert res["filename"].endswith(".jpg")


def test_query_is_owner_scoped():
    """chat_id приходит из URL — без скоупа владельца это чужой архив."""
    pool = _Pool(_row())
    asyncio.run(V.fetch_media(pool, _Bot(), 42, 5, 7))
    assert pool.args == (42, 5, 7)


def test_missing_message_is_404_not_crash():
    res = asyncio.run(V.fetch_media(_Pool(None), _Bot(), 1, 5, 7))
    assert not res["ok"] and res["status"] == 404


def test_message_without_attachment():
    res = asyncio.run(V.fetch_media(_Pool(_row(media_file_id=None)), _Bot(), 1, 5, 7))
    assert not res["ok"] and res["status"] == 404


def test_oversized_file_explained_not_attempted():
    """Bot API физически не отдаёт больше 20 МБ — честно объясняем, а не молчим."""
    res = asyncio.run(V.fetch_media(_Pool(_row(media_size=50 * 1024 * 1024)), _Bot(), 1, 5, 7))
    assert not res["ok"] and res["status"] == 413
    assert "20" in res["error"]


def test_expired_file_is_explained_not_500():
    """У давно удалённых сообщений файл на стороне Telegram мог протухнуть."""
    res = asyncio.run(V.fetch_media(_Pool(_row()), _Bot(fail=True), 1, 5, 7))
    assert not res["ok"] and res["status"] == 410
    assert "Telegram" in res["error"]


def test_empty_download_is_not_reported_as_success():
    res = asyncio.run(V.fetch_media(_Pool(_row()), _Bot(data=b""), 1, 5, 7))
    assert not res["ok"]


def test_mime_per_media_type():
    for mtype, expect in (("voice", "audio/ogg"), ("video", "video/mp4"),
                          ("document", "application/octet-stream")):
        res = asyncio.run(V.fetch_media(_Pool(_row(media_type=mtype)), _Bot(), 1, 5, 7))
        assert res["mime"] == expect, mtype


def test_peer_supplied_filename_is_used():
    res = asyncio.run(V.fetch_media(_Pool(_row(media_name="отчёт.pdf")), _Bot(), 1, 5, 7))
    assert res["filename"] == "отчёт.pdf"


# ── Эндпоинт ──────────────────────────────────────────────────────────────────

def _endpoint() -> str:
    start = _API.index("async def vault_media")
    return _API[start:start + 3500]


def test_route_registered():
    assert '"/api/miniapp/vault/media/{chat_id}/{msg_id}"' in _API


def test_bot_token_never_leaves_the_server():
    """Прямая ссылка Telegram на файл содержит токен бота — отдавать её нельзя."""
    body = _endpoint()
    assert "file_path" not in body and "api.telegram.org" not in body
    assert "body=res[\"data\"]" in body, "файл обязан проксироваться, а не редиректом"


def test_peer_filename_cannot_break_the_header():
    """Имя файла задаёт собеседник: перевод строки или кавычка в заголовке —
    это порча ответа."""
    body = _endpoint()
    assert "ascii_name" in body and "filename*=UTF-8" in body


def test_error_status_is_propagated_not_flattened():
    body = _endpoint()
    assert 'res.get("status")' in body


def test_bot_session_is_closed():
    body = _endpoint()
    assert "session.close()" in body


# ── Интерфейс ─────────────────────────────────────────────────────────────────

def test_ui_opens_media_with_auth_not_a_plain_link():
    """Эндпоинт требует Bearer — простой ссылкой его не открыть."""
    assert "function openVaultMedia" in _UI
    assert "'Authorization': 'Bearer ' + TK" in _UI


def test_ui_frees_the_blob():
    """Иначе каждый просмотр держит файл в памяти вкладки."""
    assert "revokeObjectURL" in _UI


def test_ui_shows_media_only_when_downloadable():
    assert "m.has_media" in _UI


def _func_body(path, name: str) -> str:
    """Тело функции по границам AST, а не по окну фиксированной длины.

    Окно в N символов промахивается, как только функция сдвинулась или подросла:
    отрицательное утверждение «искомого нет» становится правдой само по себе, и
    защита выключается молча — ровно это ловит tests/test_no_silently_disabled_guards.
    """
    import ast

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            lines = src.split("\n")
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"функция {name} не найдена в {path.name}")


def test_service_exposes_has_media_flag_but_not_file_id():
    body = _func_body(_ROOT / "services" / "vault_service.py", "_ui_message")
    assert '"has_media"' in body
    assert '"media_file_id":' not in body, "file_id наружу отдавать незачем"
