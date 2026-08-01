"""Регрессия: у DM-рассылок не было авто-темпа (паритет с масс-инвайтом).

Жалоба: «у модулей нет нужных настроек, что есть у конкурентов». Масс-инвайт умеет
pace="auto" — темп считается по состоянию ВСЕГО флота за сегодня
(flood_engine.auto_strategy): флуд у одного аккаунта тормозит всех (Telegram смотрит
на аккаунты как на группу). DM-рассылка же предлагала только slow/normal/fast — три
числа, выбранные вслепую, без учёта здоровья флота. Это разрыв именно в
анти-детект-слое (там глубина важнее минимализма).

Фикс (переиспользуем существующий flood_engine.auto_strategy, без нового модуля):
- dm_engine.run_campaign: pace=="auto" → pace_mult из auto_strategy (fail-safe в
  normal при сбое — «авто» не опаснее обычного);
- dm_campaign_create принимает "auto" в whitelist темпа;
- UI (форма кампании): опция «🤖 Авто».

Тесты падают без фикса: раньше "auto" отбрасывался в normal, движок его не знал,
опции в UI не было.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import dm_engine, mini_app_api


def _index_html() -> str:
    return (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_dm_engine_handles_auto_pace_via_flood_engine():
    src = inspect.getsource(dm_engine.run_campaign)
    assert '_pace == "auto"' in src, "run_campaign должен обрабатывать pace='auto'"
    assert "auto_strategy" in src, (
        "авто-темп должен считаться через flood_engine.auto_strategy (единый источник), "
        "а не выдумывать свой множитель"
    )


def test_dm_engine_auto_is_not_more_dangerous_on_failure():
    """«Авто» при сбое расчёта обязан падать в нейтральный темп (1.0), а не в
    самый быстрый — иначе сбой стратегии = максимальный риск бана."""
    src = inspect.getsource(dm_engine.run_campaign)
    # в ветке auto есть except, и множитель по умолчанию 1.0 (normal), не 0.5
    auto_block = src.split('_pace == "auto"', 1)[1].split("else:", 1)[0]
    assert "except Exception" in auto_block, "сбой auto_strategy должен ловиться"
    assert "_pace_mult = 1.0" in auto_block, (
        "при сбое авто-темпа берём нейтральный 1.0, а не быстрый"
    )


def test_dm_create_accepts_auto_pace():
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def dm_campaign_create\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "dm_campaign_create не найден"
    body = m.group(1)
    assert '"slow", "normal", "fast", "auto"' in body, (
        "whitelist темпа DM должен принимать 'auto' (иначе он молча коэрсится в normal)"
    )


def test_dm_ui_has_auto_pace_option():
    html = _index_html()
    # опция auto именно в селекторе темпа кампании cmpPace
    m = re.search(r'<select id="cmpPace">(.*?)</select>', html, re.DOTALL)
    assert m, "селектор темпа DM cmpPace не найден"
    assert 'value="auto"' in m.group(1), "в темпе DM-кампании нет опции «Авто»"
