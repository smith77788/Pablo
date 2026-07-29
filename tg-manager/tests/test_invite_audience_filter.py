"""Регресс (PRODUCT_DEPTH_PLAN P1): фильтры аудитории в инвайте.

parsed_audiences хранит is_premium/is_bot/username/is_active/phone, но инвайт
собирал аудиторию БЕЗ фильтров → в приглашение шли боты/удалённые/приватные без
username. Вынесен единый `services/audience_filters.parsed_audience_filters`
(тот же, что в парсер-вью), применён в `_exec_mass_invite`, композер шлёт
`aud_filters`.
"""
from __future__ import annotations

import inspect
import os
import re

from services import mini_app_api, op_worker
from services.audience_filters import parsed_audience_filters

_HTML = os.path.join(os.path.dirname(__file__), "..", "mini_app", "index.html")


def test_filter_sql_builder():
    # булевы фильтры → условия без доп. параметров
    sql, params = parsed_audience_filters(
        {"with_username": "1", "not_bot": "1", "premium": "1", "active": "1", "with_phone": "1"},
        base_params_count=2,
    )
    assert params == []
    assert "username IS NOT NULL" in sql and "COALESCE(is_bot,FALSE)=FALSE" in sql
    assert "is_premium=TRUE" in sql and "is_active=TRUE" in sql and "phone IS NOT NULL" in sql
    assert sql.startswith(" AND ")
    # ничего не выбрано → пусто
    assert parsed_audience_filters({}, 1) == ("", [])
    # source добавляет ровно один параметр с корректным индексом
    sql2, p2 = parsed_audience_filters({"source": "news"}, base_params_count=2)
    assert p2 == ["%news%"] and "$3" in sql2


def test_mini_app_reuses_shared_helper_not_local_dup():
    src = inspect.getsource(mini_app_api)
    # локального определения быть НЕ должно (единый источник — audience_filters)
    assert "def parsed_audience_filters(" not in src, "должна быть ОДНА реализация (в audience_filters)"
    assert "from services.audience_filters import parsed_audience_filters" in src


def test_invite_submit_passes_aud_filters():
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def mass_inviter_submit\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "mass_inviter_submit not found"
    body = m.group(1)
    assert 'body.get("aud_filters")' in body, "submit должен читать aud_filters из тела"
    assert '"aud_filters"' in body and "with_username" in body and "not_bot" in body


def test_exec_mass_invite_applies_filters():
    src = inspect.getsource(op_worker)
    m = re.search(r"async def _exec_mass_invite\(.*?FROM parsed_audiences.*?FROM parsed_audiences", src, re.DOTALL)
    assert m, "invite parsed_audiences assembly not found"
    body = m.group(0)
    assert "parsed_audience_filters" in body and 'params.get("aud_filters")' in body, (
        "_exec_mass_invite должен применять фильтры к выборке parsed_audiences"
    )
    # применено к ОБОИМ путям (parse_run_id и вся аудитория)
    assert body.count("parsed_audience_filters(") >= 2


def test_ui_invite_filters_wired():
    html = open(_HTML, encoding="utf-8").read()
    for cid in ("invFltUsername", "invFltNotBot", "invFltPremium", "invFltActive"):
        assert f'id="{cid}"' in html, f"нет чекбокса фильтра {cid}"
    m = re.search(r"async function submitMassInvite\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m and "aud_filters" in m.group(1) and "with_username" in m.group(1), (
        "submitMassInvite должен собирать aud_filters из чекбоксов"
    )
