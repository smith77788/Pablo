from __future__ import annotations
import time
import logging
from collections import deque
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


class BehavioralEngine:
    """Движок анализа поведения аккаунтов: аномалии, предсказания, обучение."""

    def __init__(self, max_history: int = 1000):
        self._events: deque = deque(maxlen=max_history)
        self._ban_predictions: dict[int, float] = {}

    def record_event(self, account_id: int, action: str, success: bool,
                     is_flood: bool = False, is_peer_flood: bool = False,
                     duration_ms: int = 0) -> None:
        self._events.append({
            "ts": time.time(),
            "acc": account_id,
            "action": action,
            "ok": success,
            "flood": is_flood,
            "peer_flood": is_peer_flood,
            "dur": duration_ms,
        })
        self._update_ban_prediction(account_id)

    def _update_ban_prediction(self, account_id: int) -> None:
        acc_events = [e for e in self._events if e["acc"] == account_id][-50:]
        if len(acc_events) < 10:
            self._ban_predictions[account_id] = 0.0
            return
        total = len(acc_events)
        flood_count = sum(1 for e in acc_events if e["flood"])
        peer_flood_count = sum(1 for e in acc_events if e["peer_flood"])
        fail_count = sum(1 for e in acc_events if not e["ok"])
        flood_rate = flood_count / total
        peer_flood_rate = peer_flood_count / total
        fail_rate = fail_count / total
        prediction = min(1.0, flood_rate * 2.0 + peer_flood_rate * 3.0 + fail_rate * 0.5)
        self._ban_predictions[account_id] = round(prediction, 3)

    def get_ban_risk(self, account_id: int) -> dict:
        risk = self._ban_predictions.get(account_id, 0.0)
        if risk > 0.7:
            level = "critical"
            action = "pause_account"
        elif risk > 0.4:
            level = "high"
            action = "slow_down"
        elif risk > 0.2:
            level = "medium"
            action = "monitor"
        else:
            level = "low"
            action = "proceed"
        return {"risk": risk, "level": level, "recommended_action": action}

    def detect_anomalies(self, account_id: int) -> list[dict]:
        anomalies = []
        acc_events = [e for e in self._events if e["acc"] == account_id][-30:]
        if len(acc_events) < 5:
            return anomalies
        recent_floods = sum(1 for e in acc_events[-10:] if e["flood"])
        earlier_floods = sum(1 for e in acc_events[:-10] if e["flood"])
        if recent_floods > 0 and earlier_floods == 0:
            anomalies.append({
                "type": "sudden_floods",
                "severity": "high",
                "message": "Резкое увеличение FloodWait — возможна эскалация",
            })
        avg_dur = sum(e["dur"] for e in acc_events) / len(acc_events)
        slow_events = [e for e in acc_events if e["dur"] > avg_dur * 3]
        if len(slow_events) > 3:
            anomalies.append({
                "type": "slowdown",
                "severity": "medium",
                "message": "Аккаунт замедляется — возможен stealth ban",
            })
        recent_fail_rate = sum(1 for e in acc_events[-5:] if not e["ok"]) / 5
        if recent_fail_rate > 0.6:
            anomalies.append({
                "type": "high_failure_rate",
                "severity": "high",
                "message": "Более 60% последних операций упали",
            })
        return anomalies

    def get_account_stats(self, account_id: int) -> dict:
        acc_events = [e for e in self._events if e["acc"] == account_id]
        if not acc_events:
            return {"total": 0, "success_rate": 0, "flood_rate": 0}
        total = len(acc_events)
        ok = sum(1 for e in acc_events if e["ok"])
        floods = sum(1 for e in acc_events if e["flood"])
        return {
            "total": total,
            "success_rate": round(ok / total * 100, 1),
            "flood_rate": round(floods / total * 100, 1),
            "ban_prediction": self._ban_predictions.get(account_id, 0.0),
        }

    def get_recommendations(self, owner_id: int, pool: asyncpg.Pool) -> list[dict]:
        recs = []
        acc_ids = set(e["acc"] for e in self._events)
        for acc_id in acc_ids:
            risk = self.get_ban_risk(acc_id)
            if risk["level"] == "critical":
                recs.append({
                    "account_id": acc_id,
                    "risk": risk,
                    "recommendation": f"Аккаунт {acc_id}: критический риск бана. Рекомендуется пауза.",
                })
            elif risk["level"] == "high":
                recs.append({
                    "account_id": acc_id,
                    "risk": risk,
                    "recommendation": f"Аккаунт {acc_id}: высокий риск. Увеличьте задержки.",
                })
        anomalies = []
        for acc_id in acc_ids:
            acc_anomalies = self.detect_anomalies(acc_id)
            for a in acc_anomalies:
                a["account_id"] = acc_id
                anomalies.append(a)
        recs.extend(anomalies)
        return recs


_engine: BehavioralEngine | None = None


def get_behavioral_engine() -> BehavioralEngine:
    global _engine
    if _engine is None:
        _engine = BehavioralEngine()
    return _engine
