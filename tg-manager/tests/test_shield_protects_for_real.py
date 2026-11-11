"""«Защита аккаунтов» защищает на деле, а не на экране.

Что было сломано (аудит 10.11.2026):

* Риск бана не измерял ничего. Оценка смотрела только в `op_telemetry` и
  ждала исход «ban», которого не пишет никто, поэтому весь флот видел одно и
  то же «Риск 0% · бан 5%» (сигмоида от −3). Ограничения Telegram, спам-блок,
  FloodWait из журнала флудов, траст, возраст и прокси в оценку не входили.
* Пауза щита была бессрочной: щит снимал `is_active` и обещал «24 ч», а
  вернуть аккаунт было некому.
* Выключенная авто-пауза врала: решение «pause» не применялось, а в историю и
  в Telegram уходило «Убран из работы» / «поставлен на паузу».
* Записка щита была «risk=0.50 ban_prob=0.27 flood_rate_1h=0.00».

Здесь — чистые проверки модели и решения. Проверки на живой базе — в
`test_shield_policy_postgres.py`.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest


@pytest.fixture
def pe():
    from services import physics_engine
    return physics_engine


@pytest.fixture
def sh():
    from services import account_shield
    return account_shield


# ── Модель риска ─────────────────────────────────────────────────────────────

def test_a_clean_account_is_not_five_percent_banned(pe):
    r = pe.score_signals({"age_days": 120, "has_proxy": True, "trust_score": 1.0})
    assert r["risk_score"] == 0.0 and r["factors"] == []
    assert r["ban_probability"] < 0.03, (
        "у чистого аккаунта снова «бан 5%» — база вероятности не честная")


def test_accounts_with_different_lives_get_different_numbers(pe):
    """Главная жалоба: у всего флота одно и то же число."""
    clean = pe.score_signals({"age_days": 120, "has_proxy": True})
    young = pe.score_signals({"age_days": 2, "has_proxy": True})
    flooded = pe.score_signals({"age_days": 120, "has_proxy": True,
                                "floods_24h": 3, "floods_7d": 6,
                                "max_flood_s_7d": 2400})
    banned = pe.score_signals({"age_days": 120, "acc_status": "spamblock"})
    scores = [clean["risk_score"], young["risk_score"],
              flooded["risk_score"], banned["risk_score"]]
    assert scores == sorted(scores) and len(set(scores)) == 4, scores
    assert banned["risk_score"] >= 0.9


@pytest.mark.parametrize("sig,code", [
    ({"severe_days_ago": 1.0}, "severe"),
    ({"restrictions_7d": 2}, "restrictions"),
    ({"floods_24h": 1}, "floods_24h"),
    ({"floods_7d": 3}, "floods_7d"),
    ({"max_flood_s_7d": 1800}, "long_flood"),
    ({"ops_7d": 20, "failed_7d": 8}, "failures"),
    ({"age_days": 2, "ops_24h": 60}, "load"),
    ({"age_days": 1}, "young"),
    ({"trust_score": 0.2}, "low_trust"),
    ({"has_proxy": False}, "no_proxy"),
    ({"acc_status": "spamblock"}, "dead_status"),
])
def test_every_factor_is_seen_and_explained_in_russian(pe, sig, code):
    r = pe.score_signals(sig)
    codes = {f["code"] for f in r["factors"]}
    assert code in codes, f"фактор {code} не учтён: {r}"
    f = next(x for x in r["factors"] if x["code"] == code)
    assert re.search("[а-яА-Я]", f["label"]) and f["impact"] > 0
    # «Telegram» и «FloodWait» — имена, которые продукт пишет везде.
    text = (f["label"] + f.get("detail", "")).replace("Telegram", "").replace("FloodWait", "")
    assert not re.search(r"[a-zA-Z]{3,}", text), (
        f"владелец не читает по-английски: {f}")


def test_risk_never_exceeds_one_and_factors_are_sorted(pe):
    r = pe.score_signals({"acc_status": "banned", "severe_days_ago": 0.5,
                          "floods_24h": 9, "floods_7d": 30, "age_days": 1,
                          "trust_score": 0.0, "has_proxy": False,
                          "ops_24h": 500, "ops_7d": 600, "failed_7d": 400,
                          "max_flood_s_7d": 99999})
    assert 0.0 < r["risk_score"] <= 1.0 and 0.0 < r["ban_probability"] <= 1.0
    impacts = [f["impact"] for f in r["factors"]]
    assert impacts == sorted(impacts, reverse=True)


def test_scores_are_recomputed_for_accounts_without_operations(pe):
    """Оценка аккаунта, переставшего работать, обязана остывать."""
    src = pe._SIGNALS_SQL
    acc_cte = src.split("re AS")[0]
    assert "op_telemetry" not in acc_cte, (
        "выборка аккаунтов снова зависит от телеметрии — старые оценки не остынут")
    run_src = inspect.getsource(pe.recompute_all)
    assert "op_telemetry" not in run_src


def test_owner_cleared_risk_is_respected(pe):
    """«Сбросить кулдаун» владельца обязан гасить и эти факторы."""
    assert pe._SIGNALS_SQL.count("risk_cleared_at") >= 3


# ── Решение щита ─────────────────────────────────────────────────────────────

_HIGH = {"risk_score": 0.95, "ban_probability": 0.9,
         "factors": [{"code": "floods_24h", "label": "FloodWait за сутки",
                      "detail": "3 за сутки", "impact": 0.4}]}


def test_pause_mode_pauses_and_says_why_in_russian(sh):
    d = sh.decide(_HIGH, sh.ShieldConfig(), 5)
    assert d.action == "pause"
    assert "risk=" not in d.note and "ban_prob" not in d.note
    assert "FloodWait за сутки" in d.note and "95%" in d.note


def test_disabled_pause_is_not_recorded_as_a_pause(sh):
    """Выключенная пауза = охлаждение, и записка говорит об этом прямо."""
    cfg = sh.ShieldConfig(response_mode="cool", auto_pause=False)
    d = sh.decide(_HIGH, cfg, 5)
    assert d.action == "cool", "при выключенной паузе в историю снова уйдёт «Пауза»"
    assert "пауза выключена" in d.note


def test_warn_only_mode_changes_nothing(sh):
    cfg = sh.ShieldConfig(response_mode="warn", auto_pause=False)
    assert sh.decide(_HIGH, cfg, 5).action == "warn"
    mid = {"risk_score": 0.75, "ban_probability": 0.4, "factors": []}
    assert sh.decide(mid, cfg, 5).action == "warn"


def test_thresholds_are_the_owners(sh):
    """Порог паузы был зашит 0.9/0.8 — теперь он настройка."""
    risk = {"risk_score": 0.8, "ban_probability": 0.3, "factors": []}
    assert sh.decide(risk, sh.ShieldConfig(), 1).action == "cool"
    careful = sh.ShieldConfig(**sh.PRESETS["careful"])
    assert sh.decide(risk, careful, 1).action == "pause"
    bold = sh.ShieldConfig(**sh.PRESETS["bold"])
    assert sh.decide(risk, bold, 1).action in ("warn", "cool")


def test_exempt_account_is_left_alone(sh):
    class _P:
        async def fetchrow(self, *a):
            return {"risk_score": 0.99, "ban_probability": 0.99,
                    "flood_rate_1h": 0, "ops_24h": 0, "last_flood_at": None,
                    "factors": None, "computed_at": None}
    cfg = sh.ShieldConfig(exempt_account_ids=[5])
    d = asyncio.run(sh.evaluate_account(_P(), 5, 1, cfg))
    assert d.action == "ok" and d.level == "exempt"


def test_balanced_preset_is_the_old_default(sh):
    """У владельцев без настроек поведение не меняется."""
    cfg = sh.ShieldConfig()
    for k, v in sh.PRESETS["balanced"].items():
        assert getattr(cfg, k) == v, k
    assert sh._detect_preset(cfg) == "balanced"


def test_preset_expands_into_thresholds(sh):
    calls = []

    class _P:
        async def execute(self, sql, *a):
            calls.append((sql, a))

        async def fetchrow(self, *a):
            return None

    asyncio.run(sh.save_shield_config(_P(), 7, preset="careful"))
    sql, args = calls[0]
    assert "pause_risk_threshold" in sql and 0.75 in args and 48 in args


# ── Пауза заканчивается ──────────────────────────────────────────────────────

def test_pause_is_marked_with_its_end(sh):
    writes = []

    class _P:
        async def execute(self, sql, *a):
            writes.append(sql)
            return "UPDATE 1"

        async def fetchrow(self, *a):
            return None

    d = sh.ShieldDecision(account_id=5, risk_score=0.95, ban_probability=0.9,
                          action="pause", note="x")
    assert asyncio.run(sh.apply_shield_decision(_P(), d, 1, cfg=sh.ShieldConfig()))
    pause_sql = next(w for w in writes if "is_active = FALSE" in w)
    assert "shield_paused_until" in pause_sql, (
        "пауза снова без срока — вернуть аккаунт будет некому")


def test_expired_pause_is_lifted_by_the_cycle(sh):
    src = inspect.getsource(sh.resume_expired)
    assert "is_active = TRUE" in src and "shield_paused_until <= NOW()" in src
    assert "sql_not_dead" in src, "мёртвый за время паузы аккаунт вернётся в работу"
    assert "resume_expired(" in inspect.getsource(sh.run_owner)


def test_manual_switch_drops_the_pause_mark():
    """Выключенный владельцем аккаунт щит по истечении срока не включает."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "services", "mini_app_accounts.py"), encoding="utf-8").read()
    m = re.search(r"SET is_active=\$1, shield_paused_until=NULL", src)
    assert m, "ручное переключение не снимает метку паузы щита"


def test_history_is_not_flooded_by_repeats(sh):
    src = inspect.getsource(sh.run_owner)
    assert "_recently_recorded(" in src and "record=not repeat" in src
