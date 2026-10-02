"""Вложение из архива не должно исполняться на нашем origin.

`/api/miniapp/vault/media/...` отдавал файл так:

    "Content-Type": res.get("mime") or "application/octet-stream",
    "Content-Disposition": f'inline; filename="{ascii_name}"; ...'

Тип файла здесь задаёт СОБЕСЕДНИК: он прислал вложение, Telegram сохранил его
mime, мы вернули его как есть и попросили браузер показать файл на месте
(`inline`). Пришлёт он `.html` или `.svg` — браузер выполнит его разметку и
скрипты НА НАШЕМ origin. Оттуда читается `localStorage`, где лежит
`ig_device_token` — долгоживущий ключ автономного входа, который меняется на
полноценный токен сессии через `/api/miniapp/pair/exchange`. То есть чужой
человек в деловой переписке получил бы доступ к аккаунту владельца вне Telegram.

Через сам интерфейс это сегодня не срабатывает: просмотрщик забирает файл
запросом с заголовком `Authorization` и делает `blob:`-ссылку, а по прямому
адресу без токена приходит 401. Но токен эндпоинты принимают и параметром
`?token=` — на этом построено сохранение файлов через нативный загрузчик
Telegram (`cloudSave`). То есть до исполнения чужой разметки оставалась одна
ссылка в интерфейсе. Соседний эндпоинт выдачи файлов (`cloud/file/.../download`)
это уже учитывает: `attachment` и `nosniff`.

Поэтому: на месте показываем только то, что браузер не исполняет (картинки,
видео, звук, PDF), остальное — вложением и с нейтральным типом, и `nosniff`
всегда, чтобы браузер не передумал насчёт типа сам.
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from services import mini_app_api as M

UID = 515150
# Формат токена проверяет aiogram при создании Bot — нужен правдоподобный.
FAKE_BOT_TOKEN = "123456789:AAEhBP0abcdefghijklmnopqrstuvwxyz12345678"


class _Pool:
    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return 0

    async def execute(self, q, *a):
        return "OK"


class _Req:
    def __init__(self, chat_id="5", msg_id="7"):
        self.match_info = {"chat_id": chat_id, "msg_id": msg_id}
        self.rel_url = type("U", (), {"query": {}})()
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self.query_string = ""
        self.method = "GET"


def _handler():
    app = web.Application()
    M.setup_routes(app, _Pool())
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or ""
        if route.method == "GET" and path == "/api/miniapp/vault/media/{chat_id}/{msg_id}":
            return route.handler
    raise AssertionError("роут выдачи вложения не зарегистрирован")


def _serve(monkeypatch, mime: str, name: str = "файл") -> web.Response:
    """Отдать вложение, которое «прислал собеседник» с данным mime."""
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    monkeypatch.setattr(M, "_bot_token", lambda: FAKE_BOT_TOKEN)
    from services import vault_service

    async def _fake(pool, bot, uid, chat_id, msg_id):
        return {"ok": True, "data": b"<script>alert(1)</script>",
                "mime": mime, "filename": name}

    monkeypatch.setattr(vault_service, "fetch_media", _fake)
    return asyncio.run(_handler()(_Req()))


def test_an_image_is_shown_in_place(monkeypatch):
    """Страховка измерителя: не отдай стенд файл вообще — весь тест пуст."""
    r = _serve(monkeypatch, "image/png")
    assert r.status == 200 and r.body
    assert r.headers["Content-Type"].startswith("image/png")
    assert r.headers["Content-Disposition"].startswith("inline"), (
        "картинку перестали показывать на месте — смотреть архив стало неудобно")


@pytest.mark.parametrize("mime", [
    "text/html",
    "image/svg+xml",          # SVG исполняет скрипты, картинкой его считать нельзя
    "application/xhtml+xml",
    "text/xml",
    "application/javascript",
    "",                        # mime не пришёл вовсе
])
def test_executable_attachment_is_not_shown_in_place(monkeypatch, mime):
    r = _serve(monkeypatch, mime)
    assert r.headers["Content-Disposition"].startswith("attachment"), (
        f"файл типа {mime!r} от собеседника открывается на месте — его разметка "
        "исполнится на нашем origin")
    assert r.headers["Content-Type"].split(";")[0] == "application/octet-stream", (
        f"тип {mime!r} от собеседника уходит браузеру как есть")


@pytest.mark.parametrize("mime", ["text/html", "image/png", "video/mp4"])
def test_the_browser_may_not_guess_the_type_itself(monkeypatch, mime):
    r = _serve(monkeypatch, mime)
    assert r.headers.get("X-Content-Type-Options") == "nosniff", (
        "без nosniff браузер сам решает, что перед ним, и решение бывает «html»")


@pytest.mark.parametrize("mime,inline", [
    ("IMAGE/PNG", True),              # регистр приходит от собеседника
    ("image/png; charset=binary", True),
    ("  video/mp4  ", True),
    ("audio/ogg", True),
    ("application/pdf", True),
    ("text/HTML", False),
    ("image/SVG+XML", False),
])
def test_the_type_is_compared_after_normalising(monkeypatch, mime, inline):
    """Иначе `text/HTML` или `text/html; charset=utf-8` проскочит мимо списка."""
    r = _serve(monkeypatch, mime)
    got = r.headers["Content-Disposition"].split(";")[0]
    assert got == ("inline" if inline else "attachment"), (
        f"{mime!r} отдан как {got}")


def test_the_peers_filename_still_cannot_break_the_header(monkeypatch):
    """Имя файла тоже от собеседника: кавычка и перевод строки ломали бы ответ."""
    r = _serve(monkeypatch, "image/png", name='a"b\nc.png')
    cd = r.headers["Content-Disposition"]
    assert "\n" not in cd and cd.count('"') == 2, cd
