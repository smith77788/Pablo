"""
ExperimentTracker — evaluates past experiments against live DB metrics.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from factory.agents.base import FactoryAgent
from factory import db

DB_PATH = Path(__file__).parent.parent.parent / "nevesty-models" / "data.db"


class ExperimentTracker(FactoryAgent):
    name = "ExperimentTracker"
    department = "analytics"
    system_prompt = "You are an experiment tracking analyst for a modeling agency."

    def _get_current_metrics(self) -> dict:
        """Read current business metrics from nevesty-models DB."""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            metrics = {}

            # orders_7d
            r = cur.execute(
                "SELECT COUNT(*) as c FROM orders WHERE created_at >= datetime('now','-7 days')"
            ).fetchone()
            metrics["orders_7d"] = r["c"] if r else 0

            # conversion (new -> confirmed/completed)
            total = cur.execute("SELECT COUNT(*) as c FROM orders").fetchone()
            converted = cur.execute(
                "SELECT COUNT(*) as c FROM orders WHERE status IN ('confirmed','in_progress','completed')"
            ).fetchone()
            if total and total["c"] > 0:
                metrics["conversion_pct"] = round(converted["c"] / total["c"] * 100, 1)

            # avg_rating
            r = cur.execute("SELECT AVG(rating) as r FROM reviews WHERE approved=1").fetchone()
            if r and r["r"]:
                metrics["reviews_avg_rating"] = round(r["r"], 2)

            # repeat_clients
            r = cur.execute(
                """
                SELECT COUNT(*) as c FROM (
                  SELECT phone FROM orders GROUP BY phone HAVING COUNT(*) >= 2
                ) t
                """
            ).fetchone()
            metrics["repeat_clients"] = r["c"] if r else 0

            conn.close()
            return metrics
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("ExperimentTracker: metrics read error: %s", e)
            return {}

    def evaluate_experiments(self) -> list[dict]:
        """Evaluate pending growth_action experiments against current metrics."""
        current = self._get_current_metrics()
        if not current:
            return []

        try:
            pending = db.fetch_all(
                "SELECT * FROM growth_actions "
                "WHERE outcome='pending' AND metric_name IS NOT NULL AND metric_baseline IS NOT NULL"
            )
        except Exception:
            return []

        results = []
        for exp in pending:
            metric = exp.get("metric_name")
            baseline = exp.get("metric_baseline")
            target = exp.get("metric_target")
            current_val = current.get(metric)

            if current_val is None or baseline is None:
                continue

            outcome = "inconclusive"
            if target is not None:
                if current_val >= target:
                    outcome = "success"
                elif current_val <= baseline * 0.95:
                    outcome = "fail"

            # Update in DB
            try:
                db.execute(
                    """UPDATE growth_actions
                       SET metric_current=?, outcome=?, evaluated_at=?
                       WHERE id=?""",
                    (current_val, outcome, datetime.now(timezone.utc).isoformat(), exp["id"]),
                )
            except Exception:
                pass

            results.append({
                "id": exp["id"],
                "action_type": exp.get("action_type"),
                "metric": metric,
                "baseline": baseline,
                "target": target,
                "current": current_val,
                "outcome": outcome,
            })

        return results

    # ─── New: real-metric feedback loop ──────────────────────────────

    def get_active_experiments(self) -> list[dict]:
        """Return all experiments in 'running' status from the experiments table."""
        try:
            return db.fetch_all(
                "SELECT * FROM experiments WHERE status='running' ORDER BY started_at DESC"
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "ExperimentTracker.get_active_experiments error: %s", e
            )
            return []

    def record_metric_result(
        self, experiment_id: int, metric_name: str, value: float
    ) -> dict:
        """Update experiment with a measured metric result and evaluate outcome.

        Rules
        -----
        - metric_after >= metric_before * 1.05  → status = 'success'
        - metric_after <= metric_before * 0.90  → status = 'failed'
        - otherwise                              → status stays 'running'
        - metric_before == 0 and value > 0      → status = 'success'
        """
        try:
            exp = db.fetch_one(
                "SELECT * FROM experiments WHERE id=?", (experiment_id,)
            )
            if not exp:
                return {"error": "not found", "id": experiment_id}

            metric_before = exp.get("conversion_a") or 0.0
            metric_after = float(value)

            if metric_before > 0:
                delta_pct = (metric_after - metric_before) / metric_before * 100
                if delta_pct >= 5:
                    new_status = "success"
                elif delta_pct <= -10:
                    new_status = "failed"
                else:
                    new_status = "running"
            else:
                new_status = "running" if metric_after == 0 else "success"

            # Persist: store new value in conversion_b; update status when conclusive
            update_fields: dict[str, object] = {"conversion_b": metric_after}
            if new_status in ("success", "failed"):
                update_fields["status"] = "concluded"
                update_fields["result"] = new_status
                update_fields["concluded_at"] = datetime.now(timezone.utc).isoformat()
                update_fields["notes"] = (
                    (exp.get("notes") or "")
                    + f"\n[AutoMetric] metric={metric_name}, before={metric_before}, "
                    f"after={metric_after}, status={new_status}"
                )

            sets = ", ".join(f"{k}=?" for k in update_fields)
            db.execute(
                f"UPDATE experiments SET {sets} WHERE id=?",
                (*update_fields.values(), experiment_id),
            )

            import logging
            logging.getLogger(__name__).info(
                "[ExperimentTracker] id=%d metric=%s before=%.2f after=%.2f → %s",
                experiment_id, metric_name, metric_before, metric_after, new_status,
            )
            return {
                "id": experiment_id,
                "metric_name": metric_name,
                "status": new_status,
                "metric_before": metric_before,
                "metric_after": metric_after,
            }
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "ExperimentTracker.record_metric_result error: %s", e
            )
            return {"error": str(e), "id": experiment_id}

    def run(self, **kwargs) -> dict:
        results = self.evaluate_experiments()
        success_count = sum(1 for r in results if r["outcome"] == "success")
        fail_count = sum(1 for r in results if r["outcome"] == "fail")
        return {
            "evaluated": len(results),
            "success": success_count,
            "fail": fail_count,
            "details": results,
        }
