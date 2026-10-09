"""Адрес прокси уходит в мини-апп, пароль к прокси — нет.

ЧТО БЫЛО. `GET /api/miniapp/proxies` расшифровывал `proxy_url` и отдавал его
клиенту целиком — вместе с логином и паролем. Экран при этом печатает только
ХОСТ: во всех четырёх местах стоит `(p.proxy_url||'').split('@').pop()`.
Единственным потребителем полного адреса был импорт сессий: выпадашка
складывала URL в `option.dataset.url`, а при отправке экран присылал его
НАЗАД на сервер — чтобы сервер проверил сессию через этот прокси. То есть
пароль к прокси ехал в браузер и обратно только для того, чтобы вернуться
туда, откуда его взяли.

Рядом стояли два места, прямо обещавшие обратное: выгрузка прокси
(`proxy_export`) маскирует адрес с обоснованием «не выгружаем логин/пароль», а
docstring у `_json_resp` обещает, что обработчик, написанный завтра через
`SELECT *` по таблице с прокси, «отдаст клиенту ***, а не доступ к аккаунту».
Барьер при этом `proxy_url` не трогал — и у исключения была записана причина:
«приложение отправляет URL назад, когда проверяет сессию через этот прокси».

ЧТО ТЕПЕРЬ. Причину исключения убрали вместе с самим круговым рейсом:

  * список прокси отдаёт адрес с замаскированным паролем;
  * экран хранит и присылает только `proxy_id`;
  * сервер берёт адрес выбранного прокси из своего хранилища по этому id — и
    проверка сессии по-прежнему идёт ЧЕРЕЗ прокси, а не с общего IP сервера
    (иначе первая же проверка выдала бы когорту, против чего в том же файле
    раскладывается партия);
  * барьер в ответах API гасит пароль в адресе по имени поля, не ломая показ
    хоста.

Сырой адрес от клиента принимается ровно в одном случае, который без него не
работает: владелец вставил НОВЫЙ прокси в форму и не нажал «Добавить».
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from aiohttp import web

from services import mini_app_api as M
from services import session_importer as SI

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "mini_app" / "index.html"
UID = 707070
SECRET = "pa55word"
PLAIN_URL = f"socks5://user:{SECRET}@1.2.3.4:1080"


# ── Список прокси ────────────────────────────────────────────────────────────

class _ListPool:
    def __init__(self):
        self.calls: list[str] = []

    async def fetch(self, q, *a):
        self.calls.append(q)
        if "FROM user_proxies" in q:
            return [{"id": 1, "label": "Мой", "proxy_url": PLAIN_URL,
                     "proxy_type": "socks5", "is_active": True,
                     "is_alive": True, "last_check": None, "created_at": None,
                     "is_backup": False, "latency_ms": 12, "acc_count": 3}]
        return []

    async def fetchrow(self, q, *a):
        self.calls.append(q)
        return None

    async def fetchval(self, q, *a):
        self.calls.append(q)
        return 1

    async def execute(self, q, *a):
        self.calls.append(q)
        return "OK"


class _Req:
    def __init__(self, **query):
        self.headers: dict[str, str] = {}
        self.query = {k: str(v) for k, v in query.items()}
        self.query_string = "&".join(f"{k}={v}" for k, v in self.query.items())
        self.rel_url = type("U", (), {"query": self.query})()
        self.match_info: dict[str, str] = {}
        self.method = "GET"

    async def json(self):
        return {}


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    M._cache.clear()
    yield
    M._cache.clear()


def _call(pool, method: str, path: str, req):
    app = web.Application()
    M.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        rpath = info.get("path") or info.get("formatter") or ""
        if route.method == method and rpath == path:
            return asyncio.run(route.handler(req))
    raise AssertionError(f"маршрут {method} {path} не зарегистрирован")


def test_the_list_does_not_hand_out_the_password():
    pool = _ListPool()
    resp = _call(pool, "GET", "/api/miniapp/proxies", _Req(limit=50, offset=0))
    body = resp.body.decode("utf-8")
    assert SECRET not in body, (
        "пароль к прокси уехал в мини-апп целиком — а экран печатает только "
        f"хост:\n{body[:400]}")
    got = json.loads(body)["proxies"][0]["proxy_url"]
    assert got.split("@").pop() == "1.2.3.4:1080", (
        f"хост потерялся вместе с паролем: {got!r} — список прокси останется "
        "без имён")


def test_the_barrier_masks_it_even_if_a_handler_forgets():
    """Обработчик, написанный завтра через SELECT *, не отдаст доступ."""
    from services.secret_masking import scrub_payload

    out = scrub_payload({"rows": [{"proxy_url": PLAIN_URL}]})
    assert SECRET not in json.dumps(out, ensure_ascii=False)


# ── Импорт сессий ────────────────────────────────────────────────────────────

class _ImportPool:
    """Пул с одним прокси владельца. Запоминает, о чём спрашивали."""

    def __init__(self, *, owns=True, stored="ENCRYPTED-BLOB"):
        self.owns, self.stored = owns, stored
        self.queries: list[str] = []

    async def fetchrow(self, q, *a):
        self.queries.append(q)
        if "FROM user_proxies" in q and self.owns:
            return {"proxy_url": self.stored}
        return None

    async def fetchval(self, q, *a):
        self.queries.append(q)
        return 0

    async def fetch(self, q, *a):
        self.queries.append(q)
        return []

    async def execute(self, q, *a):
        self.queries.append(q)
        return "OK"


def _run_import(monkeypatch, pool, **kw):
    """Импорт одной строки. Возвращает proxy_url, с которым звали проверку."""
    seen: list = []

    async def _validate(sess, proxy_url=None):
        seen.append(proxy_url)
        return {"valid": False, "error": "проверка не нужна этому тесту"}

    monkeypatch.setattr(SI, "detect_format", lambda line: "session_string")
    monkeypatch.setattr(SI, "extract_session_string", lambda line, fmt: "SESSION")
    monkeypatch.setattr(SI, "validate_session", _validate)
    asyncio.run(SI.import_sessions(pool, UID, "одна-строка", **kw))
    return seen


def test_the_import_takes_the_chosen_address_from_the_store(monkeypatch):
    """Прислали только id — адрес берём у себя, проверка идёт через прокси."""
    pool = _ImportPool(stored="ENCRYPTED-BLOB")
    seen = _run_import(monkeypatch, pool, proxy_id=5)
    assert seen == ["ENCRYPTED-BLOB"], (
        f"проверка сессии пошла с {seen!r}. None здесь означает «с общего IP "
        "сервера» — ровно тот признак когорты, из-за которого партия ниже "
        "раскладывается по разным выходам")


def test_the_stored_address_stays_encrypted_on_the_way(monkeypatch):
    """Расшифровывает account_manager._parse_proxy — здесь это лишний пароль
    в памяти процесса."""
    pool = _ImportPool(stored="ENCRYPTED-BLOB")
    seen = _run_import(monkeypatch, pool, proxy_id=5)
    assert seen[0] == "ENCRYPTED-BLOB"


def test_a_pasted_address_still_works(monkeypatch):
    """Единственный случай, когда сырой адрес от клиента нужен."""
    pool = _ImportPool(owns=False)
    calls: list = []

    async def _ensure(p, owner, url, label=None):
        calls.append(url)
        return 77

    monkeypatch.setattr(SI, "ensure_user_proxy", _ensure)
    seen = _run_import(monkeypatch, pool, proxy_url=PLAIN_URL)
    assert calls == [PLAIN_URL], "вставленный вручную прокси потерялся"
    assert seen == [PLAIN_URL], "проверка должна идти через него же"


def test_someone_elses_proxy_id_is_not_trusted(monkeypatch):
    """Чужой id не закрепляем и адрес по нему не ищем."""
    pool = _ImportPool(owns=False)
    seen = _run_import(monkeypatch, pool, proxy_id=5)
    assert seen == [None], f"по чужому id достали адрес: {seen!r}"


def test_the_api_ignores_a_client_address_when_an_id_is_given():
    """Подменить адрес у выбранного прокси через тело запроса нельзя."""
    src = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    i = src.index("async def import_sessions_api")
    body = src[i:src.index("app.router.add_post(\"/api/miniapp/import_sessions\"", i)]
    assert 'proxy = None if proxy_id else data.get("proxy_url")' in body, (
        "обработчик снова берёт адрес прокси из тела запроса при заданном id")


# ── Экран ────────────────────────────────────────────────────────────────────

def test_the_screen_keeps_no_proxy_address():
    html = INDEX.read_text(encoding="utf-8")
    assert "dataset.url=p.proxy_url" not in html, (
        "выпадашка снова прячет полный адрес прокси в разметке страницы")


def _js_function(html: str, signature: str) -> str:
    """Тело JS-функции по балансу скобок.

    Окно фиксированной длины здесь нельзя: внутри стоит ОТРИЦАТЕЛЬНАЯ проверка
    («dataset» не должно быть), и стоит коду сдвинуться — окно промахнётся мимо
    функции, «искомого нет» станет правдой, и защита выключится молча. Это и
    стережёт tests/test_no_silently_disabled_guards.py.
    """
    i = html.index(signature)
    j = html.index("{", i)
    depth = 0
    for k in range(j, len(html)):
        if html[k] == "{":
            depth += 1
        elif html[k] == "}":
            depth -= 1
            if depth == 0:
                return html[i:k + 1]
    raise AssertionError(f"не нашёл конец функции {signature}")


def test_the_screen_sends_only_the_id_for_a_saved_proxy():
    html = INDEX.read_text(encoding="utf-8")
    body = _js_function(html, "async function submitAccImport()")
    assert "dataset" not in body, (
        "адрес выбранного прокси снова уходит на сервер из разметки")
    assert re.search(r"if\(!proxyId && newProxy\)\{ proxyUrl=newProxy; \}", body), (
        "потерялся единственный законный случай сырого адреса — вставленный "
        "вручную новый прокси")


def test_the_screen_still_shows_the_host():
    """Маскировка не должна оставить список прокси без имён."""
    html = INDEX.read_text(encoding="utf-8")
    assert html.count("(p.proxy_url||'').split('@').pop()") >= 3
