"""Предел загрузки обязан действовать НА ЧТЕНИИ, а не после него.

Три ручки мини-аппа объявляли предел («>25 МБ», «лимит 50 МБ») и проверяли его
так:

    data = await field.read(decode=False)
    if len(data) > _MAX: ...

То есть сначала втягивали поле multipart ЦЕЛИКОМ в память процесса, а потом
смотрели на длину. Предел при этом объявлен, но не действует: владелец с правом
загрузки отправляет гигабайт, процесс мини-аппа буферизует его весь, шлюз
ложится для ВСЕХ владельцев — и в ответ тот всё равно получает своё «файл
слишком большой». Авторизация тут не защищает: достаточно одного своего же
владельца с толстым каналом или кривым клиентом.

Проверка не текстовая: поддельное поле multipart отдаёт бесконечный поток и
ВЗРЫВАЕТСЯ, если у него вызвали `read()` — чтение поля целиком. Ручка обязана
читать `read_chunk` и остановиться на пределе.
"""
import asyncio
import json

import pytest
from aiohttp import web

from services import mini_app_api


class _UnboundedRead(BaseException):
    """Ручка прочитала поле целиком — предел не действует.

    Наследник `BaseException`, а НЕ `Exception`, сознательно: все три ручки
    обёрнуты в `except Exception: return 400`, поэтому обычное исключение они
    проглотят и проверка пройдёт на СТАРОМ коде — ответ 400 она и ждёт. Так эта
    проверка и прошла в первый раз на заведомо сломанном `field.read()`, пока
    тип не сменили. Измеритель обязан пробивать обработчик ошибок ручки.
    """


class _Runaway(BaseException):
    """Чтение ушло в бесконечность — тоже мимо `except Exception` ручки."""


class _EndlessField:
    """Поле multipart с бесконечным содержимым.

    `read()` здесь — ошибка по определению: именно он и означает «втянуть всё».
    `read_chunk` отдаёт мусор ровно запрошенного размера и считает, сколько
    отдал, чтобы проверка могла убедиться: чтение остановилось, а не ушло в
    бесконечность.
    """

    def __init__(self, name: str = "file", filename: str = "list.txt") -> None:
        self.name = name
        self.filename = filename
        self.given = 0

    async def read(self, decode: bool = False):  # noqa: ARG002
        raise _UnboundedRead(
            "ручка вызвала field.read() — поле читается целиком, предел не действует")

    async def read_chunk(self, size: int = 8192) -> bytes:
        # Больше любого объявленного предела, но каждая выдача мелкая: если
        # ручка не остановится сама, проверка упадёт по счётчику, а не по памяти.
        if self.given > 300 * 1024 * 1024:
            raise _Runaway("чтение не остановилось на пределе")
        self.given += size
        return b"\x00" * size

    async def text(self) -> str:
        raise _UnboundedRead(
            "ручка вызвала field.text() — поле читается целиком, предел не действует")


class _EndlessText(_EndlessField):
    """Бесконечное ТЕКСТОВОЕ поле: подпись, идентификатор аккаунта, фильтры.

    Отдельный класс нужен, потому что такое поле ручки читают не через
    `read()`, а через `text()` — и по коду `(await field.text())[:1024]`
    выглядит как «берём тысячу символов», хотя поле сначала втягивается
    целиком. Предел у этих полей тоже был только после чтения.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name=name, filename=None)

    def get_charset(self, default: str = "utf-8") -> str:
        return default

    async def read_chunk(self, size: int = 8192) -> bytes:
        if self.given > 300 * 1024 * 1024:
            raise _Runaway("чтение текстового поля не остановилось на пределе")
        self.given += size
        return b"a" * size


class _TextField:
    def __init__(self, name: str, value: str = "") -> None:
        self.name = name
        self.filename = None
        self._value = value

    def get_charset(self, default: str = "utf-8") -> str:
        return default

    async def text(self) -> str:
        return self._value

    async def read(self, decode: bool = False):  # noqa: ARG002
        return self._value.encode()

    async def read_chunk(self, size: int = 8192) -> bytes:
        data, self._value = self._value.encode()[:size], ""
        return data


class _Reader:
    def __init__(self, fields) -> None:
        self._fields = list(fields)

    def __aiter__(self):
        self._it = iter(self._fields)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Request:
    """Минимальный запрос: ровно то, что трогают ручки загрузки."""

    def __init__(self, fields, match_info=None) -> None:
        self._fields = fields
        self.match_info = match_info or {}
        self.query = {}
        self.rel_url = type("U", (), {"query": {}})()
        self.headers = {"Content-Type": "multipart/form-data; boundary=x"}
        self.method = "POST"
        self.path = "/"
        self.app = None

    async def multipart(self):
        return _Reader(self._fields)

    async def json(self):
        raise ValueError("не JSON")

    def can_read_body(self) -> bool:
        return True


class _Pool:
    async def fetch(self, q, *a): return []
    async def fetchrow(self, q, *a): return None
    async def fetchval(self, q, *a): return None
    async def execute(self, q, *a): return "OK"


@pytest.fixture()
def handlers(monkeypatch):
    """Ручки по РЕАЛЬНОМУ пути: собираем приложение и берём их из роутера."""
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777000111)
    app = web.Application()
    mini_app_api.setup_routes(app, _Pool())
    out = {}
    for route in app.router.routes():
        name = getattr(route.handler, "__name__", "")
        if name:
            out[name] = route.handler
    return out


def _body(resp) -> dict:
    try:
        return json.loads(resp.text or resp.body.decode())
    except Exception:
        return {}


UPLOADS = [
    # ручка, поля запроса, параметры пути
    ("invite_parse_file", lambda: [_EndlessField(), _TextField("group", "")], {}),
    ("uch_contact_media", lambda: [_EndlessField(filename="a.jpg"),
                               _TextField("text", "привет")],
     {"contact_id": "11111111-1111-1111-1111-111111111111"}),
    ("uch_segment_media", lambda: [_EndlessField(filename="a.jpg"),
                                   _TextField("text", "привет")], {}),
]


@pytest.mark.parametrize("name,fields,match", UPLOADS, ids=[u[0] for u in UPLOADS])
def test_the_upload_stops_instead_of_swallowing_the_stream(handlers, name, fields, match):
    handler = handlers.get(name)
    assert handler is not None, f"ручка {name} не зарегистрирована — проверка измеряет не то"
    field_list = fields()
    resp = asyncio.new_event_loop().run_until_complete(
        handler(_Request(field_list, match)))
    assert resp.status in (400, 413), (
        f"{name}: предел не сработал, ответ {resp.status} {_body(resp)}")
    endless = next(f for f in field_list if isinstance(f, _EndlessField))
    assert endless.given <= 120 * 1024 * 1024, (
        f"{name}: прочитано {endless.given} байт — чтение не остановилось у предела")


def test_the_helper_itself_stops_at_the_limit():
    """Сам помощник: читает кусками и останавливается, не дочитывая остаток."""
    field = _EndlessField()
    loop = asyncio.new_event_loop()
    with pytest.raises(mini_app_api.UploadTooLarge):
        loop.run_until_complete(mini_app_api.read_upload(field, 1024 * 1024))
    loop.close()
    assert field.given <= 2 * 1024 * 1024, "помощник прочитал больше предела"


def test_too_large_is_a_value_error():
    """`UploadTooLarge` — наследник `ValueError`.

    Прежние ручки ловят перебор как `except ValueError`; смена типа тихо
    превратила бы их 413 в необработанное исключение.
    """
    assert issubclass(mini_app_api.UploadTooLarge, ValueError)


TEXT_FIELDS = [
    ("invite_parse_file", lambda: [_TextField("file", "@kolya"), _EndlessText("group")], {}),
    ("uch_contact_media", lambda: [_TextField("file", "x"), _EndlessText("text")],
     {"contact_id": "11111111-1111-1111-1111-111111111111"}),
    ("uch_segment_media", lambda: [_TextField("file", "x"), _EndlessText("filters")], {}),
    ("uch_segment_media", lambda: [_TextField("file", "x"), _EndlessText("account_ids")], {}),
]


@pytest.mark.parametrize("name,fields,match", TEXT_FIELDS,
                         ids=[f"{n}:{i}" for i, (n, _f, _m) in enumerate(TEXT_FIELDS)])
def test_a_text_field_stops_instead_of_swallowing_the_stream(handlers, name, fields, match):
    """Подпись и фильтры — тоже поля multipart, и предел у них тоже на чтении."""
    handler = handlers.get(name)
    assert handler is not None, f"ручка {name} не зарегистрирована — проверка измеряет не то"
    field_list = fields()
    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(handler(_Request(field_list, match)))
    finally:
        loop.close()
    # Ручка может ответить и 400, и 413, и даже дойти до своей логики с
    # обрезанной подписью — важно одно: поток она не проглотила целиком.
    assert resp is not None
    endless = next(f for f in field_list if isinstance(f, _EndlessText))
    assert endless.given <= 2 * 1024 * 1024, (
        f"{name}/{endless.name}: прочитано {endless.given} байт — "
        "текстовое поле читается без предела")


def test_no_handler_reads_a_multipart_field_whole():
    """Сеть на весь API-слой: `part.text()`/`field.read()` там быть не должно.

    Проба выше гоняет только те ручки, у которых запрос собирается тремя
    полями. Медиа в диалог аккаунта, файл сессии и облако принимают ещё и
    `access_hash`, `proxy_id`, имя файла — подделать их все дешевле один раз
    разбором, чем поддерживать четыре поддельных запроса.

    Проверка по именам получателя (`part`, `field`) — так эти поля зовут во всём
    слое; `await resp.text()` у ответа aiohttp под неё не попадает. Имя — слабее
    типа, и это сознательный компромисс: цена ошибки здесь — лишний разговор в
    ревью, а не пропущенная находка.
    """
    import ast

    from tests.miniapp_source import api_files

    PART_NAMES = {"part", "field"}
    offenders = []
    for path in api_files():
        src = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            recv = node.func.value
            if not (isinstance(recv, ast.Name) and recv.id in PART_NAMES):
                continue
            if node.func.attr in ("text", "read", "json"):
                offenders.append(f"{path.name}:{node.lineno}: {recv.id}.{node.func.attr}()")
    assert not offenders, (
        "поле multipart читается целиком — предел, объявленный после чтения, "
        "не действует; читайте через read_upload/read_upload_text:\n  "
        + "\n  ".join(offenders))
