"""Исходящие вебхуки: подпись, повторы, каталог событий, SSRF.

Живая база (триггеры, раскладка, доставка) — в
tests/test_integrations_postgres.py; здесь — то, что проверяется без неё.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

from services import webhooks as wh

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_signature_round_trip_and_tamper():
    body = b'{"event":"operation.done"}'
    hdr = wh.sign("whsec_x", 1_700_000_000, body)
    assert hdr.startswith("t=1700000000,v1=")
    assert wh.verify("whsec_x", hdr, body, now=1_700_000_010)
    assert not wh.verify("whsec_x", hdr, body + b" ", now=1_700_000_010), "тело подменено"
    assert not wh.verify("другой", hdr, body, now=1_700_000_010), "чужой секрет"
    assert not wh.verify("whsec_x", hdr, body, now=1_700_000_000 + 3600), "старый запрос переигран"
    assert not wh.verify("whsec_x", "мусор", body)


def test_signature_covers_timestamp():
    """Метка времени внутри подписи: подставить свежую к старому телу нельзя."""
    body = b"{}"
    old = wh.sign("s", 100, body)
    forged = old.replace("t=100", "t=200")
    assert not wh.verify("s", forged, body, now=200)


def test_backoff_grows_and_is_capped():
    seq = [wh.backoff_seconds(n) for n in range(1, wh.MAX_ATTEMPTS + 3)]
    assert seq == sorted(seq), seq
    assert seq[0] == 60
    assert max(seq) == 12 * 3600


def test_normalize_events_keeps_known_only():
    assert wh.normalize_events(["operation.done", "нет такого", "OPERATION.DONE"]) == ["operation.done"]
    assert wh.normalize_events("account.lost, account.added") == ["account.lost", "account.added"]
    assert wh.normalize_events(["webhook.test"]) == [], "тест — не подписка"
    assert wh.normalize_events(None) == []


def test_every_catalog_event_has_a_source_in_the_migration():
    """Событие в каталоге без триггера — галочка, по которой не придёт ничего."""
    sql = open(os.path.join(ROOT, "schema_v262_integrations.sql"), encoding="utf-8").read()
    op_statuses = set(re.search(
        r"NEW\.status IN \(([^)]*)\)", sql).group(1).replace("'", "").replace(" ", "").split(","))
    for ev in wh.EVENTS:
        if ev == "webhook.test":
            continue
        if ev.startswith("operation."):
            assert ev.split(".", 1)[1] in op_statuses, ev
            assert "'operation.' || NEW.status" in sql
        else:
            assert f"'{ev}'" in sql, f"у события {ev} нет источника в миграции"


def test_operation_events_cover_every_terminal_status():
    from services import op_status
    for st in op_status.TERMINAL:
        assert f"operation.{st}" in wh.EVENTS, st


def test_body_shape_is_stable():
    body = json.loads(wh.build_body("account.added", 7, "2026-11-01T00:00:00+00:00",
                                    {"account_id": 1}))
    assert body == {"id": 7, "event": "account.added",
                    "created_at": "2026-11-01T00:00:00+00:00", "data": {"account_id": 1}}


def test_summary_is_human_and_russian():
    s = wh.summarize("operation.partial", {"operation_id": 5, "op_type": "broadcast",
                                           "done_items": 9, "total_items": 10})
    assert "#5" in s and "9/10" in s and "частично" in s
    s = wh.summarize("account.lost", {"phone": "+7999", "old_status": "ok",
                                      "new_status": "banned"})
    assert "+7999" in s and "banned" in s
    assert wh.summarize("неизвестное", {}) == "неизвестное"


@pytest.mark.parametrize("url", [
    "http://example.com/hook",          # не https
    "https://127.0.0.1/hook",
    "https://localhost/hook",
    "https://[::ffff:127.0.0.1]/hook",
    "https://10.0.0.5/hook",
    "https://169.254.169.254/latest",   # metadata
    "",
])
def test_internal_or_plain_http_targets_are_refused(url):
    with pytest.raises(ValueError):
        asyncio.new_event_loop().run_until_complete(wh.validate_http_url(url))


def test_delivery_rechecks_ssrf_before_every_send():
    """Адрес проверен при сохранении, но DNS мог смениться: перед отправкой —
    снова. Иначе сохранённый вчера публичный адрес сегодня ведёт внутрь."""
    class _Http:
        def post(self, *a, **k):
            raise AssertionError("запрос ушёл на внутренний адрес")

    ok, code, err = asyncio.new_event_loop().run_until_complete(wh.send_http(
        _Http(), {"url": "https://127.0.0.1/x", "secret_enc": ""}, "webhook.test",
        0, None, {}))
    assert not ok and code is None and "внутреннюю" in err


def test_delivery_does_not_follow_redirects():
    src = open(os.path.join(ROOT, "services", "webhooks.py"), encoding="utf-8").read()
    assert "allow_redirects=False" in src


def test_secret_is_returned_under_a_name_the_scrubber_keeps():
    """`secret` вычищает последний барьер ответа — секрет не дошёл бы до экрана."""
    from services.secret_masking import scrub_payload
    assert scrub_payload({"signing_secret": "whsec_abc"})["signing_secret"] == "whsec_abc"
    src = open(os.path.join(ROOT, "services", "mini_app_integrations.py"), encoding="utf-8").read()
    assert '"signing_secret": secret' in src
