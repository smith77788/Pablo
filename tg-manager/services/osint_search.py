"""Поиск аккаунтов по нику или почте — мост к модулю blackbird.

Сам Blackbird живёт ОТДЕЛЬНО: корневой каталог `blackbird/`, в образе —
`/app/blackbird` со своим venv (`Dockerfile`). Его зависимости (aiohttp,
pillow, reportlab в своих пинах) не пересекаются с зависимостями продукта,
поэтому здесь он не импортируется, а запускается отдельным процессом:

    /app/blackbird/.venv/bin/python -m pablo_blackbird --username <ник>

Обёртка печатает JSON в stdout; отсюда он разбирается в словарь.

Почему операция, а не задача в обработчике. Поиск идёт по ~600 сайтам и
длится от секунд до пары минут. Бот и мини-апп ставят операцию
(`operation_bus.submit("osint_search")`), исполнитель op_worker ждёт процесс,
и итог приходит владельцу обычным уведомлением о завершении операции;
найденные профили ложатся в журнал операции (`operation_log`), то есть
видны в карточке операции и в её выгрузке CSV. Аккаунты Telegram не нужны:
запросы идут с сервера к публичным страницам сайтов.

Модуль без импортов бота и aiohttp: его зовут и op_worker, и обработчики.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

# Где лежит модуль. В образе — /app/blackbird (см. корневой Dockerfile).
DEFAULT_DIR = "/app/blackbird"
# Потолок одного поиска. Обёртка сама держит 600 с; здесь чуть больше, чтобы
# её собственный отказ успел дойти до нас текстом, а не убийством процесса.
RUN_TIMEOUT_S = 660
# Как часто спрашивать отмену, пока процесс работает.
_CANCEL_POLL_S = 2.0
# Сколько найденных ссылок печатать в уведомлении (остальное — в журнале).
NOTIFY_MAX_LINKS = 40

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")
# Ник на сайтах бывает с точкой, дефисом и подчёркиванием. Пробелы, слэши и
# ведущий дефис не пропускаем: запрос подставляется в адреса сайтов и уходит
# аргументом процесса.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._\-]{1,63}$")
_TG_LINK_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/", re.IGNORECASE)


class SearchUnavailable(RuntimeError):
    """Модуль blackbird не установлен рядом с продуктом."""


class SearchFailed(RuntimeError):
    """Поиск запущен, но не отработал (упал, завис, вернул нечитаемое)."""


def module_dir() -> str:
    return os.environ.get("BLACKBIRD_DIR") or DEFAULT_DIR


def module_python() -> str:
    return os.environ.get("BLACKBIRD_PYTHON") or os.path.join(
        module_dir(), ".venv", "bin", "python")


def is_available() -> bool:
    return (os.path.isfile(module_python())
            and os.path.isdir(os.path.join(module_dir(), "pablo_blackbird")))


def parse_query(text: str) -> tuple[str, str]:
    """Разобрать ввод человека: ('username'|'email', запрос).

    Понимает `@ник`, ссылку `t.me/ник` и почту. Бросает ValueError с
    подсказкой по-русски — её показывают как есть.
    """
    q = (text or "").strip()
    if not q:
        raise ValueError("Укажите ник или почту")
    if "\n" in q or " " in q:
        raise ValueError("Нужен один ник или одна почта, без пробелов")
    if "@" in q[1:]:
        if not _EMAIL_RE.match(q):
            raise ValueError("Почта выглядит неправильно")
        return "email", q.lower()
    q = _TG_LINK_RE.sub("", q).lstrip("@").strip("/")
    if not _USERNAME_RE.match(q):
        raise ValueError(
            "Ник может содержать латинские буквы, цифры, точку, дефис и "
            "подчёркивание, от 2 до 64 символов")
    return "username", q


async def run_search(
    kind: str,
    query: str,
    *,
    should_stop: Callable[[], Awaitable[bool]] | None = None,
    timeout_s: float = RUN_TIMEOUT_S,
) -> dict | None:
    """Запустить поиск и дождаться результата.

    Возвращает разобранный JSON обёртки (`found_count`, `accounts`, ...) или
    None, если поиск остановили (`should_stop` ответил True). Отмену
    спрашивает каждые пару секунд и убивает процесс сразу.
    """
    if kind not in ("username", "email"):
        raise ValueError(f"неизвестный вид поиска: {kind}")
    if not is_available():
        raise SearchUnavailable(
            "Модуль поиска не установлен на сервере (нет "
            f"{module_python()})")
    env = dict(os.environ)
    env.setdefault("BLACKBIRD_WORKDIR", "/tmp/blackbird")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = await asyncio.create_subprocess_exec(
        module_python(), "-m", "pablo_blackbird", f"--{kind}", query,
        cwd=module_dir(), env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    comm = asyncio.ensure_future(proc.communicate())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        while True:
            done, _ = await asyncio.wait({comm}, timeout=_CANCEL_POLL_S)
            if done:
                break
            if should_stop is not None:
                try:
                    stop = await should_stop()
                except Exception:
                    # Блип БД — не стоп (CLAUDE.md: «блип БД стопом НЕ считается»).
                    stop = False
                if stop:
                    _kill(proc)
                    await asyncio.gather(comm, return_exceptions=True)
                    return None
            if loop.time() > deadline:
                _kill(proc)
                await asyncio.gather(comm, return_exceptions=True)
                raise SearchFailed(f"Поиск не уложился в {int(timeout_s)} с")
    except asyncio.CancelledError:
        _kill(proc)
        comm.cancel()
        raise
    out, err = comm.result()
    if proc.returncode != 0:
        tail = (err or b"").decode("utf-8", "replace").strip()[-300:]
        raise SearchFailed(f"Поиск завершился с ошибкой: {tail or proc.returncode}")
    try:
        data = json.loads((out or b"").decode("utf-8", "replace"))
    except ValueError as exc:
        raise SearchFailed("Поиск вернул нечитаемый ответ") from exc
    if not isinstance(data, dict):
        raise SearchFailed("Поиск вернул нечитаемый ответ")
    accounts = data.get("accounts")
    data["accounts"] = [a for a in accounts if isinstance(a, dict)] if isinstance(accounts, list) else []
    return data


def _kill(proc) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def found_accounts(data: dict) -> list[dict]:
    return [a for a in (data.get("accounts") or [])
            if isinstance(a, dict) and a.get("status", "FOUND") == "FOUND" and a.get("url")]


def summary_text(kind: str, query: str, data: dict, max_links: int = NOTIFY_MAX_LINKS) -> str:
    """Итог поиска для уведомления в боте. Текст уходит с parse_mode=HTML,
    поэтому всё внешнее экранировано, а тегов нет вовсе: уведомление режется
    по длине, и разрезанный тег уронил бы сообщение целиком."""
    found = found_accounts(data)
    what = "нику" if kind == "username" else "почте"
    head = f"🕵️ Поиск по {what} {html.escape(query)}: "
    if not found:
        return head + "аккаунтов не найдено."
    lines = [head + f"найдено аккаунтов — {len(found)}."]
    for acc in found[:max_links]:
        name = html.escape(str(acc.get("name") or "сайт"))
        lines.append(f"• {name}: {html.escape(str(acc.get('url')))}")
    if len(found) > max_links:
        lines.append(f"…и ещё {len(found) - max_links} — в журнале операции.")
    return "\n".join(lines)


def log_message(acc: dict) -> str:
    """Подпись строки журнала: сайт и категория."""
    name = str(acc.get("name") or "сайт")
    cat = str(acc.get("category") or "").strip()
    return f"{name} · {cat}" if cat else name
