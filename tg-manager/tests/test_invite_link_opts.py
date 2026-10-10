"""Регресс: нормализация параметров инвайт-ссылки своего канала.

Инвайт-ссылки — легитимный рост своего сообщества (люди вступают сами).
_normalize_invite_opts — чистая логика опций (без сети): срок, лимит переходов,
подпись, и взаимоисключение «лимит ↔ вступление по заявке» (ограничение Telegram).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from services.account_manager import _normalize_invite_opts

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_defaults_all_none():
    o = _normalize_invite_opts(_now=_NOW)
    assert o == {"title": None, "expire_dt": None, "usage_limit": None,
                 "request_needed": False}


def test_expire_seconds_becomes_absolute_utc_date():
    o = _normalize_invite_opts(expire_seconds=3600, _now=_NOW)
    assert o["expire_dt"] == _NOW + timedelta(seconds=3600)


def test_nonpositive_values_treated_as_unset():
    o = _normalize_invite_opts(expire_seconds=0, usage_limit=-5, _now=_NOW)
    assert o["expire_dt"] is None
    assert o["usage_limit"] is None


def test_usage_limit_kept_when_no_request():
    o = _normalize_invite_opts(usage_limit=100, _now=_NOW)
    assert o["usage_limit"] == 100
    assert o["request_needed"] is False


def test_request_needed_disables_usage_limit():
    # Telegram запрещает числовой лимит вместе с вступлением по заявке.
    o = _normalize_invite_opts(usage_limit=100, request_needed=True, _now=_NOW)
    assert o["request_needed"] is True
    assert o["usage_limit"] is None


def test_title_trimmed_to_32_and_empty_to_none():
    assert _normalize_invite_opts(title="   ", _now=_NOW)["title"] is None
    long = "x" * 50
    assert _normalize_invite_opts(title=long, _now=_NOW)["title"] == "x" * 32
