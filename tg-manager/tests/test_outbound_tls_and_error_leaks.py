"""Исходящий TLS проверяется, а текст исключения не уходит пользователю.

1. Сессия бота (`_connector_init["ssl"] = False`) и общий HTTP-клиент
   (`CERT_NONE`) ходили наружу без проверки сертификата. Токен бота едет в
   каждом адресе Bot API, токены подключённых ботов и ключи ИИ — через общий
   клиент: любой посредник на пути забирал их целиком.
2. Глобальный обработчик ошибок слал в чат `type(exc).__name__: str(exc)[:200]`
   — туда попадали SQL, пути и куски данных.

Оба теста падают без фикса.
"""
from __future__ import annotations

import ast
import re
import ssl
from pathlib import Path

from services.security import outbound_ssl_context

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")


def test_outbound_context_verifies(monkeypatch):
    monkeypatch.delenv("OUTBOUND_TLS_VERIFY", raising=False)
    ctx = outbound_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_emergency_switch_is_explicit(monkeypatch):
    monkeypatch.setenv("OUTBOUND_TLS_VERIFY", "0")
    assert outbound_ssl_context().verify_mode == ssl.CERT_NONE


def test_main_does_not_disable_tls():
    bad = re.compile(r"ssl\"\]\s*=\s*False|ssl\s*=\s*False|CERT_NONE|check_hostname\s*=\s*False")
    offenders = [ln.strip() for ln in MAIN.splitlines() if bad.search(ln)]
    assert not offenders, offenders
    assert '_connector_init["ssl"] = outbound_ssl_context()' in MAIN
    assert "ssl_ctx = outbound_ssl_context()" in MAIN


def _handler_src() -> str:
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_global_error_handler":
            return ast.get_source_segment(MAIN, node) or ""
    raise AssertionError("_global_error_handler не найден")


def test_error_handler_does_not_echo_exception_text():
    src = _handler_src()
    # Сообщения пользователю собираются в .answer(...): внутри — ни str(exc),
    # ни имени класса исключения.
    for m in re.finditer(r"\.answer\(", src):
        call = src[m.start(): src.index(")\n", m.start())]
        assert "exc_text" not in call and "__name__" not in call and "str(exc)" not in call, call
    assert "exc_text" not in src
