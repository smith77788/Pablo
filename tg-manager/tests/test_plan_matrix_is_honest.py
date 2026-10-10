"""Экран тарифов обещал не то, что тариф даёт.

Состав тарифов в мини-аппе был написан руками и разошёлся с теми значениями,
по которым проверка доступа реально работает:

  * бесплатному обещали «✅ 50 аккаунтов» — фактический лимит tg-аккаунтов на
    free равен НУЛЮ. Человек подключал продукт ради аккаунтов и получал отказ,
    хотя экран прямо перед оплатой говорил обратное;
  * бесплатному обещали «✅ Базовые рассылки» — `basic_broadcast` требует
    платный тариф;
  * платному обещали «500 аккаунтов» при безлимите, то есть экран ещё и
    недопродавал;
  * бесплатному обещали «1 управляемый бот» при фактических пяти.

Теперь состав собирается из `tariffs.plan_matrix()` — из тех же
`resource_limit` и `feature_plan`, по которым гейтится доступ, с учётом
env-переопределений. Разойтись им негде, потому что второго списка больше нет.
"""
from __future__ import annotations

import re
from pathlib import Path

from bot.utils import tariffs

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"
API = Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"


def _plan(name: str) -> dict:
    for p in tariffs.plan_matrix():
        if p["plan"] == name:
            return p
    raise AssertionError(f"тарифа {name} нет в матрице")


def _limit(plan: dict, key: str) -> dict:
    for l in plan["limits"]:
        if l["key"] == key:
            return l
    raise AssertionError(f"лимита {key} нет в матрице")


def _feature(plan: dict, key: str) -> dict:
    for f in plan["features"]:
        if f["key"] == key:
            return f
    raise AssertionError(f"возможности {key} нет в матрице")


def test_matrix_covers_both_plans():
    assert [p["plan"] for p in tariffs.plan_matrix()] == list(tariffs.PLANS)


def test_limits_repeat_the_enforced_numbers():
    """Матрица не своя таблица, а пересказ `resource_limit`."""
    for p in tariffs.plan_matrix():
        for l in p["limits"]:
            assert l["value"] == tariffs.resource_limit(l["key"], p["plan"]), (
                f"{p['plan']}/{l['key']}: витрина разошлась с лимитом")
            assert l["label"] and l["label"] != l["key"], "подпись не по-русски"


def test_features_repeat_the_enforced_gate():
    for p in tariffs.plan_matrix():
        for f in p["features"]:
            required = tariffs.feature_plan(f["key"])
            expected = (tariffs.PLAN_LEVELS[p["plan"]]
                        >= tariffs.PLAN_LEVELS[required])
            assert f["included"] is expected, (
                f"{p['plan']}/{f['key']}: витрина разошлась с гейтом")


def test_free_plan_does_not_promise_accounts_it_cannot_give():
    """Ровно та неправда, что стояла на экране."""
    free = _plan("free")
    assert _limit(free, "accounts")["value"] == 0
    assert _limit(free, "accounts")["display"] == "0"
    assert _feature(free, "basic_broadcast")["included"] is False


def test_paid_plan_is_shown_as_unlimited_not_capped():
    paid = _plan("paid")
    acc = _limit(paid, "accounts")
    assert tariffs.is_unlimited(acc["value"])
    assert acc["display"] == "∞", "безлимит нельзя показывать числом"


def test_env_override_reaches_the_screen(monkeypatch):
    """Владелец меняет лимит переменной окружения — витрина обязана поехать."""
    monkeypatch.setenv("LIMIT_FREE_BOTS", "3")
    assert _limit(_plan("free"), "bots")["value"] == 3


def test_config_endpoint_serves_the_matrix():
    src = API.read_text("utf-8")
    block = src[src.index("async def miniapp_config"):]
    block = block[:block.index("add_get(\"/api/miniapp/config\"")]
    assert "plan_matrix()" in block, "состав тарифов не уезжает в мини-апп"
    assert '"plans": plans' in block


# ── Мини-апп: второго списка больше нет ──────────────────────────────────────

def test_miniapp_has_no_hand_written_plan_list():
    # Комментарии описывают прежнюю неправду — они не то, что видит человек.
    src = re.sub(r"^\s*//.*$", "", INDEX.read_text("utf-8"), flags=re.M)
    assert "_PLAN_FEATURES" not in src, (
        "список состава тарифа снова написан руками — он разойдётся с гейтом")
    for lie in ("50 аккаунтов", "500 аккаунтов", "1 управляемый бот",
                "Базовые рассылки"):
        assert lie not in src, f"на экране снова зашитое обещание: {lie}"


def test_billing_builds_features_from_the_server():
    src = INDEX.read_text("utf-8")
    fn = src[src.index("function _planLines("):]
    fn = fn[:fn.index("\n}", fn.index("\n}") + 2) + 2]
    assert "window._planMatrix" in fn
    assert "f.included" in fn, "платное и бесплатное не различаются"


def test_billing_does_not_invent_a_list_when_the_matrix_is_missing():
    """О составе тарифа принимают решение о покупке — выдумывать его нельзя."""
    src = INDEX.read_text("utf-8")
    fn = src[src.index("async function openBilling()"):]
    fn = fn[:fn.index("\n}\n")]
    assert "_planLines(plan)" in fn
    assert re.search(r"if \(!feats\)", fn), (
        "нет ветки на случай, когда состав тарифа не загрузился")
    assert "не загрузился" in fn
