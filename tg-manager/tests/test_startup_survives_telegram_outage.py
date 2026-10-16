"""Мини-апп обязан подниматься и работать, когда Telegram недоступен.

ЗАЧЕМ ЭТОТ ФАЙЛ. Продукт лежал целиком, и ни один из 3600 тестов этого не видел.
Причина оказалась не в коде функций, а в ПОРЯДКЕ старта: на пути к запуску
HTTP-сервера стоял голый вызов в api.telegram.org, а сам поллинг был единственной
подсистемой без присмотра.

  1. `bot.set_my_commands(...)` — косметическое меню команд — вызывался БЕЗ
     try примерно за сотню строк до старта веб-сервера. Сетевой блип, 429 или
     5xx на стороне Telegram — и процесс умирал ДО того, как мини-апп начнёт
     отвечать.
  2. `dp.start_polling(...)` был голым `await`. Любая ошибка сети завершала
     процесс ВМЕСТЕ с уже поднятым HTTP-сервером.

Дальше — `restartPolicyMaxRetries: 10` в railway.json: десять падений подряд, и
платформа перестаёт перезапускать сервис. Недоступность Telegram на несколько
минут превращается в бессрочно лежащее приложение, хотя ни одна его часть не
сломана. Снаружи: страница мини-аппа открывается (её отдаёт кэш), а все запросы
к данным висят или падают.

ЗДЕСЬ мы поднимаем НАСТОЯЩИЙ процесс с ЗАВЕДОМО НЕРАБОЧИМ токеном (Telegram
отвечает Unauthorized на каждый вызов) и требуем, чтобы мини-апп всё равно
отдавался, а /metrics отвечал. Это дороже юнит-теста и стоит того: дешевле
любого часа простоя.

Нужен живой Postgres (INFRAGRAM_TEST_DSN). Без него файл пропускается.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def _subprocess_can_import(mod: str) -> bool:
    """Импортируется ли модуль в ОТДЕЛЬНОМ процессе (как настоящий `python main.py`).

    Здесь стартует реальный подпроцесс main.py, а он тянет telethon НАПРЯМУЮ.
    In-process заглушка telethon из conftest в подпроцесс НЕ попадает, поэтому
    `pytest.importorskip('telethon')` (видит заглушку) давал ложное «есть» и тест
    падал ошибкой импорта в подпроцессе вместо честного skip. Проверяем реальную
    доступность там, где она и нужна — в подпроцессе."""
    try:
        return subprocess.run([sys.executable, "-c", f"import {mod}"],
                              capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN"),
    pytest.mark.skipif(not _subprocess_can_import("telethon"),
                       reason="стартовому подпроцессу нужен НАСТОЯЩИЙ telethon "
                              "(in-process заглушка conftest в подпроцесс не попадает)"),
    pytest.mark.skipif(not _subprocess_can_import("aiogram"),
                       reason="стартовому подпроцессу нужен aiogram"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url: str, timeout: float = 5.0):
    """(код, сколько байт). Отказ соединения — (None, 0)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, 0
    except Exception:
        return None, 0


@pytest.fixture(scope="module")
def running_app():
    """Настоящий `python main.py` с нерабочим токеном Telegram."""
    port = _free_port()
    env = dict(os.environ)
    env.update({
        # Токен заведомо невалиден: КАЖДЫЙ вызов Telegram вернёт Unauthorized —
        # это и есть моделируемая недоступность.
        "MANAGER_BOT_TOKEN": "1:definitely-invalid",
        "TG_API_ID": "1", "TG_API_HASH": "x", "TOKEN_ENCRYPTION_KEY": "t",
        "DATABASE_URL": re.sub(r"/([^/?]+)\?", "/startup_smoke?", DSN, count=1),
        "PORT": str(port),
        # Фоновые циклы здесь не нужны и только замедляют старт.
        "INFRAGRAM_ROLE": "web",
    })

    import asyncpg, asyncio

    async def _mkdb():
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute("DROP DATABASE IF EXISTS startup_smoke")
            await admin.execute("CREATE DATABASE startup_smoke")
        finally:
            await admin.close()

    asyncio.new_event_loop().run_until_complete(_mkdb())

    proc = subprocess.Popen(
        [sys.executable, "main.py"], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 120
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            pytest.fail(
                "процесс умер на старте, хотя недоступен только Telegram:\n"
                + out[-3000:])
        code, size = _get(base + "/miniapp", timeout=3)
        # Пока идёт инициализация, порт держит заглушка, отвечающая "starting".
        if code == 200 and size > 100_000:
            ready = True
            break
        time.sleep(1)

    if not ready:
        proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        pytest.fail("мини-апп не начал отдаваться за 120с:\n" + out[-3000:])

    yield base, proc

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


# ── ядро ─────────────────────────────────────────────────────────────────────

def test_miniapp_is_served_while_telegram_is_down(running_app):
    """Главное: страница отдаётся, хотя КАЖДЫЙ вызов Telegram падает."""
    base, _proc = running_app
    code, size = _get(base + "/miniapp")
    assert code == 200, f"мини-апп не отдаётся: HTTP {code}"
    assert size > 100_000, (
        f"вместо приложения отдано {size} байт — это заглушка старта, "
        "то есть инициализация не завершилась")


def test_api_answers_instead_of_hanging(running_app):
    """Запрос к API обязан ОТВЕЧАТЬ (пусть отказом), а не висеть.

    Висящий запрос — это то, что пользователь видит как «вечная загрузка».
    """
    base, _proc = running_app
    t0 = time.monotonic()
    code, _ = _get(base + "/api/miniapp/accounts", timeout=10)
    assert code in (401, 403), f"ожидал отказ авторизации, получил {code}"
    assert time.monotonic() - t0 < 10, "ответ пришёл слишком долго"


def test_metrics_are_scrapeable(running_app):
    base, _proc = running_app
    code, size = _get(base + "/metrics")
    assert code == 200 and size > 0


def test_process_stays_alive_through_repeated_polling_failures(running_app):
    """Процесс не должен умирать от того, что Telegram недоступен.

    Именно это и приводило к исчерпанию лимита перезапусков платформы: десять
    падений подряд — и сервис остаётся лежать до ручного деплоя.
    """
    base, proc = running_app
    time.sleep(12)          # успевает пройти несколько неудачных попыток поллинга
    assert proc.poll() is None, "процесс завершился из-за недоступного Telegram"
    assert _get(base + "/miniapp")[0] == 200, "после сбоев поллинга веб перестал отвечать"


# ── храповики на порядок старта ──────────────────────────────────────────────

def _main_src() -> str:
    return open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()


def test_no_unguarded_telegram_call_before_the_web_server():
    """Ни одного голого вызова Telegram до старта HTTP-сервера.

    Проверяем на исходнике: поднять процесс с КАЖДЫМ возможным сбоем Telegram
    в тесте нельзя, а порядок старта — ровно то, что сломалось.
    """
    import ast

    src = _main_src()
    tree = ast.parse(src)
    lines = src.split("\n")

    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "main")
    # Точка, с которой веб уже поднимается.
    web_start = next(i for i, l in enumerate(lines, 1)
                     if "_web_resilient(" in l and "async def" not in l)

    # Собираем `await bot.<...>` внутри main() до этой точки.
    guarded: list[int] = []
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Await):
                    guarded.append(sub.lineno)
    bad = []
    for node in ast.walk(main_fn):
        if not (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)):
            continue
        fn = node.value.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "bot"):
            continue
        if node.lineno >= web_start or node.lineno in guarded:
            continue
        bad.append(f"main.py:{node.lineno}: await bot.{fn.attr}(...)")
    assert not bad, (
        "вызов Telegram без try ДО старта HTTP-сервера:\n  " + "\n  ".join(bad)
        + "\n\nСбой Telegram уронит процесс раньше, чем поднимется мини-апп, "
          "и десять таких падений исчерпают лимит перезапусков платформы."
    )


def test_database_blip_does_not_burn_the_restart_budget():
    """Короткая недоступность БД не должна валить процесс с первой попытки.

    Без повторов процесс падает мгновенно, платформа перезапускает — и он падает
    снова, пока база поднимается. Десять таких падений за секунды исчерпывают
    restartPolicyMaxRetries=10, и сервис остаётся лежать до ручного деплоя,
    хотя база вернулась через минуту.
    """
    src = _main_src()
    assert "_create_pool_resilient" in src, "подключение к БД снова без повторов"
    i = src.index("async def _create_pool_resilient")
    body = src[i:i + 1800]
    assert "for attempt in range" in body and "asyncio.sleep" in body, (
        "повторов с паузой нет — блип базы снова будет тратить перезапуски")
    assert "raise last" in body, (
        "после исчерпания попыток нужно честно упасть: обслуживать запросы нечем")


def test_startup_settings_do_not_kill_the_process():
    """Чтение настроек с БД — не повод потерять весь продукт.

    Эти вызовы стоят ДО старта веб-сервера; любой сбой в них раньше означал, что
    мини-апп не поднимется вовсе.
    """
    import ast

    src = _main_src()
    tree = ast.parse(src)
    lines = src.split("\n")
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "main")
    web_start = next(i for i, l in enumerate(lines, 1)
                     if "_web_resilient(" in l and "async def" not in l)
    guarded = set()
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Await):
                    guarded.add(sub.lineno)
    # Вложенные функции (_resilient, _web_resilient и т.п.) исполняются ПОЗЖЕ и
    # каждая имеет свой присмотр — их тело к порядку старта отношения не имеет.
    for node in ast.walk(main_fn):
        if node is main_fn:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            guarded.update(range(node.lineno, node.end_lineno + 1))
    # Что разрешено оставить голым и почему.
    ALLOWED = {
        "_start_bootstrap_health_server",   # сама ловит свои ошибки
        "_create_pool_resilient",           # повторяет и падает осознанно
        "PostgresFSMStorage",               # create() глотает сбой DDL внутри
    }
    bad = []
    for node in ast.walk(main_fn):
        if not (isinstance(node, ast.Await) and node.lineno < web_start
                and node.lineno not in guarded):
            continue
        text = lines[node.lineno - 1]
        if any(a in text for a in ALLOWED):
            continue
        bad.append(f"main.py:{node.lineno}: {text.strip()[:80]}")
    assert not bad, (
        "голый await до старта HTTP-сервера — его сбой уронит мини-апп:\n  "
        + "\n  ".join(bad))


def test_polling_is_restarted_not_fatal():
    """Поллинг обязан перезапускаться, а не ронять процесс."""
    src = _main_src()
    i = src.index("await dp.start_polling(")
    head = src[max(0, i - 1800):i]
    assert "while True:" in head and "try:" in head, (
        "start_polling снова голый: ошибка сети завершит процесс вместе с "
        "HTTP-сервером")
    tail = src[i:i + 1200]
    assert "except asyncio.CancelledError" in tail, (
        "остановку процесса нельзя путать со сбоем — иначе завершение зависнет")
    assert "asyncio.sleep" in tail, "перезапуск без паузы задолбит Telegram"
