"""Сегмент → действие флотом (бэкенд): написать/медиа/пригласить по срезу.

mini_app_api не импортируется в песочнице — проверяем контракт по исходникам:
эндпоинты, маршруты, реюз движков, авто-разбивка ЛС на чанки ≤1000, гейт давления.
"""
from __future__ import annotations

import os


# Весь API-слой мини-аппа: ручки контактов вынесены из 25-тысячестрочного
# mini_app_api.py в services/mini_app_uch.py, и проверка не должна зависеть
# от того, в каком файле лежит обработчик.
def _api_src() -> str:
    from tests.miniapp_source import api_source

    return api_source()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _slice(api: str, start: str, end: str) -> str:
    return api[api.index(start):api.index(end)]


def test_endpoints_and_routes():
    api = _api_src()
    for fn in ("uch_segment_preview", "uch_segment_message",
               "uch_segment_media", "uch_segment_invite"):
        assert f"async def {fn}" in api
        assert f", {fn})" in api  # маршрут зарегистрирован


def test_message_autosplits_into_chunks():
    api = _api_src()
    msg = _slice(api, "async def uch_segment_message", "async def uch_segment_media")
    assert "_SEG_DM_CHUNK" in msg
    # Разбивка на чанки по получателям варианта (vrefs — аудитория A/B-варианта
    # либо весь сегмент без A/B).
    assert "range(0, len(vrefs), _SEG_DM_CHUNK)" in msg
    assert '"bulk_dm_adhoc"' in msg
    assert '"op_ids"' in msg and '"chunks"' in msg
    # гейт давления перед постановкой (ban-safety)
    assert "_segment_pressure_ok" in msg


def test_media_writes_own_file_per_chunk():
    api = _api_src()
    med = _slice(api, "async def uch_segment_media", "async def uch_segment_invite")
    # каждый чанк — своя копия файла (op удаляет свою; общий путь → гонка удаления)
    assert "mkstemp" in med and "range(0, len(refs), _SEG_DM_CHUNK)" in med
    assert '"media_path": path' in med


def test_invite_uses_mass_invite_no_chunk():
    api = _api_src()
    inv = _slice(api, "async def uch_segment_invite", "async def uch_contact_update")
    assert '"mass_invite"' in inv and '"source": "import_list"' in inv
    assert "_uch_invite_target" in inv
    assert "invite_method" in inv


def test_conservative_defaults():
    api = _api_src()
    msg = _slice(api, "async def uch_segment_message", "async def uch_segment_media")
    # консервативный темп по умолчанию (45с)
    assert '"delay", 45' in msg
    # кап чанка = 1000, и он один на весь продукт: число живёт в общем модуле
    # среза, а не дублируется в каждом файле, который его читает
    from services.contacts_hub import segments

    assert segments.SEG_DM_CHUNK == 1000
    assert "_SEG_DM_CHUNK = _segments.SEG_DM_CHUNK" in api


def test_frontend_segment_controls():
    ui = _read("mini_app/index.html")
    # оси фильтра: пол + CRM-стадия
    assert "toggleGenderFilter" in ui and 'class="chip contact-gender"' in ui
    assert "toggleStageFilter" in ui and 'class="chip contact-stage"' in ui
    # действия по сегменту в панели выбора + тумблер «весь срез»
    assert "openSegmentWrite()" in ui and "openSegmentInvite()" in ui
    assert "toggleSegmentWhole" in ui and "SEGMENT_WHOLE" in ui
    # единый источник фильтров среза для списка и действия
    assert "_currentSegmentFilters" in ui
    # превью сегмента + сегментные эндпоинты
    assert "/uch/segment/preview" in ui
    assert "/uch/segment/message" in ui and "/uch/segment/media" in ui and "/uch/segment/invite" in ui
