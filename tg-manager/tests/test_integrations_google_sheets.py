"""Google Sheets: разбор ссылки, ключа сервисного аккаунта и подпись JWT.

Сеть к Google здесь не нужна: JWT проверяется открытым ключом той же пары, что
его подписала, — ровно так его проверит oauth2.googleapis.com.
"""
from __future__ import annotations

import base64
import json

import pytest

from services import google_sheets as gs


def _sa(**over):
    from Crypto.PublicKey import RSA
    key = RSA.generate(2048)
    info = {"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com",
            "private_key": key.export_key().decode(), "private_key_id": "kid1",
            "project_id": "proj"}
    info.update(over)
    return info, key


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_spreadsheet_id_from_link_or_raw():
    sid = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    assert gs.parse_spreadsheet_id(f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid=0") == sid
    assert gs.parse_spreadsheet_id(sid) == sid
    assert gs.parse_spreadsheet_id("  " + sid + " ") == sid
    assert gs.parse_spreadsheet_id("https://example.com/") is None
    assert gs.parse_spreadsheet_id("") is None
    assert gs.parse_spreadsheet_id("'; DROP") is None


def test_service_account_validation_speaks_russian():
    with pytest.raises(ValueError, match="JSON"):
        gs.parse_service_account("не json")
    with pytest.raises(ValueError, match="СЕРВИСНОГО"):
        gs.parse_service_account(json.dumps({"type": "authorized_user"}))
    info, _ = _sa(client_email="")
    with pytest.raises(ValueError, match="client_email"):
        gs.parse_service_account(info)
    info, _ = _sa(private_key="-----BEGIN PRIVATE KEY-----\nмусор\n-----END PRIVATE KEY-----")
    with pytest.raises(ValueError, match="private_key"):
        gs.parse_service_account(info)


def test_parsed_account_keeps_only_what_is_needed():
    info, _ = _sa(client_secret="лишнее", token_uri="https://evil.example/token")
    out = gs.parse_service_account(json.dumps(info))
    assert set(out) == {"type", "client_email", "private_key", "private_key_id", "project_id"}


def test_jwt_is_rs256_signed_for_sheets_scope():
    from Crypto.Hash import SHA256
    from Crypto.Signature import pkcs1_15
    info, key = _sa()
    tok = gs.build_jwt(gs.parse_service_account(info), now=1_000)
    h, c, s = tok.split(".")
    header, claims = json.loads(_b64d(h)), json.loads(_b64d(c))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "kid1"}
    assert claims["iss"] == info["client_email"]
    assert claims["scope"] == "https://www.googleapis.com/auth/spreadsheets"
    assert claims["aud"] == "https://oauth2.googleapis.com/token"
    assert claims["exp"] - claims["iat"] == 3600
    pkcs1_15.new(key.publickey()).verify(SHA256.new(f"{h}.{c}".encode()), _b64d(s))


def test_token_url_is_fixed_not_taken_from_the_key():
    """token_uri из присланного ключа не используется: иначе подменённый ключ
    отправил бы подписанный JWT на чужой адрес."""
    src = open(gs.__file__, encoding="utf-8").read()
    assert 'info.get("token_uri")' not in src and "info['token_uri']" not in src


def test_event_row_layout():
    import datetime as dt
    row = gs.event_row("operation.done", 9, dt.datetime(2026, 11, 1, 3, 4, 5, tzinfo=dt.timezone.utc),
                       "Операция выполнена #9", {"a": 1})
    assert row[0] == "2026-11-01 03:04:05"
    assert row[1] == "Операция выполнена"
    assert row[2] == "Операция выполнена #9"
    assert json.loads(row[3]) == {"a": 1}
    assert row[4:] == [9, "operation.done"]
    assert len(row) == len(gs.EVENT_HEADER)


def test_sheet_name_is_quoted_for_a1():
    assert gs._q("Лист") == "'Лист'"
    assert gs._q("Bob's") == "'Bob''s'"


def test_access_errors_tell_what_to_do():
    msg = gs._explain(403, {}, "bot@proj.iam.gserviceaccount.com")
    assert "bot@proj.iam.gserviceaccount.com" in msg and "редактор" in msg
    assert "ссылку" in gs._explain(404, {})


def test_no_shared_platform_account():
    """Общий сервисный аккаунт платформы дал бы любому владельцу запись в
    таблицу другого владельца, открытую этому аккаунту."""
    src = open(gs.__file__, encoding="utf-8").read()
    assert "os.getenv" not in src and "os.environ" not in src
