"""D19: токен сессии мини-аппа не ходит в строке запроса.

ЧТО ЛОМАЛОСЬ. `_get_uid` принимал `?token=` (SSE, выгрузки, `downloadFile`),
а access-лог aiohttp пишет строку запроса целиком. Двухчасовой ключ от
аккаунта владельца оседал в логах платформы, истории браузера и прокси.

ЧТО ТЕПЕРЬ. Ссылкам без заголовка выдаётся билет на 2 минуты в своём домене
подписи; `?token=` сервер не принимает; access-лог маскирует значения
секретных параметров; клиент больше не кладёт TK в адрес.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from services import mini_app_api as M

# Настоящая функция, взятая при сборе: соседние тесты в общем прогоне
# подменяют M._get_uid мимо monkeypatch, и к нашему запуску там чужая лямбда.
_REAL_GET_UID = M._get_uid
from services.mini_app_auth import (
    URL_TICKET_TTL, make_token, make_url_ticket, parse_token, parse_url_ticket,
)
from services.secret_masking import RedactingAccessLogger, redact_query

BOT = "123456789:AAEhBP0abcdefghijklmnopqrstuvwxyz12345678"
UID = 424242
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _bot_token(monkeypatch):
    monkeypatch.setattr(M, "_bot_token", lambda: BOT)


def _req(path: str, headers: dict | None = None):
    return make_mocked_request("GET", path, headers=headers or {})


def test_ticket_roundtrip():
    assert parse_url_ticket(make_url_ticket(UID, BOT), BOT) == UID


def test_ticket_expires_fast(monkeypatch):
    t = make_url_ticket(UID, BOT)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + URL_TICKET_TTL + 5)
    assert parse_url_ticket(t, BOT) is None


def test_ticket_and_session_token_are_not_interchangeable():
    assert parse_token(make_url_ticket(UID, BOT), BOT) is None
    assert parse_url_ticket(make_token(UID, BOT), BOT) is None


def test_forged_ticket_rejected():
    t = make_url_ticket(UID, BOT)
    prefix, uid, ts, sig = t.split(".")
    assert parse_url_ticket(f"{prefix}.{UID + 1}.{ts}.{sig}", BOT) is None


def test_session_token_in_query_is_refused():
    tok = make_token(UID, BOT)
    assert _REAL_GET_UID(_req(f"/api/miniapp/events?token={tok}")) is None


def test_ticket_in_query_and_bearer_header_are_accepted():
    assert _REAL_GET_UID(_req(f"/x?ticket={make_url_ticket(UID, BOT)}")) == UID
    tok = make_token(UID, BOT)
    assert _REAL_GET_UID(_req("/x", {"Authorization": f"Bearer {tok}"})) == UID


def test_redact_query_masks_secret_values_only():
    out = redact_query("/api/x?token=abc&ticket=t1.1.2.3&page=2&format=csv")
    assert "abc" not in out and "t1.1.2.3" not in out
    assert "page=2" in out and "format=csv" in out
    assert redact_query("/plain") == "/plain"


def test_access_logger_never_writes_the_ticket():
    lines: list[str] = []

    class _L:
        def info(self, fmt, *args):
            lines.append(fmt % args)

    class _Resp:
        status = 200
        body_length = 10

    ticket = make_url_ticket(UID, BOT)
    RedactingAccessLogger(_L(), "").log(
        _req(f"/api/miniapp/events?ticket={ticket}"), _Resp(), 0.01)
    assert lines and ticket not in lines[0]


def test_http_server_uses_the_redacting_logger():
    src = (ROOT / "services" / "payment_webhook.py").read_text(encoding="utf-8")
    assert "access_log_class=RedactingAccessLogger" in src


def test_client_never_puts_the_session_token_in_a_url():
    ui = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    screens = "".join(p.read_text(encoding="utf-8")
                      for p in (ROOT / "mini_app" / "screens").glob("*.js"))
    hits = re.findall(r"token='\s*\+\s*encodeURIComponent\(TK\)|[?&]token=", ui + screens)
    assert not hits, f"токен сессии снова уходит в адрес: {hits[:3]}"


def test_ticket_cannot_buy_a_new_ticket():
    """Утёкшая ссылка не должна продлевать себя бесконечно."""
    src = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    i = src.index("async def url_ticket(")
    body = src[i:i + 1200]
    assert 'request.query.get("ticket")' in body and "401" in body
