"""Секреты не должны попадать ни в логи, ни в текст, который видит человек."""
from __future__ import annotations

from services.secret_masking import mask_bot_token, mask_session, redact_secrets

TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
SESSION = "1" + "AbCdEf0123456789" * 8


def test_bot_token_keeps_id_and_hides_secret():
    masked = mask_bot_token(TOKEN)
    assert masked == "1234567890:***"
    assert "AAHdqTcvCH" not in masked
    # Хвост токена — это прямо кусок ключа, его тоже не должно быть.
    assert TOKEN[-8:] not in masked


def test_bot_token_garbage_is_fully_masked():
    assert mask_bot_token("") == "***"
    assert mask_bot_token(None) == "***"
    assert mask_bot_token("не токен") == "***"
    # Похожее на токен, но короткое, целиком под маской.
    assert mask_bot_token("123:short") == "***"


def test_session_string_is_never_partially_shown():
    assert mask_session(SESSION) == "***"
    assert SESSION[:20] not in mask_session(SESSION)


def test_redact_removes_token_from_arbitrary_text():
    text = f"getMe failed for token={TOKEN}: Unauthorized"
    out = redact_secrets(text)
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in out
    assert "1234567890:***" in out
    assert "Unauthorized" in out, "полезная часть сообщения должна остаться"


def test_redact_removes_session_string():
    out = redact_secrets(f"session={SESSION} is dead")
    assert SESSION not in out
    assert "***" in out and "is dead" in out


def test_redact_hides_password_in_dsn_and_proxy():
    out = redact_secrets(
        "connect failed: postgres://app:s3cr3t@db.internal:5432/infragram")
    assert "s3cr3t" not in out
    assert "postgres://app:***@" in out

    out2 = redact_secrets("proxy socks5://user7:pAssw0rd@1.2.3.4:1080 dead")
    assert "pAssw0rd" not in out2
    assert "socks5://user7:***@" in out2


def test_redact_is_safe_on_empty_and_long_input():
    assert redact_secrets(None) == ""
    assert redact_secrets("") == ""
    assert len(redact_secrets("x" * 10000)) <= 2000


def test_redact_leaves_ordinary_text_alone():
    msg = "Аккаунт 42 не найден, попробуйте ещё раз"
    assert redact_secrets(msg) == msg


# ── Узкие места: форматтер логов и ответ API ────────────────────────────────

def test_log_formatter_scrubs_token_from_any_line(caplog):
    import logging

    from services.logger import _StructuredFormatter

    rec = logging.LogRecord(
        "svc.test", logging.WARNING, __file__, 1,
        "getMe failed for token=%s", (TOKEN,), None)
    out = _StructuredFormatter(use_json=False).format(rec)
    assert TOKEN not in out
    assert "1234567890:***" in out


def test_log_formatter_json_scrubs_message_and_exception():
    import json
    import logging

    from services.logger import _StructuredFormatter

    try:
        raise RuntimeError(f"connect failed: postgres://app:s3cr3t@db:5432/x")
    except RuntimeError:
        import sys
        exc = sys.exc_info()
    rec = logging.LogRecord(
        "svc.test", logging.ERROR, __file__, 1,
        "session=%s died", (SESSION,), exc)
    out = json.loads(_StructuredFormatter(use_json=True).format(rec))
    assert SESSION not in out["msg"]
    assert "s3cr3t" not in out["exc"]


def test_log_formatter_leaves_short_lines_untouched():
    import logging

    from services.logger import _StructuredFormatter

    rec = logging.LogRecord("svc.test", logging.INFO, __file__, 1,
                            "готово: 5 аккаунтов", (), None)
    assert "готово: 5 аккаунтов" in _StructuredFormatter(use_json=False).format(rec)


def test_api_error_response_scrubs_secrets():
    import json as _json

    from services.mini_app_api import _err

    resp = _err(f"asyncpg: postgres://app:s3cr3t@db:5432/infragram unreachable", 500)
    body = _json.loads(resp.text)
    assert "s3cr3t" not in body["error"]
    assert "postgres://app:***@" in body["error"]

    resp2 = _err(f"Telethon отверг сессию {SESSION}", 500)
    assert SESSION not in _json.loads(resp2.text)["error"]


def test_api_error_keeps_paywall_marker():
    """Чистка не должна ломать разметку пейволла (её читает фронт)."""
    import json as _json

    from services.mini_app_api import _err

    body = _json.loads(_err("Нужна подписка", 403).text)
    assert body.get("paywall") is True
    assert body.get("code") == "subscription_required"
