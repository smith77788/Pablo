"""SEO → организм: находимость каналов в мир-снимке + подсказка мозга.

Модуль SEO переставал быть островом: seo_advisor уже даёт анализ и рекомендации,
но организм (мозг подсказок) о находимости не знал. Теперь world._seo считает
слабо оптимизированные объекты, а мозг предлагает «оптимизировать SEO» цепочкой
в существующий экран.
"""
from __future__ import annotations

import asyncio
import os

from services.organism.brain import build_suggestions
from services.organism import world

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _snap(**over):
    base = {
        "fleet": {"accounts": 20, "active": 18, "dead": 0, "pressure": 20,
                  "governor_mult": 1.0, "governor_level": "green", "geo": {}},
        "ops": {"running": 0, "pending": 0, "failed_24h": 0, "last_failed": None},
        "graph": {"contacts": 500, "hot_leads": 0, "intents_24h": 0},
        "vault": {"health": "ok", "stale_days": 0},
        "goal": None,
        "growth": {"channels": 0, "growth_ops_7d": 0},
        "seo": {"scored": 0, "weak": 0, "worst": None},
        "bots": {"total": 0, "active": 0, "inactive": 0},
    }
    for k, v in over.items():
        base[k] = {**(base.get(k) or {}), **v} if isinstance(v, dict) else v
    return base


def test_weak_seo_raises_suggestion():
    sugs = build_suggestions(_snap(seo={"scored": 3, "weak": 2, "worst": "Мой канал"}))
    s = next((x for x in sugs if x["id"] == "seo_weak"), None)
    assert s is not None
    assert s["action"]["kind"] == "seo"
    assert "2" in s["title"]
    assert "Мой канал" in s["why"]        # имя худшего объекта в тексте


def test_no_weak_seo_no_suggestion():
    assert not any(s["id"] == "seo_weak"
                   for s in build_suggestions(_snap(seo={"scored": 5, "weak": 0, "worst": None})))
    # снимок без блока seo вообще — тоже без подсказки (fail-open)
    base = _snap()
    del base["seo"]
    assert not any(s["id"] == "seo_weak" for s in build_suggestions(base))


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, q, *a):
        return self._rows


def test_world_seo_counts_weak_and_worst():
    # два пустых (grade 'red', score 40) + один сильный (полный текст с ключами)
    rows = [
        {"title": "", "username": "", "about": ""},
        {"title": "", "username": "", "about": ""},
        {"title": "Крипта Новости Аналитика", "username": "cryptonews",
         "about": "Ежедневная аналитика рынка криптовалют, сигналы и обзоры — подпишитесь."},
    ]
    out = asyncio.run(world._seo(_FakePool(rows), 1))
    assert out["scored"] == 3
    assert out["weak"] == 2          # два слабых, сильный не считается
    assert out["worst"]              # имя худшего заполнено (fallback «Канал»)


def test_world_seo_fail_open_on_db_error():
    class Boom:
        async def fetch(self, q, *a):
            raise RuntimeError("db down")

    out = asyncio.run(world._seo(Boom(), 1))
    assert out == {"scored": 0, "weak": 0, "worst": None}


def test_world_snapshot_wires_seo_block():
    src = open(os.path.join(ROOT, "services", "organism", "world.py"), encoding="utf-8").read()
    assert "async def _seo" in src
    assert '"seo": await _seo(' in src
    assert "seo_advisor" in src


def test_pulse_action_routes_seo():
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    assert "if (k==='seo') return openSeo();" in html
    assert "seo:'Оптимизировать SEO'" in html
