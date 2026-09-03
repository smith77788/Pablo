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


# ── Фаза 2: детектор вспышек ─────────────────────────────────────────────────

def test_threat_needs_minimum_sample():
    """Одна смерть — статистический шум, а не вспышка."""
    assert ie.classify_threat(1, 1, 100) == "calm"
    assert ie.classify_threat(0, 0, 100) == "calm"


def test_threat_detects_acceleration_not_absolute_count():
    """Вспышка — это УСКОРЕНИЕ относительно собственной суточной нормы."""
    # 8 смертей за сутки, но равномерно (0 за последний час) — не шторм
    assert ie.classify_threat(0, 8, 100) in ("watch", "warning")
    # те же 8 за сутки, но 5 из них за последний час — шторм
    assert ie.classify_threat(5, 8, 100) == "storm"


def test_threat_share_of_fleet():
    """Доля выкошенного флота — самостоятельный признак шторма."""
    assert ie.classify_threat(0, 6, 4) == "storm"      # 60% флота
    assert ie.classify_threat(0, 5, 10) == "warning"   # ~33%


def test_threat_levels_are_known_values():
    for args in [(0, 0, 0), (5, 8, 100), (0, 2, 100), (0, 6, 4), (1, 3, 10)]:
        assert ie.classify_threat(*args) in ie.THREAT_LEVELS


def test_threat_handles_garbage_input():
    assert ie.classify_threat(None, None, None) == "calm"
    assert ie.classify_threat(-5, -5, -5) == "calm"


def test_outbreak_detector_present_and_wired():
    assert hasattr(ie, "detect_outbreaks")
    src = _read("services/immunity_engine.py")
    # подключён в основной проход движка
    assert "await detect_outbreaks(pool)" in src
    # затухание по ФАКТУ отсутствия смертей в окне, а не по времени строки
    assert "NOT EXISTS" in src and "immunity_signatures s" in src
    # объяснимость: предупреждение в лог при warning/storm
    assert "ban-weather[%s]" in src
