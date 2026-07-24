"""Auto-reply match_mode (класс #3: параметр принят, но не персистился).

UI шлёт match_mode (contains/exact/starts), но create_auto_reply не читал и не
писал его в INSERT → выбор exact/starts молча игнорировался (правило всегда
работало как contains). _match_rule сам режимы поддерживает — терялось при
создании. Тесты фиксируют и семантику режимов, и персист в endpoint.
"""
from __future__ import annotations

import inspect

from services.auto_responder import _match_rule


def _rule(keyword, mode):
    return {"trigger_type": "keyword", "keyword": keyword, "match_mode": mode}


def test_exact_mode():
    r = _rule("hello", "exact")
    assert _match_rule(r, "hello") is True
    assert _match_rule(r, "hello world") is False
    assert _match_rule(r, "say hello") is False


def test_starts_mode():
    r = _rule("hello", "starts")
    assert _match_rule(r, "hello world") is True
    assert _match_rule(r, "say hello") is False


def test_contains_mode_default():
    r = _rule("hello", "contains")
    assert _match_rule(r, "well hello there") is True
    # дефолт (mode отсутствует) = contains
    assert _match_rule({"trigger_type": "keyword", "keyword": "hi"}, "oh hi mark") is True


def test_multi_keyword_comma():
    r = _rule("promo,discount,sale", "contains")
    assert _match_rule(r, "any sale today") is True
    assert _match_rule(r, "goodbye world") is False
    # exact по нескольким
    assert _match_rule(_rule("promo,discount", "exact"), "discount") is True
    assert _match_rule(_rule("promo,discount", "exact"), "discount!") is False


def test_create_endpoint_persists_match_mode():
    """create_auto_reply обязан читать и писать match_mode в INSERT."""
    import pathlib

    api = pathlib.Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"
    src = api.read_text(encoding="utf-8")
    start = src.index("async def create_auto_reply")
    body = src[start:start + 3000]
    assert 'body.get("match_mode")' in body, "match_mode не читается из тела"
    insert = body.index("INSERT INTO auto_replies")
    assert "match_mode" in body[insert:insert + 300], \
        "INSERT не содержит match_mode — параметр не персистится"
