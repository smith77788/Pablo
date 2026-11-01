"""D19, вторая половина: секреты из строки запроса не оседают в access-логе.

Пропуск (`?ticket=`) живёт две минуты, но пишется в журнал доступа так же,
как писался токен; туда же попадает всё, что клиент по старой памяти кладёт
в адрес. aiohttp по умолчанию пишет строку запроса целиком, поэтому сервер
пишет access-лог через `RedactingAccessLogger`: значения секретных
параметров заменены на «***».

И отдельный случай, найденный этим проходом: выгрузка контактов клала токен
сессии в адрес (`params.set('token', TK)`) — мимо обоих прежних храповиков,
которые искали только `token=` в строке. После того как сервер перестал
принимать `?token=`, кнопки выгрузки отвечали 401.
"""
from __future__ import annotations

import re
from pathlib import Path

from aiohttp.test_utils import make_mocked_request

from services.secret_masking import RedactingAccessLogger, redact_query

ROOT = Path(__file__).resolve().parent.parent


def test_redact_query_masks_secret_values_only():
    out = redact_query("/api/x?token=abc&ticket=t1.2.3&page=2&format=csv")
    assert "abc" not in out and "t1.2.3" not in out
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

    secret = "u1.9999999999.deadbeefcafe"
    RedactingAccessLogger(_L(), "").log(
        make_mocked_request("GET", f"/api/miniapp/events?ticket={secret}"),
        _Resp(), 0.01)
    assert lines and secret not in lines[0]
    assert "/api/miniapp/events" in lines[0]


def test_http_server_uses_the_redacting_logger():
    src = (ROOT / "services" / "payment_webhook.py").read_text(encoding="utf-8")
    assert "access_log_class=RedactingAccessLogger" in src


def test_no_url_gets_the_session_token_as_a_param():
    """`params.set('token', TK)` / `{token: TK}` в адресе — тот же класс."""
    ui = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    screens = "".join(p.read_text(encoding="utf-8")
                      for p in (ROOT / "mini_app" / "screens").glob("*.js"))
    hits = re.findall(r"""\.set\(\s*['"]token['"]\s*,\s*TK\s*\)""", ui + screens)
    assert not hits, f"токен сессии снова уходит в адрес: {hits[:3]}"
