"""Нечитаемое тело запроса — это 400 по-русски, а не 500 «что-то пошло не так».

Тело читают около 250 изменяющих ручек, и ни одна не разбирала ошибку разбора.
`await request.json()` на мусоре или на ПУСТОМ теле бросает JSONDecodeError, и
это ловит либо общий `except` ручки, либо страховочный слой — в обоих случаях
владелец получает «Что-то пошло не так, попробуйте ещё раз». Вред тот же, что
и у опечатки в адресе, и ни одна часть не косметическая:

  * совет неверный: повтор того же запроса даст то же самое;
  * на каждый такой заход в журнал уезжает полная трассировка, и настоящие
    пятисотки тонут среди чужих опечаток;
  * 500 для клиента значит «повтори позже», 400 — «не повторяй».

Чинить это в 250 ручках нельзя: класс останется открытым для 251-й. Поэтому
решение стоит слоем, там же, где уже стоит страховка от необработанных
исключений, — и здесь проверяется поведение слоя, а не текст исходника.

Главная тонкость в том, чтобы не соврать в обратную сторону. JSONDecodeError
прилетает и ИЗНУТРИ: из jsonb-поля базы, из ответа чужого API. Ответить на
это «ты прислал мусор» значило бы обвинить клиента в своей поломке, да ещё и
посоветовать не повторять. Поэтому признак точный — исключение вылетело из
разбора ТЕЛА, — и обе стороны проверяются здесь по отдельности.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from services import mini_app_api as M

UID = 775501


class _Pool:
    """Пул, до которого дело не должно доходить: запрос обрывается раньше."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, q, *a):
        self.calls.append(q)
        return []

    async def fetchrow(self, q, *a):
        self.calls.append(q)
        return None

    async def fetchval(self, q, *a):
        self.calls.append(q)
        return 0

    async def execute(self, q, *a):
        self.calls.append(q)
        return "OK"

    def acquire(self):
        raise AssertionError("разбор тела не должен доходить до базы")


def _app(pool, extra=None):
    app = web.Application()
    M.setup_routes(app, pool)
    if extra is not None:
        app.router.add_post("/api/miniapp/_probe_own_error", extra)
    return app


def _post(app, path, data, *, headers=None):
    async def go():
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(path, data=data, headers=headers or {})
            return resp.status, await resp.text()
    return asyncio.run(go())


def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    M._cache.clear()


# ── Вина клиента ─────────────────────────────────────────────────────────────

def test_garbage_in_the_body_answers_four_hundred(monkeypatch):
    """Мусор вместо JSON: ответ 400 и по-русски, а не отчёт о поломке."""
    _auth(monkeypatch)
    status, text = _post(_app(_Pool()), "/api/miniapp/uch/groups", b"{not json")
    assert status == 400, (
        f"нечитаемое тело ответило {status}: владельцу скажут «попробуйте ещё "
        "раз», а повтор даст то же самое")
    body = json.loads(text)
    msg = body.get("error") or body.get("detail") or text
    assert "Запрос не разобран" in msg, f"сообщение не то: {msg!r}"
    # Текст обязан быть русским: английское владелец не читает.
    assert not any(c.isascii() and c.isalpha() for c in msg.replace("-", "")), (
        f"в сообщении латиница: {msg!r}")


def test_an_empty_body_answers_four_hundred_too(monkeypatch):
    """Пустое тело — самый частый случай: ручку позвали вообще без данных."""
    _auth(monkeypatch)
    status, _ = _post(_app(_Pool()), "/api/miniapp/uch/groups", b"")
    assert status == 400, f"пустое тело ответило {status}, а не 400"


def test_the_body_never_reaches_the_log(monkeypatch, caplog):
    """В теле бывают токены. В журнал уходит метод и путь, и только."""
    _auth(monkeypatch)
    secret = '{"token": "СЕКРЕТ-НЕ-В-ЖУРНАЛ'.encode("utf-8")
    with caplog.at_level("WARNING"):
        _post(_app(_Pool()), "/api/miniapp/uch/groups", secret)
    whole = "\n".join(r.getMessage() for r in caplog.records)
    assert "СЕКРЕТ-НЕ-В-ЖУРНАЛ" not in whole, "тело запроса утекло в журнал"
    assert "/api/miniapp/uch/groups" in whole, (
        "в журнале нет даже пути: разбираться будет не с чем")


def test_a_broken_body_is_logged_without_a_traceback(monkeypatch, caplog):
    """Трассировка на опечатку клиента прячет настоящие пятисотки."""
    _auth(monkeypatch)
    with caplog.at_level("WARNING"):
        _post(_app(_Pool()), "/api/miniapp/uch/groups", b"{not json")
    broken = [r for r in caplog.records if "нечитаемое тело" in r.getMessage()]
    assert broken, "нечитаемое тело вообще не отмечено в журнале"
    assert all(r.exc_info is None for r in broken), (
        "на нечитаемое тело пишется трассировка, хотя сервер цел")


# ── Наша собственная вина: соврать в обратную сторону нельзя ────────────────

def test_our_own_decode_error_still_answers_five_hundred(monkeypatch):
    """Та же ошибка разбора, но из НАШИХ данных, — это по-прежнему 500.

    Без этой проверки признак мог бы ловить любой JSONDecodeError, и поломка
    в разборе jsonb-поля базы или ответа чужого API докладывалась бы клиенту
    как «ты прислал мусор», да ещё с советом не повторять.
    """
    _auth(monkeypatch)

    async def handler(request):
        await request.json()            # тело ЧИТАЕТСЯ и разбирается нормально
        json.loads("{сломанное-из-базы")  # а падает уже наш разбор
        return web.json_response({"ok": True})

    status, text = _post(_app(_Pool(), extra=handler),
                         "/api/miniapp/_probe_own_error", b'{"a": 1}')
    assert status == 500, (
        f"наша собственная ошибка разбора ответила {status}: клиенту сказали, "
        "что он прислал мусор, хотя мусор наш")
    assert "Внутренняя" in text or "ошибка" in text.lower()


def test_the_detector_sees_both_sides_apart():
    """Проба самого признака, без сети: иначе правила выше ничего не значат."""
    try:
        json.loads("{свои данные")
    except ValueError as exc:
        assert not M.body_came_broken_from_the_client(exc), (
            "своя ошибка разбора принята за вину клиента")
    # А не-ValueError не ошибка разбора вовсе.
    assert not M.body_came_broken_from_the_client(RuntimeError("что угодно"))


# ── Чтобы класс не открылся заново на следующей ручке ───────────────────────

def _handlers_reading_a_body_unguarded() -> list[str]:
    """Ручки, читающие тело под общим `except … 500` без проверки вины.

    Слой ниже ловит всё, что ДОЛЕТЕЛО до него, но у многих ручек свой общий
    `except`, который перехватывает ошибку раньше. Поэтому правило смотрит на
    ручки, а не только на слой: иначе двадцать восемь мест закрыты, а
    двадцать девятое откроет класс снова.
    """
    import ast

    from tests.test_a_typo_in_a_link_is_not_our_fault import (
        _catches_narrowly, _handlers, _is_catch_all_500, _reachable)

    def is_body(node):
        if not isinstance(node, ast.Await):
            return False
        c = node.value
        return (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr in ("json", "post")
                and "request" in ast.unparse(c.func.value))

    bad = []
    for name, (fname, node, routes) in sorted(_handlers().items()):
        if not any(_reachable(r) for r in routes):
            continue
        hits: list = []

        def walk(n, stack):
            if is_body(n):
                for t in reversed(stack):
                    if any(_catches_narrowly(h) for h in t.handlers):
                        return
                    if _is_catch_all_500(t):
                        hits.append(t)
                        return
                return
            for ch in ast.iter_child_nodes(n):
                walk(ch, stack + [n] if isinstance(n, ast.Try)
                     and ch in n.body else stack)

        walk(node, [])
        for t in hits:
            guarded = any("bad_body_response" in ast.unparse(h)
                          for h in t.handlers)
            if not guarded:
                bad.append(f"{fname} {name}")
    return sorted(set(bad))


def test_the_sweep_sees_the_handlers_that_read_a_body():
    """Пустая выдача сделала бы правило ниже зелёным ни на чём."""
    import ast

    from tests.test_a_typo_in_a_link_is_not_our_fault import _handlers

    reading = [n for n, (_f, node, _r) in _handlers().items()
               if "request.json()" in ast.unparse(node)
               or "request.post()" in ast.unparse(node)]
    assert len(reading) >= 100, (
        f"ручек, читающих тело, найдено мало: {len(reading)}")


def test_no_handler_swallows_a_broken_body_as_a_server_fault():
    """Ручка со своим общим `except` обязана спросить, чья это вина."""
    bad = _handlers_reading_a_body_unguarded()
    assert not bad, (
        "ручка читает тело под общим `except … 500` и не спрашивает "
        "`bad_body_response`: битое или пустое тело снова будет показано "
        "владельцу как поломка сервера, с неверным советом повторить:\n  "
        + "\n  ".join(bad))


# ── Сообщение обязано доходить до глаз владельца ────────────────────────────

def test_the_message_survives_the_whole_way_to_the_screen():
    """Русский текст с сервера обязан дойти до экрана, а не быть заменён.

    Сообщение проходит три звена, и каждое может его молча потерять: сервер
    кладёт текст под СВОИМ ключом, `api()` читает из ОПРЕДЕЛЁННЫХ ключей, а
    `errRu` подменяет всё, в чём нет кириллицы, общей фразой «Что-то пошло не
    так, попробуйте ещё раз». Разойдись ключи — и вся работа этого файла
    выродится в ту же самую общую фразу, причём незаметно: ответ по-прежнему
    400, в тестах сервера всё зелено, а владелец читает прежний неверный
    совет. Поэтому звенья сверяются здесь, а `errRu` не грепается, а
    ПРОГОНЯЕТСЯ в node.
    """
    import pathlib
    import re
    import subprocess

    from tests.miniapp_source import api_function, js_functions

    root = pathlib.Path(__file__).resolve().parents[1]
    server = api_function("_err")
    key = re.search(r"body\s*=\s*\{\s*[\"'](\w+)[\"']\s*:", server)
    assert key, "в `_err` больше не видно ключа, под которым уходит текст"

    ui = (root / "mini_app" / "index.html").read_text(encoding="utf-8")
    reads = re.search(r"msg\s*=\s*((?:_j\.\w+\s*\|\|\s*)+_j\.\w+)", ui)
    assert reads, "в `api()` больше не видно, из каких ключей читается текст"
    assert key.group(1) in reads.group(1), (
        f"сервер кладёт текст под ключом {key.group(1)!r}, а приложение читает "
        f"из {reads.group(1)!r}: сообщение потеряется, и владелец увидит общую "
        "фразу вместо объяснения")

    # И последнее звено: кириллица обязана проходить как есть.
    script = js_functions()["errRu"] + """
console.log(JSON.stringify({
  ru: errRu(new Error('Запрос не разобран: приложение отправило неполные данные.')),
  en: errRu(new Error("Cannot read properties of undefined")),
}));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert "Запрос не разобран" in got["ru"], (
        f"русское сообщение сервера подменено: {got['ru']!r}")
    assert "Cannot read" not in got["en"], (
        "английский текст исключения показывается владельцу как есть")
