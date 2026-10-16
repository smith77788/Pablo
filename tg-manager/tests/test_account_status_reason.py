"""«Нету информации» о причине: аккаунт нёс машинный status_reason (op_worker
писал «network/proxy failure …», «session_expired …»), но пользователь его
никогда не видел — не понимал, ПОЧЕМУ активный аккаунт не работает.

Фикс: account_detail отдаёт status_reason; UI humanize-ит его (accReasonHuman) и
показывает «почему не работает» + путь к фиксу.
"""
from __future__ import annotations

import re

from tests.miniapp_source import api_source, js_function, miniapp_source


def _api_src() -> str:
    return api_source()


def _index_html() -> str:
    # Весь фронт: accReasonHuman уехала в screens/accounts_list.js.
    return miniapp_source()


def test_account_detail_selects_status_reason():
    src = _api_src()
    m = re.search(r"async def account_detail\(.*?\n(.*?)\n    async def accounts_export",
                  src, re.DOTALL)
    assert m, "account_detail не найден"
    body = m.group(1)
    # обе ветки (admin и owner) должны тянуть status_reason
    assert body.count("status_reason") >= 2, (
        "status_reason должен быть в обоих SELECT (admin + owner)"
    )


def test_ui_humanizes_status_reason():
    html = _index_html()
    assert "function accReasonHuman(" in html, "нужен humanizer причины"
    # ключевые классы причин → человекочитаемые подсказки
    for token in ("proxy", "flood", "session", "ban"):
        assert token in html.lower()
    # причина реально попадает в карточку аккаунта
    assert "accReasonHuman(a.status_reason)" in html
    assert "Последний сбой" in html


def test_humanizer_maps_known_reasons_locally():
    """Логику humanizer можно проверить, вычленив тело функции и прогнав как JS
    нельзя без node — поэтому проверяем текстовые ветки присутствуют."""
    # Границы по скобкам: accReasonHuman уехала в screens/accounts_list.js, а
    # buildAccDetail осталась в index.html — срез между ними не существует, а
    # до выноса он прихватывал ещё и половину соседних функций.
    seg = js_function("accReasonHuman")
    assert "Проблема с прокси" in seg
    assert "FloodWait" in seg
    assert "переавторизац" in seg.lower()
    assert "ограничен Telegram" in seg
