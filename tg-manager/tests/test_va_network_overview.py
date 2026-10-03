"""Сетевая сводка виртуального администратора — взгляд руководителя сети.

Администратор ведёт каждый канал по отдельности; владельцу сотни каналов нужен
и общий срез: сколько администраторов работает, сколько сеть выпускает за
неделю, какой средний охват, что заходит по сети, сколько черновиков ждут и где
сбои. Здесь проверяется, что этот агрегат честно считается по таблицам
администратора и доходит до экрана.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import channel_admin as ca  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class _Pool:
    """Пул, отвечающий заранее заданными строками по ключевым словам SQL."""

    def __init__(self, answers=None, fail=False):
        self.answers = answers or {}
        self.fail = fail

    def _match(self, sql):
        for kw, val in self.answers.items():
            if kw in sql:
                return val
        return None

    async def fetchrow(self, sql, *a):
        if self.fail:
            raise RuntimeError("база недоступна")
        return self._match(sql)

    async def fetchval(self, sql, *a):
        if self.fail:
            raise RuntimeError("база недоступна")
        return self._match(sql)

    async def fetch(self, sql, *a):
        if self.fail:
            raise RuntimeError("база недоступна")
        return self._match(sql) or []


def test_overview_aggregates_all_slices():
    pool = _Pool({
        "FROM va_channel_admin a WHERE a.owner_id": {
            "installed": 5, "active": 3, "review": 1, "errors": 1, "members": 12000,
        },
        "count(DISTINCT channel_id) FROM managed_channels": 8,
        "count(*) AS posts_7d": {"posts_7d": 21, "avg_views": 543.7},
        "FROM va_admin_drafts WHERE owner_id": 4,
        "GROUP BY pillar ORDER BY score DESC": [
            {"pillar": "Польза", "score": 900, "views": 700, "posts": 5},
            {"pillar": "Новости", "score": 400, "views": 300, "posts": 8},
        ],
        "a.fail_streak > 0 OR a.last_error IS NOT NULL) ": [
            {"channel_id": 111, "title": "Мой канал", "last_error": "нет прав", "fail_streak": 3},
        ],
    })
    out = asyncio.run(ca.network_overview(pool, 1))
    assert out["admins_installed"] == 5 and out["admins_active"] == 3
    assert out["channels_total"] == 8
    assert out["posts_7d"] == 21 and out["avg_views_7d"] == 543
    assert out["members_total"] == 12000
    assert out["pending_drafts"] == 4
    assert out["errors"] == 1
    assert out["top_pillars"][0]["pillar"] == "Польза"
    assert out["top_pillars"][0]["avg_views"] == 700
    assert out["attention"][0]["channel_id"] == "111"
    assert out["attention"][0]["error"] == "нет прав"


def test_overview_is_failsoft():
    """Экран сети обязан открыться даже при полном отказе базы."""
    out = asyncio.run(ca.network_overview(_Pool(fail=True), 1))
    assert out["admins_installed"] == 0
    assert out["top_pillars"] == [] and out["attention"] == []


def test_empty_network_is_zeroes_not_error():
    out = asyncio.run(ca.network_overview(_Pool({}), 1))
    assert out["admins_installed"] == 0 and out["posts_7d"] == 0
    assert out["members_total"] == 0


# ── проводка наружу ──────────────────────────────────────────────────────────


def test_api_returns_network():
    api = _read("services/mini_app_api.py")
    assert "network_overview(pool, uid)" in api
    assert '"network": network' in api
    assert 'request.query.get("summary") != "0"' in api
    assert "if include_summary else []" in api


def test_ui_renders_the_network_summary():
    js = _read("mini_app/screens/va_admin.js")
    assert "_vaNetworkHtml(" in js, "сводка сети не рисуется на экране"
    assert "d.network" in js
    assert "Сеть каналов" in js
    assert "Что заходит по сети" in js
    assert "Требуют внимания" in js


def test_network_actions_render_only_in_network_summary():
    """Подсказки сети не должны обращаться к `n` из карточки одного канала."""
    js = _read("mini_app/screens/va_admin.js")
    network_start = js.index("function _vaNetworkHtml")
    network_end = js.index("\nfunction ", network_start + 10)
    control_start = js.index("function _vaControlHtml")
    control_end = js.index("\nfunction ", control_start + 10)
    network = js[network_start:network_end]
    control = js[control_start:control_end]
    assert network.count("n.next_actions") == 3
    assert "openVaChannel" in network and "Что сделать дальше" in network
    assert "n.next_actions" not in control


def test_summary_hidden_for_a_fresh_owner():
    """Пустому владельцу цифры 0/0 не показываем — вход остаётся чистым."""
    js = _read("mini_app/screens/va_admin.js")
    i = js.index("function _vaNetworkHtml")
    body = js[i:i + 300]
    assert "!n.admins_installed" in body and "return ''" in body
