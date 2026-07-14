"""Проход по 5 направлениям (Strike, SEO, Dashboard, Гео, паритет бот↔mini-app)
как единый организм: пульс/сигналы связывают модули.

- Strike: reflex пульса (fail-open) бережёт флагнутые аккаунты от добивания.
- Dashboard: приборный щиток — реальные vitals SEO + гео (owner-scoped, fail-soft).
- Паритет: бот /dashboard показывает те же SEO/гео/карантин, что и mini-app.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_strike_respects_quarantine_fail_open():
    """mass_report не бросает в бой карантинные аккаунты, но fail-open: пустой
    фильтр не обнуляет операцию (лучше рискнуть, чем no-op)."""
    se = _read("services/strike_engine.py")
    seg = se[se.index("viable_accounts = preflight_accounts"):]
    seg = seg[:2000]
    assert "is_account_quarantined" in seg
    assert "if _healthy:" in seg  # пустой фильтр → оставляем исходный список
    assert "except Exception:" in seg  # fail-open


def test_dashboard_has_seo_and_geo_vitals():
    """Дашборд — приборный щиток: реальные SEO (tracked_keywords) + гео
    (global_presence_plans), owner-scoped, fail-soft."""
    api = _read("services/mini_app_api.py")
    assert "async def _seo_vitals" in api and "async def _geo_vitals" in api
    assert "FROM tracked_keywords WHERE owner_id=$1" in api
    assert "FROM global_presence_plans WHERE owner_id=$1" in api
    # реально инъектятся в ответ дашборда
    assert '"seo": await _seo_vitals(uid)' in api
    assert '"geo": await _geo_vitals(uid)' in api
    ui = _read("mini_app/index.html")
    assert "dk-seo" in ui and "dk-geo" in ui
    assert "d.seo" in ui and "d.geo" in ui


def test_bot_dashboard_parity_seo_geo_pulse():
    """Bot-паритет: /dashboard в боте показывает те же SEO/гео/карантин, что mini-app."""
    md = _read("bot/handlers/metrics_dashboard.py")
    assert "tracked_keywords" in md and "global_presence_plans" in md
    assert "get_account_health" in md and "quarantine" in md
    # источники owner-scoped, обёрнуты в fail-soft
    assert "WHERE owner_id=$1" in md
