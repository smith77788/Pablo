"""Исходник мини-аппа целиком: index.html плюс вынесенные экраны.

Мини-апп перестал быть одним файлом. `index.html` пилится на
`mini_app/screens/*.js` — монолит был 1.4 МБ, и каждая правка в нём рискованнее
правки в модуле. Тест, который читает ТОЛЬКО index.html, после каждого выноса
начинает падать не потому, что функциональность пропала, а потому, что она
переехала в соседний файл.

Поэтому проверки «есть ли в мини-аппе такой контрол / такая функция» должны
брать источник отсюда:

    from tests.miniapp_source import miniapp_source
    UI = miniapp_source()

Если проверка про РАЗМЕТКУ (id, onclick, структура экрана) — берите
`miniapp_html()`: разметка живёт только в index.html, и подмешивать к ней JS
незачем.
"""
from __future__ import annotations

import functools
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MINI_APP = ROOT / "mini_app"


@functools.lru_cache(maxsize=1)
def miniapp_html() -> str:
    """Только разметка — index.html как есть."""
    return (MINI_APP / "index.html").read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def screen_files() -> tuple[pathlib.Path, ...]:
    """Вынесенные экраны, в порядке подключения в index.html.

    Порядок важен: файлы — обычные классические скрипты, они делят одну
    глобальную область и грузятся ПОСЛЕ главного блока.
    """
    return tuple(sorted((MINI_APP / "screens").glob("*.js")))


@functools.lru_cache(maxsize=1)
def miniapp_source() -> str:
    """Разметка и весь JS мини-аппа одной строкой."""
    parts = [miniapp_html()]
    parts += [p.read_text(encoding="utf-8") for p in screen_files()]
    return "\n".join(parts)


def source_of(function_name: str) -> str:
    """Файл, в котором объявлена функция, — целиком.

    Нужно там, где тест разбирает тело функции: склеенный источник для этого не
    годится, границы файлов в нём условны.
    """
    import re

    pat = re.compile(r"^(?:async )?function " + re.escape(function_name) + r"\s*\(", re.M)
    for text in (miniapp_html(), *(p.read_text(encoding="utf-8") for p in screen_files())):
        if pat.search(text):
            return text
    raise AssertionError(f"функция {function_name} не найдена ни в одном файле мини-аппа")


# ── Серверная сторона: API-слой тоже перестал быть одним файлом ──────────────
#
# Тот же урок, что и выше, но про сервер. `services/mini_app_api.py` разросся до
# 25 тысяч строк и пилится на `services/mini_app_*.py`. Ратчет, который читает
# ТОЛЬКО mini_app_api.py, после каждого выноса краснеет не потому, что ручка
# пропала, а потому, что она переехала — так за один вынос прокси упали три
# проверки, ни одна из которых не про прокси. Реестр маршрутов и «есть ли в API
# такая ручка» берите отсюда.

SERVICES = ROOT / "services"


@functools.lru_cache(maxsize=1)
def api_files() -> tuple[pathlib.Path, ...]:
    """Все файлы API-слоя мини-аппа: главный плюс вынесенные модули."""
    return tuple(sorted(SERVICES.glob("mini_app_*.py")))


@functools.lru_cache(maxsize=1)
def api_source() -> str:
    """Весь API-слой одной строкой."""
    return "\n".join(p.read_text(encoding="utf-8") for p in api_files())


@functools.lru_cache(maxsize=1)
def api_routes() -> frozenset[str]:
    """Пути, зарегистрированные где-либо в API-слое мини-аппа."""
    import re

    return frozenset(re.findall(
        r'app\.router\.add_(?:get|post|put|delete|patch)\("([^"]+)"', api_source()))
