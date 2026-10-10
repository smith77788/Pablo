"""Уровень 8: режим «авто» — темп считает движок, а не пользователь вслепую.

ЧТО БЫЛО. Темп задавался тремя числами (slow/normal/fast), выбранными вслепую:
пользователь не знает, сколько флудов флот словил за последний час, и не
пересматривает выбор по ходу операции. И — главное — выбор был про аккаунт, а
Telegram смотрит на аккаунты как на ГРУППУ: несколько флудов за сутки это сигнал
по всему флоту, и замедляться должен весь флот, а не только пострадавший.

`recommended_daily_limit` уже отвечает за «сколько можно ЭТОМУ аккаунту».
`auto_strategy` отвечает за другой вопрос — «с какой скоростью сегодня вообще
можно работать всем», и потому считает по владельцу, а не по аккаунту.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from services import flood_engine as fe

WORKER = Path(__file__).resolve().parents[1] / "services" / "op_worker.py"


class _Pool:
    def __init__(self, row):
        self.row = row
        self.queries: list[str] = []

    async def fetchrow(self, q, *a):
        self.queries.append(q)
        return self.row


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _strategy(row):
    return _run(fe.auto_strategy(_Pool(row), 100))


# ── решения ──────────────────────────────────────────────────────────────────

def test_clean_busy_day_speeds_up():
    r = _strategy({"ok": 60, "fails": 5, "floods": 0})
    assert r["pace_mult"] < 1.0, "чистый день с объёмом — можно быстрее базового"
    assert "чист" in r["reason"]


def test_floods_slow_the_whole_fleet():
    one = _strategy({"ok": 30, "fails": 5, "floods": 1})
    two = _strategy({"ok": 30, "fails": 5, "floods": 2})
    assert one["pace_mult"] > 1.0, "флуд обязан тормозить, а не только пугать"
    assert two["pace_mult"] > one["pace_mult"], "чем больше флудов, тем сильнее тормоз"
    assert "флудов" in two["reason"]


def test_floods_shrink_the_batch():
    """Оборванный батч стоит целей и времени — на рискованном дне он должен быть
    короче."""
    assert _strategy({"ok": 10, "fails": 2, "floods": 1})["batch_size"] == 3
    assert _strategy({"ok": 10, "fails": 2, "floods": 4})["batch_size"] == 1


def test_pace_multiplier_is_capped():
    r = _strategy({"ok": 0, "fails": 50, "floods": 99})
    assert r["pace_mult"] <= 4.0, "тормоз не должен превращаться в вечную паузу"


def test_low_conversion_slows_down():
    r = _strategy({"ok": 4, "fails": 20, "floods": 0})
    assert r["pace_mult"] > 1.0, "много отказов — гнать объём бессмысленно"
    assert "онверси" in r["reason"]


def test_small_sample_is_not_over_read():
    """Две попытки — не статистика. Ускоряться и тормозить по ним нельзя."""
    assert _strategy({"ok": 2, "fails": 0, "floods": 0})["pace_mult"] == 1.0
    assert _strategy({"ok": 0, "fails": 2, "floods": 0})["pace_mult"] == 1.0


def test_no_data_is_neutral():
    assert _strategy(None)["pace_mult"] == 1.0
    assert _strategy({"ok": 0, "fails": 0, "floods": 0})["pace_mult"] == 1.0


def test_db_failure_is_neutral_not_fast():
    class Boom:
        async def fetchrow(self, q, *a):
            raise RuntimeError("db down")
    r = _run(fe.auto_strategy(Boom(), 100))
    assert r["pace_mult"] == 1.0, "сбой расчёта не должен разгонять флот"


def test_every_decision_is_explained():
    for row in ({"ok": 60, "fails": 5, "floods": 0}, {"ok": 30, "fails": 5, "floods": 2},
                {"ok": 4, "fails": 20, "floods": 0}, None):
        assert _strategy(row)["reason"], (
            "автоматика, которая не объясняет решение, неотличима от произвола"
        )


def test_scope_is_the_fleet_not_the_account():
    r = _Pool({"ok": 1, "fails": 0, "floods": 0})
    _run(fe.auto_strategy(r, 100))
    q = r.queries[0]
    assert "owner_id" in q and "CURRENT_DATE" in q, (
        "решение о темпе — по всему флоту за сегодня; per-account вопрос уже "
        "закрывает recommended_daily_limit"
    )


# ── проводка ─────────────────────────────────────────────────────────────────

def _exec_src() -> str:
    src = WORKER.read_text(encoding="utf-8")
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", src, re.DOTALL)
    assert m
    return m.group(0)


def test_worker_applies_auto_strategy():
    body = _exec_src()
    assert 'if _pace == "auto"' in body, "режим должен реально ветвиться"
    assert "auto_strategy" in body, "движок обязан быть подключён"
    assert "min(batch_size" in body, (
        "авто-батч берётся как более строгий из выбранного и рассчитанного"
    )


def test_auto_failure_falls_back_to_base_not_fast():
    body = _exec_src()
    m = re.search(r'if _pace == "auto":.*?\n\n', body, re.DOTALL)
    assert m, "блок авто-режима не найден"
    assert "_pace_mult, _batch_delay = 1.0, 3.0" in m.group(0), (
        "сбой расчёта → базовый темп; «авто» не имеет права быть опаснее ручного"
    )


def test_auto_reason_reaches_the_user():
    body = _exec_src()
    assert "_auto_reason" in body and "Авто-темп" in body, (
        "пользователь должен видеть, ПОЧЕМУ система выбрала такой темп"
    )


def test_api_accepts_auto():
    api = (Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py").read_text(
        encoding="utf-8")
    assert '"slow", "normal", "fast", "auto"' in api, (
        "иначе бэкенд молча заменит auto на normal и режим не заработает"
    )


def test_ui_offers_auto():
    html = (Path(__file__).resolve().parents[1] / "mini_app" / "index.html").read_text(
        encoding="utf-8")
    m = re.search(r'<select id="massInvitePace".*?</select>', html, re.DOTALL)
    assert m, "селектор темпа не найден"
    assert 'value="auto"' in m.group(0), "режим без пункта в UI недоступен пользователю"
