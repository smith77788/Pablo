"""Генератор Pyrogram JSON: чистый билдер + round-trip с импортёром."""
from __future__ import annotations

import base64
import json

import pytest

from services import session_export as se


def test_build_pyrogram_json_fields():
    key = bytes(range(256))  # ровно 256 байт
    out = se.build_pyrogram_json(2, key, api_id=12345, user_id=777, is_bot=False)
    d = json.loads(out)
    assert d["dc_id"] == 2
    assert d["api_id"] == 12345
    assert d["user_id"] == 777
    assert d["is_bot"] is False
    assert d["test_mode"] is False
    # auth_key — base64 ровно 256 байт (симметрично импортёру b64decode)
    assert base64.b64decode(d["auth_key"]) == key


def test_build_rejects_bad_auth_key():
    with pytest.raises(se.SessionExportError):
        se.build_pyrogram_json(2, b"tooshort", api_id=1)
    with pytest.raises(se.SessionExportError):
        se.build_pyrogram_json(2, None, api_id=1)


def test_build_rejects_bad_dc():
    with pytest.raises(se.SessionExportError):
        se.build_pyrogram_json(0, bytes(256), api_id=1)


def test_roundtrip_with_importer_shape():
    """JSON, собранный генератором, читается тем же контрактом, что ждёт импортёр
    (dc_id/auth_key/user_id + base64-декодируемый ключ 256 байт)."""
    key = bytes([7]) * 256
    out = se.build_pyrogram_json(4, key, api_id=999, user_id=42)
    d = json.loads(out)
    for req in ("dc_id", "auth_key", "user_id"):
        assert req in d
    assert len(base64.b64decode(d["auth_key"])) == 256
