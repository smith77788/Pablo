"""Новый вектор Strike: снятие ВНЕШНЕЙ инфраструктуры цели (scam/phishing домены).

Массовые in-app репорты — слабый вектор. Зато scam/fraud-каналы гонят трафик на
внешние площадки (платёжки/фишинг/фейк-магазины), у которых есть реально
действующие пути снятия, НЕ зависящие от Telegram: Google Safe Browsing (красный
экран жертве), APWG (блок-листы браузеров, автоотправка письмом), хостинг/
регистратор через RDAP. Даже при живом канале снятие площадки ломает воронку.

Без фикса движок этот слой не трогал вовсе — все векторы били по самому Telegram.
Тесты: чистое извлечение доменов + маршрутизация категорий + kit + оркестратор
(APWG-письма) + интеграция в staggered_strike (в т.ч. при канарейка-стопе).
"""
from __future__ import annotations

import asyncio
import inspect
import time

from services import strike_engine as se


# ── Чистое извлечение внешних доменов ───────────────────────────────────────────
def test_extract_external_domains_drops_telegram_and_noise():
    texts = [
        "Оплата тут https://Evil-Pay.com/checkout и http://www.scam.co/x",
        "зеркало https://EVIL-pay.com/2 наш канал t.me/foo и https://telegram.org/faq",
        "мусор config.py file.json просто текст без ссылок",
    ]
    got = se._extract_external_domains(texts)
    # только внешние домены, в нижнем регистре, без www, без дублей, отсортированы
    assert got == ["evil-pay.com", "scam.co"], got


def test_extract_external_domains_empty_safe():
    assert se._extract_external_domains([]) == []
    assert se._extract_external_domains(["", None]) == []
    # только Telegram-ссылки → пусто (сам Telegram не «внешняя площадка»)
    assert se._extract_external_domains(["t.me/x https://telegra.ph/y"]) == []


# ── Маршрутизация категорий ─────────────────────────────────────────────────────
def test_infra_category_gate():
    for k in ("fraud", "scam", "phishing", "drugs", "weapons", "darknet", "casino"):
        assert se._is_infra_category(k, None) is True, k
    # preset тоже учитывается
    assert se._is_infra_category("other", "scam") is True
    # CSAM/насилие/терроризм — НЕ сюда (их путь NCMEC/IWF/правоохрана, не APWG)
    for k in ("csam", "childabuse", "violence", "terrorism"):
        assert se._is_infra_category(k, None) is False, k


# ── Готовый kit ─────────────────────────────────────────────────────────────────
def test_build_infra_takedown_kit_channels():
    kit = se.build_infra_takedown_kit("BadChan", ["evil-pay.com", "scam.co"], "fraud")
    assert kit["domains"] == ["evil-pay.com", "scam.co"]
    assert len(kit["entries"]) == 2
    ch = kit["entries"][0]["channels"]
    # каналы отсортированы по действенности: Safe Browsing → APWG → хостинг
    assert [c["key"] for c in ch] == ["safebrowsing", "apwg", "hosting"]
    assert any("safebrowsing.google.com" in c["url"] for c in ch)
    assert any("reportphishing@apwg.org" in c["url"] for c in ch)
    assert any("rdap.org/domain/" in c["url"] for c in ch)


def test_apwg_body_has_domain_and_target():
    body = se._build_apwg_report_body("evil-pay.com", "@badchan", "2026-01-01T00:00:00Z")
    assert "evil-pay.com" in body
    assert "t.me/badchan" in body
    assert "APWG" in body or "apwg" in body.lower()


# ── Оркестратор report_scam_infrastructure ──────────────────────────────────────
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakePool:
    """Пул с одним активным SMTP-ящиком."""

    def __init__(self, rows=None):
        self._rows = rows if rows is not None else [{
            "id": 1, "email": "op@mail.com", "smtp_host": "smtp.mail.com",
            "smtp_port": 587, "smtp_pass": "secret", "auth_type": "password",
            "oauth_provider": None, "oauth_refresh_token": None,
            "oauth_access_token": None, "oauth_expires_at": None,
        }]

    async def fetch(self, q, *args):
        return list(self._rows)

    async def execute(self, *a, **k):
        return "OK"


def _patch_email(monkeypatch, sent):
    async def fake_send(host, port, login, secret, frm, to, subject, body,
                        auth_type="password"):
        sent.append({"to": to, "subject": subject})
        return (True, None)

    monkeypatch.setattr(se, "_send_email", fake_send)
    import services.token_vault as tv
    monkeypatch.setattr(tv, "decrypt_token", lambda s: s, raising=False)


def test_report_scam_infrastructure_no_pool_returns_kit_only():
    res = _run(se.report_scam_infrastructure(
        None, 0, "@x", ["evil.com"], "fraud", None))
    assert res["apwg_sent"] == 0
    assert res["kit"]["domains"] == ["evil.com"]        # kit есть даже без pool
    assert "skip_reason" in res


def test_report_scam_infrastructure_non_infra_skips():
    res = _run(se.report_scam_infrastructure(
        _FakePool(), 42, "@x", ["evil.com"], "csam", None))
    assert res["apwg_sent"] == 0
    assert "infra" in res["skip_reason"]


def test_report_scam_infrastructure_sends_apwg_per_domain(monkeypatch):
    sent: list = []
    _patch_email(monkeypatch, sent)
    res = _run(se.report_scam_infrastructure(
        _FakePool(), 42, "@badchan", ["evil-pay.com", "scam.co"], "fraud", None))
    assert res["apwg_sent"] == 2
    assert all(m["to"] == "reportphishing@apwg.org" for m in sent)
    assert len(sent) == 2


def test_report_scam_infrastructure_no_smtp_returns_kit(monkeypatch):
    res = _run(se.report_scam_infrastructure(
        _FakePool(rows=[]), 42, "@x", ["evil.com"], "fraud", None))
    assert res["apwg_sent"] == 0
    assert res["kit"]["entries"]                        # kit всё равно готов
    assert "kit" in res["skip_reason"] or "ящик" in res["skip_reason"]


# ── Вектор: жалоба регистратору через RDAP ──────────────────────────────────────
def test_parse_rdap_abuse_email_nested():
    rdap = {"entities": [{
        "roles": ["registrar"],
        "entities": [{
            "roles": ["abuse"],
            "vcardArray": ["vcard", [
                ["version", {}, "text", "4.0"],
                ["fn", {}, "text", "Abuse Dept"],
                ["email", {}, "text", "ABUSE@Registrar.com"],
            ]],
        }],
    }]}
    assert se._parse_rdap_abuse_email(rdap) == "abuse@registrar.com"


def test_parse_rdap_abuse_email_absent():
    assert se._parse_rdap_abuse_email({}) is None
    assert se._parse_rdap_abuse_email({"entities": [{"roles": ["registrar"]}]}) is None
    assert se._parse_rdap_abuse_email("junk") is None


def test_rdap_registrar_abuse_fail_open_on_none():
    async def fetch_none(url):
        return None
    assert _run(se._rdap_registrar_abuse("evil.com", _fetch=fetch_none)) is None

    async def fetch_boom(url):
        raise RuntimeError("network down")
    assert _run(se._rdap_registrar_abuse("evil.com", _fetch=fetch_boom)) is None


def test_registrar_body_has_domain_and_target():
    body = se._build_registrar_abuse_body("evil.com", "@badchan", "2026-01-01T00:00:00Z")
    assert "evil.com" in body and "t.me/badchan" in body
    assert "ICANN" in body or "Acceptable Use" in body


def test_report_domain_registrar_sends_per_resolved_domain(monkeypatch):
    sent: list = []
    _patch_email(monkeypatch, sent)

    async def fake_fetch(url):
        # RDAP-контакт есть только для одного домена — второй пропускаем (fail-open)
        if "evil-pay.com" in url:
            return {"entities": [{"roles": ["abuse"], "vcardArray": [
                "vcard", [["email", {}, "text", "abuse@reg.com"]]]}]}
        return None

    res = _run(se.report_domain_registrar(
        _FakePool(), 42, "@badchan", ["evil-pay.com", "no-rdap.co"],
        "fraud", None, _fetch=fake_fetch))
    assert res["registrar_sent"] == 1
    assert len(sent) == 1 and sent[0]["to"] == "abuse@reg.com"
    contacts = {c["domain"]: c for c in res["contacts"]}
    assert contacts["evil-pay.com"]["ok"] is True
    assert contacts["no-rdap.co"]["abuse_email"] is None


def test_report_domain_registrar_non_infra_and_no_pool_skip():
    r1 = _run(se.report_domain_registrar(_FakePool(), 42, "@x", ["e.com"], "csam", None))
    assert r1["registrar_sent"] == 0 and "infra" in r1["skip_reason"]
    r2 = _run(se.report_domain_registrar(None, 0, "@x", ["e.com"], "fraud", None))
    assert r2["registrar_sent"] == 0


# ── Интеграция в staggered_strike ───────────────────────────────────────────────
def _accs(n):
    return [{"id": i, "session_str": "s" * 20, "trust_score": 1.0}
            for i in range(1, n + 1)]


def _plan(accs, intel=None, reason="fraud"):
    return se.StrikePlan(
        targets=["@victim"], accounts=list(accs), reason=reason, preset=None,
        label="t", intel=intel or {}, waves=se.plan_waves(accs, 3),
        started_at=time.time(), phase="recon", mode="normal", owner_id=42)


def _install_mocks(monkeypatch, per_account_result, recon_domains=None):
    calls = {"recon": 0, "infra": 0}

    async def fake_strike(acc, peer, intel, reason, preset, texts, idx, wave, sem,
                          mode="normal", pool=None):
        return dict(per_account_result)

    async def fake_network(accounts, intel, reason, preset):
        return {"nodes_attacked": 0, "total_reports": 0}

    async def fake_spambot(acc, target):
        return {"status": "sent", "bots": {}}

    async def fake_abuse(target, cat, title="", members=0):
        return {"ok": True, "submitted": 1, "total": 1}

    async def fake_verify(acc, target, max_attempts=3, delay_range=(25, 50)):
        return None

    async def fake_claim(ids):
        return list(ids)

    async def fake_release(ids):
        return None

    async def fake_map(session_str, target, _acc=None):
        calls["recon"] += 1
        return {"external_domains": list(recon_domains or [])}

    async def fake_rdap(domain, _fetch=None):
        calls["rdap"] = calls.get("rdap", 0) + 1
        return f"abuse@registrar-of-{domain}"

    monkeypatch.setattr(se, "_rdap_registrar_abuse", fake_rdap)
    monkeypatch.setattr(se, "_one_account_strike", fake_strike)
    monkeypatch.setattr(se, "strike_network_nodes_v2", fake_network)
    monkeypatch.setattr(se, "_escalate_to_spambot", fake_spambot)
    monkeypatch.setattr(se, "submit_abuse_form", fake_abuse)
    monkeypatch.setattr(se, "verify_target_takedown", fake_verify)
    monkeypatch.setattr(se.random, "uniform", lambda a, b: 0)
    import services.op_worker as opw
    monkeypatch.setattr(opw, "try_claim_accounts", fake_claim)
    monkeypatch.setattr(opw, "release_accounts", fake_release)
    import services.account_manager as am
    monkeypatch.setattr(am, "strike_map_target", fake_map)
    return calls


def test_staggered_runs_infra_from_intel_domains(monkeypatch):
    # домены уже есть в разведке → APWG уходит, лишней карты цели НЕ делаем
    sent: list = []
    _patch_email(monkeypatch, sent)
    calls = _install_mocks(monkeypatch, {"peer_reported": True})
    plan = _plan(_accs(6),
                 intel={"@victim": {"external_domains": ["evil-pay.com", "scam.co"]}})
    results = _run(se.staggered_strike(plan, pool=_FakePool()))
    r = results[0]
    assert r.infra_domains == ["evil-pay.com", "scam.co"]
    assert r.infra_apwg_sent == 2
    assert calls["recon"] == 0            # домены были — карта цели не нужна
    assert any(m["to"] == "reportphishing@apwg.org" for m in sent)
    # второй удар: жалоба регистратору каждого домена (abuse из RDAP)
    assert r.infra_registrar_sent == 2
    assert any(m["to"].startswith("abuse@registrar-of-") for m in sent)


def test_staggered_recons_domains_when_intel_empty(monkeypatch):
    # intel пуст → одна карта цели добывает внешние домены → APWG
    sent: list = []
    _patch_email(monkeypatch, sent)
    calls = _install_mocks(monkeypatch, {"peer_reported": True},
                           recon_domains=["fraud-site.net"])
    plan = _plan(_accs(6), intel={})
    results = _run(se.staggered_strike(plan, pool=_FakePool()))
    r = results[0]
    assert calls["recon"] == 1
    assert r.infra_domains == ["fraud-site.net"]
    assert r.infra_apwg_sent == 1


def test_staggered_canary_abort_keeps_infra_no_recon(monkeypatch):
    # флот под флудом (канарейка-стоп): аккаунтные волны стоят, но APWG по уже
    # известным доменам уходит (письмо флот не жжёт), а лишнюю карту НЕ делаем.
    sent: list = []
    _patch_email(monkeypatch, sent)
    calls = _install_mocks(monkeypatch,
                           {"peer_reported": False, "_peer_flood": True,
                            "error": "PEER_FLOOD"})
    plan = _plan(_accs(6),
                 intel={"@victim": {"external_domains": ["evil-pay.com"]}})
    results = _run(se.staggered_strike(plan, pool=_FakePool()))
    r = results[0]
    assert r.canary_aborted is True
    assert calls["recon"] == 0            # флот в опасности — карту цели не жжём
    assert r.infra_apwg_sent == 1         # но домен из intel добит по APWG
    assert r.infra_registrar_sent == 1    # и жалоба регистратору (флот не жжёт)


def test_staggered_non_infra_skips_infra(monkeypatch):
    # некатегорийный reason (violence) → инфра-вектор не запускается
    sent: list = []
    _patch_email(monkeypatch, sent)
    calls = _install_mocks(monkeypatch, {"peer_reported": True},
                           recon_domains=["should-not-be-used.com"])
    plan = _plan(_accs(6), intel={}, reason="violence")
    results = _run(se.staggered_strike(plan, pool=_FakePool()))
    r = results[0]
    assert calls["recon"] == 0
    assert r.infra_domains == []
    assert r.infra_apwg_sent == 0


# ── Проводка/источники ──────────────────────────────────────────────────────────
def test_summary_surfaces_infra_line():
    r = se.StrikeResult(target="@x", infra_domains=["evil-pay.com", "scam.co"],
                        infra_apwg_sent=2, infra_registrar_sent=1)
    out = se.format_strike_summary([r])
    assert "Внешняя инфраструктура" in out
    assert "APWG отправлено" in out
    assert "регистратору" in out


def test_recon_extracts_external_domains_wired():
    src = inspect.getsource(
        __import__("services.account_manager", fromlist=["strike_map_target"])
        .strike_map_target)
    assert "external_domains" in src
    assert "_extract_external_domains" in src


def test_staggered_calls_infra_orchestrator():
    src = inspect.getsource(se.staggered_strike)
    assert "report_scam_infrastructure" in src
    assert "report_domain_registrar" in src
    assert "_is_infra_category" in src
