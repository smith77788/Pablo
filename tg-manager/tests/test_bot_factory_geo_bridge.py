"""Фабрика ботов подключена к Гео-сети: создание ботов по гео с SEO-ключами.

Раньше из экрана «Фабрика ботов» нельзя было создать ботов по городам/странам
с локализованными SEO-именами — это умеет только пайплайн Гео-сети (op
global_presence_bot: плейсхолдеры {{CITY_NAME}}/{{COUNTRY}} + перестановка/
спинтакс). Второго пайплайна в фабрике НЕ заводим (анти-дубль) — вместо этого
кнопка-мост открывает модалку Гео-сети сразу на типе актива «бот».
"""
from __future__ import annotations

import pathlib
import re

HTML = (pathlib.Path(__file__).resolve().parents[1] / "mini_app" / "index.html").read_text("utf-8")


def test_bot_factory_has_geo_bridge_button():
    # В режиме «создать новых» есть вход в гео-создание ботов.
    assert "openGpCreateModal('bot')" in HTML
    assert "🌍 Создать ботов по гео" in HTML


def test_gp_modal_accepts_preset_asset_type():
    # Модалка Гео-сети умеет открыться на заданном типе актива (мост даёт 'bot').
    m = re.search(r"async function openGpCreateModal\(([^)]*)\)", HTML)
    assert m and "presetAssetType" in m.group(1), "openGpCreateModal должна принимать тип актива"
    assert "if (_at && presetAssetType) _at.value = presetAssetType" in HTML


def test_geo_bot_pipeline_is_live():
    # Цель моста — реальный, зарегистрированный пайплайн.
    from services.operation_bus import OP_REGISTRY
    assert "global_presence_bot" in OP_REGISTRY
    api = (pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py").read_text("utf-8")
    assert '"global_presence_bot": _exec_global_presence_bot' in api
    # Гео-сеть предлагает тип актива «бот» в модалке.
    assert '<option value="bot">' in HTML


# ── Фабрика каналов/чатов → Гео-сеть ──────────────────────────────────────────

def test_channel_factory_has_geo_bridge():
    # Кнопка-мост на экране фабрики каналов + хелпер, выбирающий тип актива по
    # чекбоксу «группа/канал».
    assert "cfOpenGeo()" in HTML
    m = re.search(r"function cfOpenGeo\(\)\s*\{(.*?)\}", HTML, re.S)
    assert m, "нет хелпера cfOpenGeo"
    body = m.group(1)
    assert "cfIsGroup" in body
    assert "openGpCreateModal(" in body
    assert "'group'" in body and "'channel'" in body


def test_geo_channel_pipeline_is_live():
    from services.operation_bus import OP_REGISTRY
    assert "global_presence_channel" in OP_REGISTRY
    assert '<option value="channel">' in HTML and '<option value="group">' in HTML
