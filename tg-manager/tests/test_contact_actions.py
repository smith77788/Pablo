"""Действия по контакту из карточки: написать в ЛС / пригласить в чат.

Пользователь на карточке контакта выбирает один/несколько своих аккаунтов и с
них пишет контакту или приглашает его в канал/чат. Реюзаем боевые движки
(bulk_dm_adhoc / mass_invite), а не наивный цикл — темп и защита от бана те же.

mini_app_api не импортируется в песочнице — проверяем на уровне исходников.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_backend_endpoints_and_routes():
    api = _read("services/mini_app_api.py")
    assert "async def uch_contact_message" in api
    assert "async def uch_contact_invite" in api
    # маршруты зарегистрированы
    assert '/message", uch_contact_message)' in api
    assert '/invite", uch_contact_invite)' in api


def test_message_reuses_dm_engine_and_invite_reuses_invite_engine():
    api = _read("services/mini_app_api.py")
    msg = api[api.index("async def uch_contact_message"):api.index("async def uch_contact_invite")]
    # ЛС идёт через безопасный DM-движок, а не прямой send
    assert '"bulk_dm_adhoc"' in msg
    assert '"usernames": [ref]' in msg
    inv = api[api.index("async def uch_contact_invite"):]
    inv = inv[:inv.index("async def uch_contact_update")]
    assert '"mass_invite"' in inv
    assert '"source": "import_list"' in inv
    # способ инвайта на выбор (паритет с массовым)
    assert '"invite_method"' in inv


def test_target_resolver_prefers_username_then_id():
    api = _read("services/mini_app_api.py")
    assert "def _uch_msg_ref" in api
    assert "def _uch_invite_target" in api
    ref = api[api.index("def _uch_msg_ref"):api.index("def _uch_invite_target")]
    # username → id; телефон в ЛС НЕ используем (ненадёжно)
    assert '"username"' in ref and "telegram_user_id" in ref
    inv = api[api.index("def _uch_invite_target"):]
    inv = inv[:inv.index("async def uch_contact_message")]
    # для инвайта телефон допустим
    assert "phones" in inv


def test_frontend_has_buttons_modals_and_submit():
    ui = _read("mini_app/index.html")
    # кнопки на карточке
    assert "openContactWrite()" in ui
    assert "openContactInvite()" in ui
    # модалки
    assert 'id="mo-contact-write"' in ui
    assert 'id="mo-contact-invite"' in ui
    # выбор аккаунтов (мультивыбор) + submit дергает новые эндпоинты
    assert "_renderContactAccPicker" in ui
    assert "ca-acc-cb" in ui
    assert "/message'" in ui and "/invite'" in ui
    # выбор способа инвайта
    assert 'id="cciMethod"' in ui


def test_frontend_picker_defaults_to_source_accounts():
    ui = _read("mini_app/index.html")
    # по умолчанию отмечаем аккаунты, где контакт найден (sources)
    assert "window._cdSources" in ui
    fn = ui[ui.index("function openContactWrite"):]
    fn = fn[:fn.index("async function submitContactWrite")]
    assert "_cdSources" in fn and "account_id" in fn
