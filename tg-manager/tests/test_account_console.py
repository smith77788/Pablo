"""Живая консоль аккаунта: диалоги / история / отправка / контакты.

Экран карточки аккаунта раньше был стеной из 14 обрезанных кнопок и не давал
попасть в переписку самого аккаунта. Добавлены: консоль (диалоги→чат→история→
ответ) и контакты аккаунта, плюс входы в разделы Каналы/Боты/Инфраструктура/
Гео-Сети. Гейт держит цепочку целой: движок → маршруты API → экраны → входы.

Пустые хелперы движка тестируются здесь же (без сети и без telethon — в тестовой
среде его нет; движок импортирует telethon лениво внутри функций).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

_DIV = re.compile(r"<(/?)div\b", re.I)


def _screen_body(sid: str) -> str:
    m = re.search(rf'<div class="screen" id="{re.escape(sid)}"', HTML)
    assert m, f"экран {sid} не найден"
    start, depth = m.start(), 0
    for t in _DIV.finditer(HTML, start):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return HTML[start:t.end()]
    raise AssertionError(f"незакрытый экран {sid}")


# ── Чистые хелперы движка ────────────────────────────────────────────────────

def test_dialog_kind():
    from services import account_console as ac
    assert ac.dialog_kind(True, True, False, False) == "bot"   # бот раньше user
    assert ac.dialog_kind(True, False, False, False) == "user"
    assert ac.dialog_kind(False, False, True, False) == "group"
    assert ac.dialog_kind(False, False, False, True) == "channel"


def test_preview_collapses_and_truncates():
    from services import account_console as ac
    assert ac.preview("  многострочный\nтекст ") == "многострочный текст"
    assert ac.preview("x" * 200, 80).endswith("…")
    assert ac.preview(None) == ""


def test_classify_error_maps_known():
    from services import account_console as ac
    assert ac.classify_error("AuthKeyUnregisteredError")[0] == "session_expired"
    assert ac.classify_error("A wait of 30s (FloodWait)")[0] == "flood"
    assert ac.classify_error("proxy connect timeout")[0] == "network"
    assert ac.classify_error("UserPrivacyRestrictedError")[0] == "privacy"
    # Русский текст причины не пустой — его увидит пользователь.
    assert ac.classify_error("whatever")[1]


def test_parse_peer():
    from services import account_console as ac
    assert ac.parse_peer("-100500") == -100500   # marked id канала/чата
    assert ac.parse_peer("777") == 777
    assert ac.parse_peer("@durov") == "@durov"


def test_engine_has_session_functions():
    from services import account_console as ac
    for fn in ("list_dialogs", "get_history", "send_text", "list_contacts"):
        assert callable(getattr(ac, fn, None)), f"нет функции движка {fn}"


def test_to_ui_contact_shape():
    from services import account_console as ac
    ui = ac._to_ui_contact({"user_id": 5, "first_name": "Ян", "last_name": "П",
                            "username": "yan", "phone": "+7", "is_premium": True,
                            "access_hash": 42})
    assert ui == {"id": 5, "name": "Ян П", "username": "yan", "phone": "+7",
                  "access_hash": "42", "is_bot": False, "premium": True, "mutual": False}
    # 64-битный hash не должен терять точность: отдаём строкой.
    big = ac._to_ui_contact({"user_id": 5, "access_hash": 7477083978437332073})
    assert big["access_hash"] == "7477083978437332073"
    # Без имени — падать на @username, потом на id.
    assert ac._to_ui_contact({"user_id": 9, "username": "u"})["name"] == "@u"
    assert ac._to_ui_contact({"user_id": 9})["name"] == "9"


def test_list_contacts_merges_book_and_dialogs(monkeypatch):
    """Контакты аккаунта = адресная книга + собеседники ЛС (книга приоритетна).

    Без второго источника «рабочий» аккаунт с пустой книгой, но живой перепиской
    показывал бы «нет контактов» — ту же ошибку уже ловил контакт-хаб.
    """
    import asyncio
    import sys
    import types
    from services import account_console as ac

    stub = types.ModuleType("services.account_manager")

    async def get_contacts(s, a=None):
        return [{"user_id": 1, "first_name": "Ann", "username": "ann",
                 "phone": "+7", "is_premium": True, "is_mutual": True}]

    async def get_dialog_contacts(s, limit=500, _acc=None):
        return [{"user_id": 1, "first_name": "AnnFromDialog"},   # дубль — книга победит
                {"user_id": 2, "first_name": "Bob"}]              # новый — добавится

    stub.get_contacts = get_contacts
    stub.get_dialog_contacts = get_dialog_contacts
    monkeypatch.setitem(sys.modules, "services.account_manager", stub)

    res = asyncio.run(ac.list_contacts("sess", {"id": 9}))
    assert res["ok"]
    by_id = {c["id"]: c for c in res["contacts"]}
    assert set(by_id) == {1, 2}
    assert by_id[1]["name"] == "Ann" and by_id[1]["phone"] == "+7"  # книга, не диалог
    assert by_id[2]["name"] == "Bob" and by_id[2]["phone"] is None


def test_list_contacts_survives_dialog_failure(monkeypatch):
    """Сбой сбора диалогов не должен ронять уже полученную адресную книгу."""
    import asyncio
    import sys
    import types
    from services import account_console as ac

    stub = types.ModuleType("services.account_manager")

    async def get_contacts(s, a=None):
        return [{"user_id": 1, "first_name": "Ann"}]

    async def get_dialog_contacts(s, limit=500, _acc=None):
        raise RuntimeError("dialogs boom")

    stub.get_contacts = get_contacts
    stub.get_dialog_contacts = get_dialog_contacts
    monkeypatch.setitem(sys.modules, "services.account_manager", stub)

    res = asyncio.run(ac.list_contacts("sess", {"id": 9}))
    assert res["ok"] and len(res["contacts"]) == 1


# ── Маршруты API ─────────────────────────────────────────────────────────────

def test_api_routes_registered():
    for route in (
        '"/api/miniapp/account/{acc_id}/dialogs"',
        '"/api/miniapp/account/{acc_id}/dialog/{peer}/history"',
        '"/api/miniapp/account/{acc_id}/dialog/{peer}/send"',
        '"/api/miniapp/account/{acc_id}/contacts"',
    ):
        assert route in API, f"маршрут не зарегистрирован: {route}"


def test_api_scopes_by_owner():
    """Сессия аккаунта достаётся строго по владельцу — иначе кросс-тенантный
    доступ к чужим перепискам."""
    assert "WHERE id=$1 AND owner_id=$2 AND is_active=TRUE" in API


# ── Экраны и входы ───────────────────────────────────────────────────────────

def test_console_screens_exist():
    for sid in ("s-accdialogs", "s-accchat", "s-acccontacts"):
        assert f'id="{sid}"' in HTML, f"экран {sid} отсутствует"


def test_console_screens_reachable():
    """К каждому экрану консоли есть хотя бы один переход push()."""
    for sid in ("s-accdialogs", "s-accchat", "s-acccontacts"):
        assert re.search(rf"push\('{sid}'\)", HTML), f"к {sid} нет входа push()"


def test_account_detail_wires_console_and_sections():
    """Карточка аккаунта ведёт в консоль и в разделы — иначе входы недостижимы."""
    body = _screen_body("s-accdetail")  # экран есть
    assert body
    # Кнопки строятся в JS buildAccDetail — проверяем по исходнику функции.
    m = re.search(r"function buildAccDetail\(", HTML)
    assert m
    nxt = re.search(r"\n(?:async )?function ", HTML[m.end():])
    chunk = HTML[m.start(): m.end() + (nxt.start() if nxt else 16000)]
    for call in ("openAccountDialogs(", "openAccountContacts(",
                 "openChannels()", "goTab('bots')", "openInfra()", "openGlobalPresence()"):
        assert call in chunk, f"в карточке аккаунта нет входа: {call}"


def test_chat_can_send():
    """Чат обязан уметь отправлять: инпут + функция отправки на нужный маршрут."""
    chat = _screen_body("s-accchat")
    assert "sendAccountChat()" in chat and 'id="achatInput"' in chat
    m = re.search(r"function sendAccountChat\(", HTML)
    assert m, "нет функции отправки"
    assert "/send" in HTML[m.start(): m.start() + 800]


def test_send_targets_are_escaped():
    """Внешний контент (тексты/имена) рендерится через esc() — защита от инъекций."""
    m = re.search(r"function reloadAccountChat\(", HTML)
    assert m
    body = HTML[m.start(): m.start() + 1600]
    assert "esc(m.text)" in body, "текст сообщения не экранируется"


# ── Слой 2: файлы / любой контакт / множественный выбор ──────────────────────

def test_send_file_engine_and_route():
    from services import account_console as ac
    import inspect
    assert callable(getattr(ac, "send_file", None)), "нет движка отправки файла"
    assert "access_hash" in inspect.signature(ac.send_file).parameters
    assert '"/api/miniapp/account/{acc_id}/dialog/{peer}/send_file"' in API, \
        "маршрут отправки файла не зарегистрирован"


def test_access_hash_threaded_everywhere():
    """access_hash проходит через историю/текст/файл — иначе диалог с контактом
    без общего чата не открыть и не написать."""
    from services import account_console as ac
    import inspect
    for fn in ("get_history", "send_text", "send_file"):
        assert "access_hash" in inspect.signature(getattr(ac, fn)).parameters, fn
    # Короткий путь: есть access_hash → InputPeerUser без запросов.
    src = (ROOT / "services" / "account_console.py").read_text(encoding="utf-8")
    assert "InputPeerUser" in src, "нет прямого резолва по access_hash"


def test_file_composer_present():
    chat = _screen_body("s-accchat")
    assert 'id="achatFile"' in chat and "sendAccountFile(" in chat, "нет прикрепления файла"
    m = re.search(r"function sendAccountFile\(", HTML)
    assert m and "/send_file" in HTML[m.start(): m.start() + 900]
    assert "20*1024*1024" in HTML[m.start(): m.start() + 900], "нет лимита размера на фронте"


def test_write_any_contact_uses_access_hash():
    """writeContact открывает чат и по @username, и по id+access_hash."""
    m = re.search(r"function writeContact\(", HTML)
    assert m
    body = HTML[m.start(): m.start() + 500]
    assert "c.access_hash" in body, "контакт без @username не резолвится по access_hash"


def test_multiselect_group_send_reuses_safe_dm():
    """«Написать всем» идёт через безопасный DM-движок (operation_bus), не циклом."""
    for fn in ("toggleAccContSelect", "toggleAccContPick", "writeSelectedContacts", "submitAccContMsg"):
        assert re.search(rf"function {fn}\(", HTML), f"нет функции {fn}"
    m = re.search(r"function submitAccContMsg\(", HTML)
    body = HTML[m.start(): m.start() + 700]
    assert "/api/miniapp/dm/adhoc_send" in body, "групповая отправка не через безопасный adhoc-путь"
    cont = _screen_body("s-acccontacts")
    assert "writeSelectedContacts()" in cont and 'id="accContBulk"' in cont
