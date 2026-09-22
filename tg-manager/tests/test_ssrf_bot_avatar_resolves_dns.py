"""Регрессия: аватар бота скачивается только после РЕЗОЛВА адреса.

Эндпоинт `POST /bot/{id}/photo` скачивает картинку по присланному URL тут же,
в том же запросе — это настоящий серверный sink. Стоял на нём синтаксический
гард `is_safe_public_url`, который по своей же документации ловит только
ЛИТЕРАЛЬНЫЕ внутренние адреса («обычное имя — резолв проверит async-слой»).
Имя, которое резолвится в 169.254.169.254 или 127.0.0.1, он пропускал, и сервер
шёл за ним сам: SSRF на cloud metadata.

Гард с резолвом (`resolve_url_is_public`) в проекте есть и стоит на отложенном
пути в `dm_engine`. Здесь он не стоял — работа из PR #12 на этом месте
потерялась при возврате в ствол.

Проверяем контракт исходника, а не сетевое поведение: поднимать DNS в тесте
незачем, а важно, что перед скачиванием зовётся именно авторитетная проверка.
Границы берём по структуре функции, а не по числу символов — фиксированное
окно ломается от дописанного комментария.
"""
from __future__ import annotations

import inspect
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bot_avatar_handler_source() -> str:
    """Тело обработчика аватара: от его def до следующего def того же уровня."""
    with open(os.path.join(ROOT, "services/mini_app_api.py"), encoding="utf-8") as f:
        src = f.read()
    i = src.index("async def bot_avatar(")
    m = re.compile(r"\n    async def (?!bot_avatar\()", re.S).search(src, i)
    return src[i:m.start()] if m else src[i:]


def test_bot_avatar_uses_dns_resolving_guard():
    seg = _bot_avatar_handler_source()
    assert "await resolve_url_is_public(photo_url)" in seg, (
        "аватар скачивается сервером сразу, поэтому проверка должна резолвить "
        "адрес; синтаксического is_safe_public_url здесь недостаточно"
    )


def test_guard_stands_before_the_download():
    """Порядок важен: проверка обязана стоять ДО сетевого запроса."""
    seg = _bot_avatar_handler_source()
    guard = seg.index("resolve_url_is_public(photo_url)")
    fetch = seg.index("sess.get(photo_url")
    assert guard < fetch, "проверка URL оказалась после скачивания"


def test_resolving_guard_rejects_name_pointing_inside(monkeypatch):
    """Сам гард: имя, резолвящееся внутрь, отвергается.

    Это и есть дыра, которую синтаксический слой не закрывает.
    """
    import asyncio

    from services import security

    async def _fake_getaddrinfo(host, port, **kw):
        # имя выглядит публичным, а резолвится в metadata-адрес
        return [(2, 1, 6, "", ("169.254.169.254", 0))]

    class _Loop:
        async def getaddrinfo(self, *a, **kw):
            return await _fake_getaddrinfo(*a, **kw)

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _Loop())

    async def _run():
        return await security.resolve_url_is_public("https://metadata.example.com/latest")

    assert asyncio.run(_run()) is False


def test_handler_is_async_so_await_is_valid():
    """Гард асинхронный — обработчик обязан быть async, иначе await не скомпилируется."""
    seg = _bot_avatar_handler_source()
    assert seg.lstrip().startswith("async def bot_avatar("), inspect.cleandoc(
        "обработчик аватара должен остаться async: гард с резолвом — корутина")
