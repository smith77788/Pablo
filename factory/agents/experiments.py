"""Autonomous experiment system for Nevesty Models AI Factory."""

import sqlite3
import json
import re
import logging
from datetime import datetime

from factory.agents.base import FactoryAgent

logger = logging.getLogger(__name__)

FACTORY_DB_PATH = "/home/user/Pablo/factory/factory.db"
DATA_DB_PATH = "/home/user/Pablo/nevesty-models/data.db"


class ExperimentProposer(FactoryAgent):
    """Proposes A/B experiment hypotheses based on current metrics."""

    department = "experiments"
    role = "experiment_proposer"
    system_prompt = (
        "You are an A/B experiment specialist for a modeling agency platform. "
        "Propose concrete, measurable hypotheses to improve conversion and order volume."
    )

    def think(self):
        """Build a prompt based on current metrics from data.db."""
        try:
            conn = sqlite3.connect(DATA_DB_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) as total_orders,
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) as cancelled
                FROM orders WHERE created_at >= datetime('now', '-30 days')
            """)
            row = cursor.fetchone()
            conn.close()
            total = row[0] or 0
            completed = row[1] or 0
            cancelled = row[2] or 0
            conversion = round(completed / total * 100, 1) if total > 0 else 0
        except Exception:
            total, completed, cancelled, conversion = 0, 0, 0, 0

        return (
            f"Current 30-day metrics: {total} total orders, "
            f"{completed} completed ({conversion}% conversion), "
            f"{cancelled} cancelled. "
            f"Based on these metrics, propose 3 concrete A/B experiments "
            f"that could improve conversion rate or order volume. "
            f"Format as JSON array with fields: hypothesis, metric, baseline, target. "
            f"Focus on: catalog presentation, booking flow, messaging, pricing display."
        )

    def run(self):
        prompt = self.think()
        analysis = super().think(prompt)
        # Try to save proposed experiments to factory.db
        try:
            conn = sqlite3.connect(FACTORY_DB_PATH)
            cursor = conn.cursor()
            json_match = re.search(r'\[.*\]', analysis, re.DOTALL)
            if json_match:
                experiments = json.loads(json_match.group())
                for exp in experiments[:3]:  # max 3
                    cursor.execute(
                        "INSERT INTO nevesty_experiments (hypothesis, metric, baseline, target) VALUES (?, ?, ?, ?)",
                        (
                            str(exp.get('hypothesis', ''))[:500],
                            str(exp.get('metric', 'conversion'))[:100],
                            float(exp.get('baseline', 0)),
                            float(exp.get('target', 0)),
                        )
                    )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug("ExperimentProposer DB save skipped: %s", e)
        return analysis


class ExperimentTracker(FactoryAgent):
    """Tracks ongoing experiments and reports results."""

    department = "experiments"
    role = "experiment_tracker"
    system_prompt = (
        "You are an experiment tracking specialist. "
        "Analyze active experiments and recommend prioritization."
    )

    def think(self):
        """Build a prompt from active experiments in factory.db."""
        try:
            conn = sqlite3.connect(FACTORY_DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, hypothesis, metric, baseline, target, status, created_at "
                "FROM nevesty_experiments WHERE status IN ('proposed', 'running') "
                "ORDER BY created_at DESC LIMIT 5"
            )
            rows = cursor.fetchall()
            conn.close()
            if not rows:
                return (
                    "No active experiments found. "
                    "Recommend starting new experiments to improve business metrics."
                )

            exp_list = []
            for r in rows:
                exp_list.append(
                    f"ID {r[0]}: {r[1]} (metric: {r[2]}, baseline: {r[3]}, target: {r[4]}, status: {r[5]})"
                )
            return (
                "Active experiments:\n" + "\n".join(exp_list) +
                "\nAnalyze progress and recommend which to prioritize or close."
            )
        except Exception as e:
            return (
                f"Cannot access experiments DB: {e}. "
                "Recommend creating new experiment hypotheses."
            )

    def run(self):
        prompt = self.think()
        return super().think(prompt)


class ResultAnalyzer(FactoryAgent):
    """Analyzes experiment results and recommends actions."""

    department = "experiments"
    role = "result_analyzer"
    system_prompt = (
        "You are a results analysis specialist for a modeling agency. "
        "Provide concrete, data-driven recommendations."
    )

    def think(self):
        return (
            "Analyze the overall experiment pipeline for Nevesty Models modeling agency. "
            "What experiments would have the highest impact on: "
            "1) Increasing catalog-to-booking conversion "
            "2) Reducing order cancellation rate "
            "3) Increasing average order value "
            "4) Growing repeat client rate "
            "Provide specific, actionable recommendations."
        )

    def run(self):
        prompt = self.think()
        return super().think(prompt)
