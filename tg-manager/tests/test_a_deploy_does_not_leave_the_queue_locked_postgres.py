"""Деплой обязан отпустить очередь и флот, а не ждать истечения аренды.

ЗАЧЕМ. Ветка едет на Railway, то есть пуш в неё означает прод, а каждый выпуск
это SIGTERM работающему процессу. Воркер к этому подготовлен: `op_worker.shutdown`
возвращает свои операции в очередь с нетронутым бюджетом живучести, снимает
аренду аккаунтов и отпускает аренду исполнителя, чтобы следующий процесс взял
очередь сразу, а не через полторы минуты.

Но вся эта уборка висит на одной тонкой цепочке: SIGTERM → обработчик сигнала
поднимает KeyboardInterrupt → `asyncio.run` отменяет главную задачу → блок
`finally` в main() успевает ДОждаться `op_worker.shutdown(pool)` и только потом
закрыть пул. Ни одно звено этой цепочки не проверялось: тесты звали `shutdown`
напрямую, то есть подтверждали, что уборка умеет работать, а не что она вообще
случается на деплое. Сломайся любое звено — уборка просто не выполнялась бы, и
снаружи это выглядело бы как «после каждого выпуска флот занят, а очередь стоит»,
без единой ошибки в логах.

ПОЧЕМУ НАСТОЯЩИЙ ПРОЦЕСС. Отмена задач, порядок finally и доставка сигнала — это
поведение рантайма, а не кода функций: в одном процессе с pytest ни сигнал, ни
`asyncio.run` воспроизвести нельзя. Поэтому здесь поднимается НАСТОЯЩИЙ
`python main.py` (с заведомо нерабочим токеном Telegram), ему посылается SIGTERM
и проверяется ОТПУЩЕННАЯ АРЕНДА в базе — наблюдаемый след того, что уборка
доработала до конца.

Нужен живой Postgres (INFRAGRAM_TEST_DSN) и настоящий telethon в подпроцессе.
Без них файл пропускается.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import re
import signal
import socket
import subprocess
import sys
import time

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def _subprocess_can_import(mod: str) -> bool:
    """Импортируется ли модуль в ОТДЕЛЬНОМ процессе (как настоящий `python main.py`).

    Заглушка telethon из conftest живёт в процессе pytest и в подпроцесс не
    попадает, поэтому importorskip здесь дал бы ложное «есть».
    """
    try:
        return subprocess.run([sys.executable, "-c", f"import {mod}"],
                              capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


# Скипы навешены на подпроцессный тест, а не на весь файл: храповики ниже
# читают исходник и обязаны работать в любом прогоне — иначе проводку, которую
# они охраняют, снова будет некому проверить.
_needs_subprocess = [
    pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN"),
    pytest.mark.skipif(not _subprocess_can_import("telethon"),
                       reason="стартовому подпроцессу нужен НАСТОЯЩИЙ telethon "
                              "(in-process заглушка conftest в подпроцесс не попадает)"),
    pytest.mark.skipif(not _subprocess_can_import("aiogram"),
                       reason="стартовому подпроцессу нужен aiogram"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "deploy_shutdown_smoke"
_DSN_DB = re.sub(r"/([^/?]+)\?", f"/{DB}?", DSN, count=1)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _fresh_db() -> None:
    import asyncpg

    admin = await asyncpg.connect(DSN)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {DB}")
        await admin.execute(f"CREATE DATABASE {DB}")
    finally:
        await admin.close()


async def _lease_holder() -> "str | None":
    """Кто сейчас держит аренду исполнителя (NULL — очередь свободна)."""
    import asyncpg

    conn = await asyncpg.connect(_DSN_DB)
    try:
        return await conn.fetchval(
            "SELECT worker_id FROM executor_lease WHERE id = 1 AND expires_at > now()")
    except Exception:
        return None            # таблицы ещё нет — аренду не брали
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def worker_process():
    """Настоящий `python main.py` в роли «всё», взявший аренду исполнителя."""
    _run(_fresh_db())
    env = dict(os.environ)
    env.update({
        # Токен заведомо невалиден: Telegram отвечает Unauthorized на каждый
        # вызов. Для этого теста важно только то, что процесс живёт и работает
        # с базой — ровно как при недоступном Telegram.
        "MANAGER_BOT_TOKEN": "1:definitely-invalid",
        "TG_API_ID": "1", "TG_API_HASH": "x", "TOKEN_ENCRYPTION_KEY": "t",
        "DATABASE_URL": _DSN_DB,
        "PORT": str(_free_port()),
        # Роль по умолчанию («всё»): именно она берёт аренду исполнителя.
        "INFRAGRAM_ROLE": "all",
    })
    proc = subprocess.Popen(
        [sys.executable, "main.py"], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    holder = None
    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            pytest.fail("процесс умер на старте:\n" + out[-3000:])
        holder = _run(_lease_holder())
        if holder:
            break
        time.sleep(1)
    if not holder:
        proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        pytest.fail("воркер не взял аренду исполнителя за 180с:\n" + out[-3000:])

    yield proc, holder

    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=15)


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
@pytest.mark.skipif(not _subprocess_can_import("telethon"),
                    reason="стартовому подпроцессу нужен НАСТОЯЩИЙ telethon")
@pytest.mark.skipif(not _subprocess_can_import("aiogram"),
                    reason="стартовому подпроцессу нужен aiogram")
def test_sigterm_releases_the_queue_for_the_next_process(worker_process):
    """После SIGTERM аренда исполнителя свободна — следующий процесс берёт очередь."""
    proc, holder = worker_process

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("процесс не завершился за 90с после SIGTERM: уборка висит, "
                    "а платформа убьёт контейнер по своему таймауту")

    out = proc.stdout.read() if proc.stdout else ""

    assert _run(_lease_holder()) is None, (
        f"аренда исполнителя осталась за {holder}: следующий процесс после "
        "деплоя не возьмёт очередь, пока она не истечёт по TTL — выпуск "
        "останавливает все операции продукта на эту паузу")
    assert "op_worker shutdown:" in out, (
        "в выводе нет итоговой строки уборки: значит `finally` в main() не "
        "дождался op_worker.shutdown — операции остались сиротами, а аккаунты "
        f"занятыми до истечения TTL\n{out[-3000:]}")
    assert "Корректная остановка воркера операций не удалась" not in out, (
        f"уборка упала с исключением:\n{out[-3000:]}")


# ── Храповик на проводку: он идёт в любом прогоне, без базы и подпроцесса ────



def _polling_call() -> "ast.Call":
    """Вызов dp.start_polling в main.py."""
    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "start_polling"):
            return node
    raise AssertionError("в main.py нет вызова start_polling — проводка изменилась")


def test_signals_are_not_handed_to_aiogram():
    """handle_signals=False обязателен: иначе SIGTERM не завершает процесс.

    По умолчанию aiogram ставит свой обработчик SIGTERM и ЗАМЕЩАЕТ наш. Его
    обработчик только останавливает поллинг, а для процесса это штатный выход
    из start_polling — цикл поднимал поллинг заново, KeyboardInterrupt не
    поднимался, блок finally не выполнялся, и уборка (возврат операций,
    снятие аренды аккаунтов и исполнителя) не случалась НИ РАЗУ.
    """
    kw = {k.arg: k.value for k in _polling_call().keywords}
    assert "handle_signals" in kw, (
        "start_polling зовётся без handle_signals=False — обработчик SIGTERM "
        "снова заберёт aiogram, и деплой перестанет завершать процесс")
    assert isinstance(kw["handle_signals"], ast.Constant) and kw["handle_signals"].value is False, (
        "handle_signals должен быть ровно False")


def test_a_graceful_polling_stop_does_not_restart_polling():
    """Штатный выход из поллинга означает остановку процесса, а не перезапуск.

    Единственный способ выйти из start_polling без исключения — осознанная
    остановка (dp.stop_polling). Если цикл в этом случае запускает поллинг
    заново, процесс не сворачивается никогда — тот же самый разрыв, но уже без
    участия сигналов.
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    loop = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.While,)) and any(
                isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                and x.func.attr == "start_polling" for x in ast.walk(node)):
            loop = node
            break
    assert loop is not None, "вызов start_polling вне цикла — проводка изменилась"
    _try = next((x for x in loop.body if isinstance(x, ast.Try)), None)
    assert _try is not None, "start_polling не под try — ошибки сети уронят процесс"
    assert any(isinstance(x, ast.Break) for x in _try.body), (
        "после штатного выхода из поллинга цикл запускает его заново: процесс "
        "не завершится ни по сигналу, ни по осознанной остановке")
