"""Ядро, шаг 2: прокси/IP аккаунта видны в карточке и назначение доходит до эффекта.

Раньше карточка позволяла НАЗНАЧИТЬ прокси, но нигде не показывала, через какой
прокси/IP аккаунт реально выходит — пользователь не мог ответить «мой реальный IP».
И модалка не предвыбирала текущий прокси → сохранение случайно снимало назначенный.

Фикс: account_detail возвращает `transport` (свой прокси = уникальный IP 1:1 с
хостом; иначе IPv6 или общий CF-relay — честно «не уникальный»); карточка рисует
строку «Прокси / IP»; модалка предвыбирает текущий прокси.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _index() -> str:
    return (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_account_detail_returns_transport_owner_scoped():
    src = _api_src()
    m = re.search(r"async def account_detail\(.*?\n(.*?)\n    async def accounts_export",
                  src, re.DOTALL)
    assert m, "account_detail не найден"
    body = m.group(1)
    # proxy_id тянется, транспорт собирается и отдаётся
    assert "proxy_id" in body
    assert '"transport": transport' in body
    # прокси владельца — owner-scoped (не показать чужой прокси/IP)
    assert "FROM user_proxies " in body and "WHERE id=$1 AND owner_id=$2" in body
    # хост берётся из расшифрованного proxy_url (это и есть выходной IP)
    assert "decrypt_token" in body
    # честная классификация: уникальный (proxy/ipv6) vs общий
    assert '"unique": True' in body and '"unique": False' in body


def test_card_shows_proxy_ip_row():
    html = _index()
    m = re.search(r"function buildAccDetail\(d\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m, "buildAccDetail не найдена"
    body = m.group(1)
    assert "d.transport" in body, "карточка должна читать transport"
    assert "Прокси / IP" in body, "должна быть строка «Прокси / IP»"
    # тап по строке ведёт в смену прокси с текущим id
    assert "openAccProxy(${a.id}, ${tp.proxy_id" in body


def test_proxy_modal_preselects_current():
    html = _index()
    m = re.search(r"async function openAccProxy\((.*?)\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m, "openAccProxy не найдена"
    args, body = m.group(1), m.group(2)
    assert "currentPid" in args, "openAccProxy должна принимать текущий прокси"
    assert "selected" in body, "текущий прокси должен предвыбираться (selected)"
    # кнопка «Прокси» в карточке передаёт текущий proxy_id
    assert "openAccProxy(${a.id}, ${a.proxy_id" in html
