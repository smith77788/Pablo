"""Рабочее пространство: видно, кто в нём, и лишнего можно убрать.

Экран «🏢 Рабочие пространства» показывал строку «3 участника» и кнопку выхода.
Кто эти трое — узнать было негде: состав, выписка приглашений и вход по коду
в продукте есть (database/db.py, bot/handlers/workspaces.py), но только из
Telegram-бота. При этом вход в пространство — это доступ к ботам владельца
(get_bot отдаёт их с РАСШИФРОВАННЫМ токеном) и к его каналам, то есть самое
дорогое, что можно раздать.

Тест держит две вещи: состав виден, и права проверяются не в интерфейсе.
Кнопка «Исключить» рисуется только владельцу и администратору, но решает не
она — решает remove_workspace_member, куда номер пространства и номер
участника приходят от клиента.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
DB = open(os.path.join(ROOT, "database", "db.py"), encoding="utf-8").read()
SNAPSHOT = open(os.path.join(ROOT, "tests", "miniapp_routes_snapshot.txt"),
                encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def _handler(name: str) -> str:
    i = API.index(f"async def {name}(request")
    j = API.index("\n    async def ", i + 10)
    return API[i:j]


def _pyfn(src: str, name: str) -> str:
    i = src.index(f"async def {name}(")
    j = src.index("\nasync def ", i + 10)
    return src[i:j]


def test_routes_exist():
    for r in ("GET /api/miniapp/workspace/{ws_id}",
              "POST /api/miniapp/workspace/{ws_id}/invite",
              "POST /api/miniapp/workspace/join",
              "DELETE /api/miniapp/workspace/{ws_id}/member/{member_id}"):
        assert r in SNAPSHOT, f"нет маршрута: {r}"


def test_detail_is_scoped_to_members():
    """Состав — чужие имена и user_id: по одному номеру пространства не отдаём."""
    h = _handler("workspace_detail")
    assert "get_workspace_role(pool, ws_id, uid)" in h, (
        "роль смотрящего не проверяется — состав чужого пространства наружу")
    assert "viewer_id=uid" in h, "состав берётся без скоупа по смотрящему"


def test_remove_checks_rights_in_db_not_in_the_screen():
    fn = _pyfn(DB, "remove_workspace_member")
    assert "WORKSPACE_INVITE_ROLES" in fn, (
        "исключать может кто угодно — право не проверено в базе")
    assert 'return "owner"' in fn, (
        "владельца можно исключить — пространство осталось бы без хозяина")
    assert "record_manual_action" in fn, "исключение не попадает в журнал"
    h = _handler("workspace_member_remove")
    assert "remove_workspace_member" in h


def test_invite_right_is_not_decided_by_the_button():
    h = _handler("workspace_invite_create")
    assert "create_workspace_invite(pool, ws_id, uid)" in h, (
        "код приглашения выписывается в обход проверки прав")
    assert "403" in h, "отказ по правам уходит не как 403"


def test_join_validates_the_code():
    h = _handler("workspace_join")
    assert "validate_string" in h, "код приглашения уходит в базу без проверки"
    assert "use_workspace_invite" in h, (
        "вход по коду не идёт через проверку срока и остатка попыток")


def test_screen_shows_who_is_inside():
    f = _fn("openWsDetail")
    assert "members" in f, "состав не выводится"
    assert "joined_at" in f, "не видно, когда человек вошёл"
    assert "WS_ROLE_RU" in f, "роль показывается ключом из базы"


def test_roles_are_russian():
    i = HTML.index("const WS_ROLE_RU")
    block = HTML[i:i + 260]
    for key in ("owner:", "admin:", "member:", "viewer:"):
        assert key in block, f"нет русской подписи роли {key}"
    assert "'viewer'" not in _fn("openWsDetail").replace("WS_ROLE_RU", "")


def test_remove_button_only_where_it_can_work():
    f = _fn("openWsDetail")
    assert "d.can_invite && !m.is_me && m.role!=='owner'" in f, (
        "кнопка «Исключить» рисуется там, где сервер всё равно откажет")


def test_invite_code_stays_on_screen():
    """Код показывается один раз: выписка нового отнимает попытку."""
    assert "_WS_INVITE" in _fn("createWsInvite")
    f = _fn("openWsDetail")
    assert "_WS_INVITE" in f, "выписанный код негде прочитать и переслать"
    assert "copyToClipboard" in f


def test_list_row_opens_the_workspace():
    f = _fn("loadWorkspaces")
    assert "openWsDetail(" in f, "строка пространства ни на что не нажимается"
    assert "errHtml(" in f, "ошибка списка — тупик без кнопки"


def test_joining_by_code_is_reachable_from_the_app():
    assert "joinWsByCode()" in HTML, "войти по коду можно только из бота"
    f = _fn("joinWsByCode")
    assert "method:'POST'" in f and "JSON.stringify" in f


def test_leaving_from_the_card_returns_to_the_list():
    f = _fn("leaveWs")
    assert "s-wsdetail" in f, (
        "после удаления остаёмся на карточке несуществующего пространства")
