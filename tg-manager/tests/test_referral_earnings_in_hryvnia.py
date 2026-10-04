"""Заработок рефералов — в гривне, но пересчётом, а не подменой значка.

Владелец считает заработок в гривне (указание 04.10.2026). Комиссия при этом
приходит в долларах: платежи идут в TON и USDT, в базе лежат commission_usd и
amount_usd, и бот начисляет «$X». Если просто заменить $ на ₴, сумма вырастет в
десятки раз на бумаге — экран будет врать о реальных деньгах.

Поэтому гривня считается по курсу, который владелец задаёт сам, а рядом всегда
видно курс и исходную сумму в долларах.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def _fn(name: str, js: str = HTML) -> str:
    m = re.search(r"(?:async )?function %s\s*\([^)]*\)\s*\{" % re.escape(name), js)
    assert m, f"нет функции {name}"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[i:j + 1]
    raise AssertionError(name)


def _no_comments(js: str) -> str:
    return "\n".join(ln for ln in js.splitlines() if not ln.strip().startswith("//"))


def _detail() -> str:
    body = API[API.index("async def referral_overview_detail"):]
    end = body.index("app.router.add_get") if "app.router.add_get" in body else len(body)
    return body[:end]


# ── Источник правды остался долларовым ───────────────────────────────────────

def test_stored_currency_is_still_dollars():
    """Значок на экране сменился, колонки в базе — нет. Если это изменится,
    пересчёт на экране станет двойным, и тест обязан об этом сказать."""
    db = open(os.path.join(ROOT, "database", "db.py"), encoding="utf-8").read()
    assert "commission_usd" in db, "комиссия больше не хранится в долларах — проверьте пересчёт в UI"


# ── Сервер отдаёт гривню и курс ──────────────────────────────────────────────

def test_detail_returns_hryvnia_with_rate_and_dollars():
    d = _detail()
    assert '"currency": "UAH"' in d
    assert '"uah_rate": uah_rate' in d
    assert '"total_earned_uah": round(earned_usd * uah_rate, 2)' in d
    assert '"total_earned": earned_usd' in d, "исходная сумма в долларах должна остаться"


def test_rate_is_per_owner_and_bounded():
    assert "async def _uah_rate" in API
    body = API[API.index("async def _uah_rate"):API.index("async def uah_rate_save")]
    assert "spine.state_get(pool, uid" in body, "курс должен быть свой у каждого владельца"
    assert "1.0 <= r <= 10000.0" in body
    assert "_UAH_RATE_DEFAULT" in body, "без курса экран всё равно должен открыться"


def test_rate_can_be_saved_and_is_validated():
    assert "async def uah_rate_save" in API
    assert '"/api/miniapp/settings/uah_rate"' in API
    body = API[API.index("async def uah_rate_save"):]
    body = body[:body.index("async def referral_overview_detail")]
    assert "1.0 <= rate <= 10000.0" in body
    assert "spine.state_set(pool, uid" in body
    assert 'replace(",", ".")' in body, "владелец вводит курс с запятой"


# ── Экран ────────────────────────────────────────────────────────────────────

def test_screen_shows_hryvnia_as_the_headline():
    body = _no_comments(_fn("openReferral"))
    assert "total_earned_uah" in body
    assert "₴" in body
    # Голого доллара в заголовке больше нет.
    assert "$${Number(d.total_earned" not in body


def test_screen_shows_the_rate_and_the_dollar_amount():
    body = _no_comments(_fn("openReferral"))
    assert "Курс пересчёта" in body
    assert "₴/$" in body
    assert "В долларах" in body
    assert "d.total_earned||0" in body


def test_rate_is_editable_from_the_screen():
    body = _no_comments(_fn("refEditRate"))
    assert "askPrompt(" in body
    assert "/api/miniapp/settings/uah_rate" in body
    assert "rate >= 1 && rate <= 10000" in body
    assert "openReferral()" in body, "после смены курса экран должен пересчитаться"
    # Заголовок «Заработано» тоже ведёт к смене курса.
    assert "refEditRate()" in _no_comments(_fn("openReferral"))


def test_money_is_formatted_in_russian():
    body = _no_comments(_fn("_money"))
    assert "replace('.', ',')" in body
    assert "(\\d{3})" in body or "(\\\\d{3})" in body or "\\d{3}" in body
