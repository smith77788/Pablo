"""Пакетный импорт сессий из ZIP: zip-бомба не распаковывается в память.

Архив до 20 МБ проверялся только по размеру самого файла, а каждый .session
читался `zf.read()` целиком. Нули сжимаются в тысячу раз: «.session» на
гигабайты из пары мегабайт архива клал процесс для всех владельцев сразу.

Тест падает без фикса: огромный .session уходил в чтение.
"""
from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

from bot.handlers import accounts


class _Msg:
    def __init__(self, raw: bytes):
        self.answers: list[str] = []
        self.document = SimpleNamespace(file_name="s.zip", file_size=len(raw), file_id="f")

        async def get_file(_fid):
            return SimpleNamespace(file_path="p")

        async def download_file(_p):
            return io.BytesIO(raw)

        self.bot = SimpleNamespace(get_file=get_file, download_file=download_file)

    async def answer(self, text, **_kw):
        self.answers.append(text)
        return self


async def test_oversized_session_in_zip_is_not_read(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.session", b"\0" * (accounts._ZIP_SESSION_MAX_BYTES + 1))
    raw = buf.getvalue()
    assert len(raw) < 100_000

    read: list[str] = []
    orig = zipfile.ZipFile.read

    def _spy(self, name, *a, **kw):
        read.append(name)
        return orig(self, name, *a, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "read", _spy)
    msg = _Msg(raw)
    await accounts.fsm_batch_import_file(msg, SimpleNamespace(), None)
    assert read == []
    assert any("не содержит .session" in a for a in msg.answers), msg.answers
