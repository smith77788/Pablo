"""Ban Weather / Fleet Immune System — Фаза 1 (фундамент).

Тестируем чистую логику движка (сигнатура/автопсия) без БД + наличие схемы
(таблицы + триггер захвата) и централизованного мутатора. См.
docs/BAN_WEATHER_MODULE.md.
"""
from __future__ import annotations

import os

from services import immunity_engine as ie

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── Сигнатура ────────────────────────────────────────────────────────────────

def test_signature_deterministic_and_grouping():
    f1 = {"op_counts": {"mass_invite": 30, "mass_publish": 5},
          "geo_country": "Germany", "warming_age_days": 1.0, "trust_score": 0.2}
    # тот же вход → та же строка (нужно для роллапов/правил)
    assert ie.compute_signature(f1) == ie.compute_signature(dict(f1))
    sig = ie.compute_signature(f1)
    assert "mass_invite" in sig and "geo=Germany" in sig
    assert "warm=fresh" in sig and "trust=low" in sig


def test_signature_buckets():
    assert "warm=fresh" in ie.compute_signature({"warming_age_days": 2})
    assert "warm=young" in ie.compute_signature({"warming_age_days": 10})
    assert "warm=mature" in ie.compute_signature({"warming_age_days": 40})
    assert "trust=high" in ie.compute_signature({"trust_score": 0.8})
    assert "trust=mid" in ie.compute_signature({"trust_score": 0.55})
    # шкала 0..1 (не 0..100): значение 1.0 — максимум доверия, а не "low"
    assert "trust=high" in ie.compute_signature({"trust_score": 1.0})
    assert "trust=low" in ie.compute_signature({"trust_score": 0.1})
    # порядок op_mix не зависит от порядка вставки (детерминизм)
    a = ie.compute_signature({"op_counts": {"a": 1, "b": 9}})
    b = ie.compute_signature({"op_counts": {"b": 9, "a": 1}})
    assert a == b and a.startswith("b+a")


# ── Автопсия ─────────────────────────────────────────────────────────────────

def test_autopsy_structure_and_cause_invites():
    event = {"acc_id": 42, "new_status": "spamblock", "is_death": True}
    feats = {"op_counts": {"mass_invite": 45}, "actions_72h": 45,
             "geo_country": "Poland", "warming_age_days": 20, "trust_score": 0.6,
             "error_rate": 0.1}
    a = ie.build_autopsy(event, feats, {"median_actions_72h": 10})
    assert set(a) >= {"signature", "summary", "probable_cause",
                      "suggested_rule", "differed_from_survivors", "features"}
    assert "инвайт" in a["probable_cause"].lower()
    assert "#42" in a["summary"] and "spamblock" in a["summary"]
    # предложенное правило ссылается на сигнатуру и действие
    assert a["suggested_rule"]["action"] in ("throttle", "quarantine", "bench", "block")
    assert a["suggested_rule"]["match"]["signature"] == a["signature"]


def test_autopsy_cause_fresh_account():
    event = {"acc_id": 7, "new_status": "banned", "is_death": True}
    feats = {"op_counts": {"mass_publish": 40}, "actions_72h": 40,
             "geo_country": "US", "warming_age_days": 1, "trust_score": 0.5}
    a = ie.build_autopsy(event, feats, {})
    assert "свеж" in a["probable_cause"].lower() or "непрогрет" in a["probable_cause"].lower()
    # свежий аккаунт → предложение карантина (жёстче)
    assert a["suggested_rule"]["action"] == "quarantine"


def test_autopsy_differ_from_survivors():
    event = {"acc_id": 1, "new_status": "banned", "is_death": True}
    feats = {"op_counts": {"x": 100}, "actions_72h": 100, "warming_age_days": 30}
    a = ie.build_autopsy(event, feats, {"median_actions_72h": 10})
    assert a["differed_from_survivors"] and "перегруз" in a["differed_from_survivors"]


def test_death_statuses_constant():
    assert "banned" in ie.DEATH_STATUSES and "spamblock" in ie.DEATH_STATUSES
    assert "active" not in ie.DEATH_STATUSES


# ── Схема: таблицы + триггер захвата ─────────────────────────────────────────

def test_schema_tables_and_trigger():
    sql = _read("schema_v146_ban_weather.sql")
    for tbl in ("account_status_events", "immunity_signatures",
                "immunity_rules", "immunity_policy"):
        assert f"CREATE TABLE IF NOT EXISTS {tbl}" in sql, f"нет таблицы {tbl}"
    # триггер-захват на tg_accounts (ключевое решение надёжности)
    assert "_immunity_capture_status" in sql
    assert "AFTER UPDATE OF acc_status ON tg_accounts" in sql
    assert "is_death" in sql
    # идемпотентность
    assert "CREATE OR REPLACE FUNCTION" in sql and "DROP TRIGGER IF EXISTS" in sql


def test_centralized_mutator_present():
    src = _read("services/account_status.py")
    assert "async def set_status" in src
    # обогащает событие, но fail-soft (не критично для смены статуса)
    assert "account_status_events" in src and "log_exc_swallow" in src


def test_engine_layers_present():
    # чистая логика отделена от БД
    for fn in ("compute_signature", "build_autopsy", "process_event",
               "process_pending", "run_once", "start"):
        assert hasattr(ie, fn), f"нет {fn}"
