"""Пока сервер перезапускается, мини-апп получает JSON, а не «starting: …».

ЗАЧЕМ. На время тяжёлого старта порт держит временный сервер (main.py,
_start_bootstrap_health_server). Он отвечал на ВСЁ текстом «starting: <фаза>»
с кодом 200 — и на запросы мини-аппа тоже. Мини-апп принимал это за данные и
показывал владельцу «Ответ сервера не является JSON» на каждом экране, пока
шёл деплой (дашборд здоровья, тост над «Итогами»).

Теперь /api/ получает 503 с `starting: true` — мини-апп понимает, что это
перезапуск, и сам повторяет запрос. Остальным путям (health-check) — 200.
"""
from __future__ import annotations

import json
import pathlib
import re

from aiohttp import web

import main

INDEX = pathlib.Path(__file__).resolve().parent.parent / "mini_app" / "index.html"


def test_api_path_gets_503_json_with_starting_flag():
    resp = main._bootstrap_response(web, "/api/miniapp/health")
    assert resp.status == 503
    assert resp.content_type == "application/json"
    body = json.loads(resp.body)
    assert body["starting"] is True
    assert body["error"] == main.BOOT_API_MESSAGE
    assert re.search("[а-я]", body["error"])  # владельцу — по-русски


def test_health_check_still_gets_200_with_phase():
    resp = main._bootstrap_response(web, "/")
    assert resp.status == 200
    assert resp.text.startswith("starting: ")


def test_miniapp_retries_while_server_boots():
    src = INDEX.read_text(encoding="utf-8")
    assert "async function _serverBooting(r)" in src
    # Ждёт старта и узнаёт оба вида ответа: новый 503 и старый «starting:» с 200.
    assert "j.starting" in src and "/^starting:/" in src
    assert "throw new Error('Ответ сервера не является JSON')" not in src


def test_toast_can_be_closed_and_hides_itself():
    src = INDEX.read_text(encoding="utf-8")
    # Видимый тост ловит палец (раньше pointer-events:none — не закрыть).
    assert re.search(r"#toast\.show\{[^}]*pointer-events:auto", src)
    assert 'class="toast-x" aria-label="Закрыть"' in src
    assert "el.addEventListener('click', hideToast)" in src
    assert "setTimeout(hideToast" in src
    # Длинный текст переносится, а не обрезается за краем экрана.
    assert re.search(r"#toast\{[^}]*white-space:normal", src)
