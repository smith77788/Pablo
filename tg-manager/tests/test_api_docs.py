"""Документация API не отстаёт от кода.

Новая ручка /api/v1 без описания, событие вебхука без примера или пример с
ключами, которых источник не кладёт, — падение здесь, а не клиент, который
строит интеграцию по устаревшей странице.
"""
from __future__ import annotations

import json
import os
import re

from services import api_docs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _registered_v1_routes() -> set[tuple[str, str]]:
    src = open(os.path.join(ROOT, "services", "rest_api.py"), encoding="utf-8").read()
    return {(m.lower(), p) for m, p in
            re.findall(r'add_(get|post|delete|put|patch)\("(/api/v1/[^"]*)"', src)}


def _documented() -> set[tuple[str, str]]:
    return {(m, p) for p, ms in api_docs.paths().items() for m in ms}


def test_every_route_is_documented_and_nothing_extra():
    reg, doc = _registered_v1_routes(), _documented()
    assert reg, "маршруты /api/v1 не нашлись — поменялся способ регистрации?"
    assert reg - doc == set(), f"ручки без документации: {sorted(reg - doc)}"
    assert doc - reg == set(), f"описаны несуществующие ручки: {sorted(doc - reg)}"


def test_protected_routes_declare_scope_and_limit():
    for path, ms in api_docs.paths().items():
        for m, op in ms.items():
            if op.get("security") == []:
                continue
            assert op.get("x-scope") in ("read", "write"), (m, path)
            assert op.get("x-rate-limit-per-minute"), (m, path)
            for code in ("401", "403", "429"):
                assert code in op["responses"], (m, path, code)


def test_documented_scopes_and_limits_match_the_handlers():
    """Область и лимит в описании — ровно те, что стоят в _gate(...) ручки."""
    src = open(os.path.join(ROOT, "services", "rest_api.py"), encoding="utf-8").read()
    handler_of = dict(re.findall(r'add_\w+\("(/api/v1/[^"]*)", (\w+)\)', src))
    for path, ms in api_docs.paths().items():
        for m, op in ms.items():
            if op.get("security") == []:
                continue
            fn = handler_of[path]
            body = src.split(f"async def {fn}(", 1)[1].split("\n    async def ", 1)[0]
            gate = re.search(r'_gate\(request, "(\w+)", (\d+)\)', body)
            assert gate, fn
            assert (op["x-scope"], op["x-rate-limit-per-minute"]) == (gate.group(1), int(gate.group(2))), fn


def test_every_event_has_an_example_and_a_webhook_entry():
    from services import webhooks as wh
    spec = api_docs.build_spec("https://x.test")
    for ev in wh.EVENTS:
        assert ev in api_docs.EVENT_EXAMPLES, f"нет примера для {ev}"
        assert ev in spec["webhooks"], ev
    assert set(api_docs.EVENT_EXAMPLES) <= set(wh.EVENTS) | set(wh.AUTOMATION_NOT_FORWARDED), \
        "пример для события, которого больше нет"


def test_trigger_event_examples_use_the_keys_the_trigger_writes():
    for fname, events in (("schema_v273_crm_connectors.sql", ("pipeline.updated", "deal.updated")),
                          ("schema_v262_integrations.sql", ("account.added",))):
        sql = open(os.path.join(ROOT, fname), encoding="utf-8").read()
        for ev in events:
            for key in api_docs.EVENT_EXAMPLES[ev]:
                assert f"'{key}'" in sql, f"{ev}: ключа {key} триггер не пишет"


def test_spec_is_openapi_31_and_serializable():
    spec = api_docs.build_spec("https://x.test/")
    assert spec["openapi"].startswith("3.1")
    assert spec["servers"] == [{"url": "https://x.test"}]
    json.dumps(spec, ensure_ascii=False)
    # Все $ref ведут в существующие компоненты.
    text = json.dumps(spec)
    for ref in set(re.findall(r'"\$ref": "#/components/(\w+)/(\w+)"', text)):
        assert ref[1] in spec["components"][ref[0]], ref


def test_page_covers_every_path_event_and_signature_recipe():
    from services import webhooks as wh
    page = api_docs.render_html(api_docs.build_spec("https://x.test"))
    assert page.startswith("<!doctype html>")
    for path in api_docs.paths():
        assert path in page, path
    for ev in wh.EVENTS:
        assert f"id='ev-{ev}'" in page, ev
    for needle in ("X-Infragram-Signature", "hmac", "createHmac", "hash_hmac", "X-Api-Key"):
        assert needle in page, needle
    assert "<script" not in page, "страница без скриптов: открывается и за прокси без CDN"


def test_signature_recipe_matches_the_real_signer():
    """Рецепт на странице считает ту же подпись, что и отправитель."""
    import hashlib
    import hmac
    from services import webhooks as wh
    body = b'{"id":1}'
    header = wh.sign("whsec_x", 1700000000, body)
    parts = dict(p.split("=", 1) for p in header.split(","))
    want = hmac.new(b"whsec_x", f"{parts['t']}.".encode() + body, hashlib.sha256).hexdigest()
    assert parts["v1"] == want
    assert 'f"{ts}.".encode() + body' in api_docs._VERIFY_PY


def test_docs_routes_are_public_and_registered():
    src = open(os.path.join(ROOT, "services", "rest_api.py"), encoding="utf-8").read()
    assert 'add_get("/api/v1/openapi.json", api_openapi)' in src
    assert 'add_get("/api/docs", api_docs_page)' in src
    for fn in ("api_openapi", "api_docs_page"):
        body = src.split(f"async def {fn}(", 1)[1].split("\n    async def ", 1)[0]
        assert "_gate(" not in body, "документация открывается без ключа"
