"""Версии файлов мини-аппа в ссылках: повторное открытие без сети.

Замер 08.11.2026 (Chromium, процессор ×4, мобильная сеть 150 мс): повторное
открытие мини-аппа занимало 3,8 с и 45 запросов. Все файлы отдавались с
`Cache-Control: no-cache`, то есть браузер на КАЖДОМ открытии переспрашивал
сервер про index.html и ещё 36 скриптов и стилей — по запросу на файл, и
каждый стоил задержку сети, даже когда ответом было пустое 304.

Решение — версия в адресе: страница ссылается на `screens/x.js?v=<версия>`,
где версия — отпечаток самого файла (время изменения и размер, как у ETag).
Ответ на адрес с ТЕКУЩЕЙ версией кэшируется навсегда (`immutable`):
изменится файл — изменится и версия в ссылке, и браузер пойдёт по новому
адресу. index.html по-прежнему перепроверяется каждый раз, и его ETag
включает версии всех файлов, на которые он ссылается: иначе после деплоя,
поменявшего только скрипт экрана, браузер получил бы 304 на старую страницу
со старыми ссылками и навсегда застрял на прошлой версии экрана.
"""
from __future__ import annotations

import hashlib
import os
import re

# Свои относительные ссылки на скрипты и стили: src="screens/x.js",
# href="ui_kit.css". Абсолютные (http://, //, /) и уже версионированные не
# трогаем.
_REF = re.compile(
    r'(?P<attr>\b(?:src|href))="(?P<path>(?![a-z]+:|/)[A-Za-z0-9_\-/.]+\.(?:js|css))"')

IMMUTABLE = "public, max-age=31536000, immutable"


def asset_refs(html: str) -> list[str]:
    """Относительные пути скриптов и стилей, на которые ссылается страница."""
    seen: list[str] = []
    for m in _REF.finditer(html):
        p = m.group("path")
        if p not in seen:
            seen.append(p)
    return seen


def version_token(full_path: str) -> str | None:
    """Версия файла: отпечаток времени изменения и размера (None — файла нет)."""
    try:
        st = os.stat(full_path)
    except OSError:
        return None
    return f"{int(st.st_mtime):x}{int(st.st_size):x}"


def resolve(static_root: str, rel: str) -> str | None:
    """Путь файла внутри mini_app/ или None (выход наружу через «../» — None)."""
    root = os.path.realpath(static_root)
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(root + os.sep):
        return None
    return full


def versions_for(static_root: str, html: str) -> dict[str, str]:
    """{относительный путь: версия} для всех существующих ссылок страницы."""
    out: dict[str, str] = {}
    for rel in asset_refs(html):
        full = resolve(static_root, rel)
        tok = version_token(full) if full else None
        if tok:
            out[rel] = tok
    return out


def stamp_versions(html: str, versions: dict[str, str]) -> str:
    """Дописать `?v=<версия>` к ссылкам на известные файлы."""
    def _sub(m: re.Match) -> str:
        p = m.group("path")
        v = versions.get(p)
        if not v:
            return m.group(0)
        return f'{m.group("attr")}="{p}?v={v}"'
    return _REF.sub(_sub, html)


def page_etag(index_key: tuple, versions: dict[str, str]) -> str:
    """ETag страницы: сама страница + версии всех её файлов."""
    h = hashlib.sha1(repr((index_key, sorted(versions.items()))).encode(), usedforsecurity=False).hexdigest()[:16]
    return f'W/"{h}"'
