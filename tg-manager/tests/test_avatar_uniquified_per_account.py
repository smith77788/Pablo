"""Аватар не должен быть байт-в-байт одинаковым на весь флот.

Аудит F8: массовая установка аватара брала один avatar_url и грузила его как
есть каждому аккаунту — идентичное фото профиля = тривиальная кластеризация
фермы (в отличие от текстовых полей, где есть spintax). Теперь байты аватара
прогоняются через media_uniquifier.uniquify перед загрузкой — каждый аккаунт
получает незаметно-уникальный вариант. Fail-safe: сбой уникализации не срывает
установку.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import profile_setter_engine as pse


class _FakeClient:
    def __init__(self):
        self.uploaded = None

    async def upload_file(self, data, file_name=""):
        self.uploaded = data
        return "FILE"

    async def __call__(self, req):
        return None

    async def disconnect(self):
        return None


def _run(coro):
    return asyncio.run(coro)


async def _immediate(coro, timeout=None):
    return await coro


def test_avatar_from_bytes_uniquified_before_upload():
    client = _FakeClient()
    marker = b"UNIQ"
    with patch.object(pse, "_connect", AsyncMock(return_value=client)), \
         patch("services.media_uniquifier.uniquify", return_value=marker) as uq, \
         patch("asyncio.wait_for", _immediate):
        res = _run(pse.set_avatar_from_bytes("s" * 20, {"id": 1}, b"ORIGINAL"))
    assert res["ok"] is True
    assert uq.call_count == 1
    assert client.uploaded == marker, "загружаться должен уникализированный вариант"


def test_avatar_uniquify_failure_falls_back_to_original():
    client = _FakeClient()
    with patch.object(pse, "_connect", AsyncMock(return_value=client)), \
         patch("services.media_uniquifier.uniquify", side_effect=Exception("boom")), \
         patch("asyncio.wait_for", _immediate):
        res = _run(pse.set_avatar_from_bytes("s" * 20, {"id": 1}, b"ORIGINAL"))
    assert res["ok"] is True
    assert client.uploaded == b"ORIGINAL", "сбой уникализации не срывает установку"


def test_avatar_from_url_path_calls_uniquify():
    import inspect
    src = inspect.getsource(pse.set_avatar_from_url)
    assert "media_uniquifier" in src and "uniquify(" in src
