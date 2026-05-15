"""🧠 Strategic AI Core — CEO Brain: принимает стратегические решения."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)

DECISION_TYPES = {
    "create_mvp":   "Создать новый продукт/MVP",
    "scale":        "Масштабировать продукт",
    "kill":         "Закрыть продукт",
    "iterate":      "Улучшить продукт",
    "grow":         "Усилить маркетинг",
    "experiment":   "Запустить A/B эксперимент",
    "optimize":     "Оптимизировать конверсию",
    "monitor":      "Продолжать мониторинг",
}


class StrategicCore(FactoryAgent):
    name = "strategic_core"
    system_prompt = """Ты — CEO Brain AI Startup Factory. Ты принимаешь стратегические решения.

ТВОИ ПРАВИЛА:
1. Максимум 3-5 активных продуктов одновременно
2. Фокус на росте конверсии — это главный KPI
3. При падении revenue → оптимизируй текущие продукты
4. При низком трафике → усиливай growth engine
5. Эксперименты: conversion > 5% → SCALE, < 2% → KILL, иначе → ITERATE
6. Каждое решение должно быть обосновано данными
7. Не создавай новые MVP если текущие не работают

Отвечай только на русском. Решения должны быть конкретными и actionable."""

    MAX_ACTIVE_PRODUCTS = 5

    def decide(self, analytics_insights: dict, all_metrics: dict) -> list[dict]:
        """Generate strategic decisions based on analytics."""
        active_products = db.get_active_products()
        running_experiments = db.get_running_experiments()
        recent_decisions = db.get_recent_decisions(10)
        pending_actions = db.get_pending_growth_actions(5)
        ideas = db.fetch_all("SELECT * FROM ideas WHERE status='new' ORDER BY priority DESC LIMIT 5")

        context = {
            "analytics_insights": analytics_insights,
            "all_metrics": all_metrics,
            "active_products_count": len(active_products),
            "active_products": [{"id": p["id"], "name": p["name"], "category": p["category"]} for p in active_products],
            "running_experiments": len(running_experiments),
            "recent_decisions": [d["decision_type"] for d in recent_decisions[-5:]],
            "pending_growth_actions": len(pending_actions),
            "top_ideas": [{"id": i["id"], "title": i["title"], "priority": i["priority"]} for i in ideas],
            "max_active_products": self.MAX_ACTIVE_PRODUCTS,
        }

        decisions_raw = self.think_json(
            "На основе данных сгенерируй список стратегических решений. Верни JSON массив:\n"
            "[\n"
            "  {\n"
            '    "type": "create_mvp|scale|kill|iterate|grow|experiment|optimize|monitor",\n'
            '    "product_id": null_или_id,\n'
            '    "priority": 1-10,\n'
            '    "rationale": "обоснование решения",\n'
            '    "action": "конкретное действие которое нужно выполнить",\n'
            '    "expected_impact": "ожидаемый результат"\n'
            "  }\n"
            "]\n"
            "Максимум 5 решений. Только те которые реально нужны сейчас.",
            context=context,
            max_tokens=1500,
        )

        if not isinstance(decisions_raw, list):
            decisions_raw = []

        decisions = []
        cycle_id = datetime.now(timezone.utc).isoformat()

        for d in decisions_raw[:5]:
            if not isinstance(d, dict) or "type" not in d:
                continue
            if d["type"] not in DECISION_TYPES:
                continue

            dec_id = db.insert("decisions", {
                "cycle_id": cycle_id,
                "decision_type": d["type"],
                "product_id": d.get("product_id"),
                "rationale": d.get("rationale", ""),
                "payload": d,
                "executed": 0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            d["_db_id"] = dec_id
            decisions.append(d)
            logger.info("[CEO] Decision: %s — %s", d["type"], d.get("rationale", "")[:60])

        return decisions, cycle_id

    def evaluate_experiment(self, experiment: dict) -> str:
        """Evaluate a running experiment and decide scale/iterate/kill."""
        conv_a = experiment.get("conversion_a", 0)
        conv_b = experiment.get("conversion_b", 0)

        # Rule-based first
        if conv_b > 5.0:
            result = "scale"
        elif conv_b < 2.0 and conv_a >= conv_b:
            result = "kill"
        else:
            result = "iterate"

        # AI confirmation for edge cases
        if 2.0 <= conv_b <= 5.0:
            ai_result = self.think_json(
                "Оцени результат A/B теста и реши: scale, iterate или kill?",
                context=experiment,
                max_tokens=256,
            )
            if isinstance(ai_result, dict) and ai_result.get("result") in ("scale", "iterate", "kill"):
                result = ai_result["result"]

        return result
