"""Мёртвый прокси должен быть виден в списке аккаунтов.

Разрыв: список аккаунтов вообще не знал про прокси. Аккаунт, чей прокси не
отвечает, показывался «Активен» с зелёным доверием — при том, что каждая его
операция падает по сети. Ровно ту же ловушку код уже признаёт для риск-пульса
(«пользователь видит „Активен“, а действия по нему не идут»), но самая частая
причина простоя — мёртвый прокси — оставалась невидимой, и человек шёл чинить
аккаунт вместо замены прокси.
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _accounts_query() -> str:
    start = _API.index("async def accounts(request")
    body = _API[start:start + 4000]
    i = body.index("SELECT id, phone")
    return body[i:body.index("ORDER BY is_active DESC")]


def test_account_rows_carry_proxy_state():
    q = _accounts_query()
    assert "proxy_alive" in q and "has_proxy" in q


def test_proxy_state_uses_scalar_subquery_not_join():
    """У tg_accounts и user_proxies совпадают id/owner_id/is_active: после JOIN
    условия из _accounts_where стали бы неоднозначными, запрос упал бы, а список
    аккаунтов молча опустел — тот же класс ошибки, что обнулял список каналов."""
    q = _accounts_query()
    assert "JOIN user_proxies" not in q
    assert "SELECT p.is_alive FROM user_proxies p" in q


def test_subquery_is_bound_to_the_account_row():
    q = _accounts_query()
    assert "p.id = tg_accounts.proxy_id" in q


def test_proxy_label_prefers_name_then_geo():
    q = _accounts_query()
    assert "COALESCE(p.label, p.geo_country)" in q


def test_credentials_are_not_selected():
    """proxy_url содержит логин и пароль — в список аккаунтов он не нужен."""
    q = _accounts_query()
    assert "proxy_url" not in q


# ── Интерфейс ─────────────────────────────────────────────────────────────────

def _render() -> str:
    start = _UI.index("function renderAccounts")
    return _UI[start:start + 3000]


def test_ui_shows_dead_proxy_instead_of_active():
    r = _render()
    assert "a.proxy_alive === false" in r
    assert "🔌" in r


def test_ui_explains_that_the_account_is_fine():
    """Иначе человек снова пойдёт чинить аккаунт вместо прокси."""
    assert "аккаунт цел, замените прокси" in _UI


def test_accounts_without_proxy_are_not_flagged():
    """Работа с адреса хоста — законный режим, а не поломка."""
    r = _render()
    assert "a.has_proxy && a.proxy_alive === false" in r


def test_strict_false_comparison_not_falsy():
    """proxy_alive = NULL (никогда не проверяли) — это не «мёртв»."""
    r = _render()
    assert "=== false" in r
    assert not re.search(r"!a\.proxy_alive\b", r), "нестрогая проверка пометит непроверенный прокси мёртвым"


def test_dead_proxy_outranks_plain_active_but_not_a_ban():
    """Порядок веток: бан и кулдаун важнее сообщения про прокси."""
    r = _render()
    assert r.index("'🚫 Бан'") < r.index("a.proxy_alive === false")
    assert r.index("a.proxy_alive === false") < r.index("status='Активен'")


# ── Срез «простаивают из-за прокси» ───────────────────────────────────────────

def test_filter_exists_and_is_strict():
    """Видеть мало — нужен срез, чтобы починить оптом. Строго IS FALSE:
    непроверенный прокси (NULL) не должен попадать в «мёртвые»."""
    start = _API.index("def _accounts_where")
    body = _API[start:start + 3000]
    assert 'flt == "proxy_down"' in body
    assert "p.is_alive IS FALSE" in body
    assert "proxy_id IS NOT NULL" in body


def test_filter_is_correlated_to_the_account_row():
    start = _API.index("def _accounts_where")
    body = _API[start:start + 3000]
    assert "p.id = tg_accounts.proxy_id" in body


def test_stats_expose_the_counter():
    assert "AS proxy_down" in _API
    assert '"proxy_down"' in _API


def test_stats_key_lookup_survives_admin_query_without_the_column():
    """Межтенантный (админский) запрос этот счётчик не считает — обращение к
    отсутствующему ключу не должно ронять экран."""
    assert "k in st.keys()" in _API


def test_ui_has_the_card_and_fills_it():
    assert "filterAcc('proxy_down'" in _UI
    assert 'id="kpi-proxydown"' in _UI
    assert "el('kpi-proxydown'" in _UI


def test_card_is_hidden_when_nothing_is_wrong():
    """Пустая карточка только занимает место в плотной строке KPI."""
    assert 'querySelector(\'[data-filter="proxy_down"]\')' in _UI
    assert "pdown > 0" in _UI


def test_client_side_fallback_is_also_strict():
    seg = _UI[_UI.index("function updateAccKpi"):]
    seg = seg[:1800]
    assert "a.proxy_alive === false" in seg
