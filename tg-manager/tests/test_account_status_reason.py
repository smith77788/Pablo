"""«Нету информации» о причине: аккаунт нёс машинный status_reason (op_worker
писал «network/proxy failure …», «session_expired …»), но пользователь его
никогда не видел — не понимал, ПОЧЕМУ активный аккаунт не работает.

Фикс: account_detail отдаёт status_reason; UI humanize-ит его (accReasonHuman) и
показывает «почему не работает» + путь к фиксу.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _index_html() -> str:
    p = Path(__file__).resolve().parent.parent / "mini_app" / "index.html"
    return p.read_text(encoding="utf-8")


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
    html = _index_html()
    seg = html[html.index("function accReasonHuman("):]
    seg = seg[:seg.index("function buildAccDetail")]
    assert "Проблема с прокси" in seg
    assert "FloodWait" in seg
    assert "переавторизац" in seg.lower()
    assert "ограничен Telegram" in seg
