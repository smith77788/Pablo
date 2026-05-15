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

    def synthesize_dept_reports(self, dept_results: dict) -> dict:
        """Synthesize department reports into a strategic briefing and delegation plan."""
        summary_parts = []
        for dept, result in dept_results.items():
            if isinstance(result, dict):
                for role, data in result.items():
                    if isinstance(data, dict) and 'result' in data:
                        summary_parts.append(f"[{dept}/{role}]: {str(data['result'])[:200]}")
                    elif isinstance(data, str):
                        summary_parts.append(f"[{dept}/{role}]: {data[:200]}")
            elif isinstance(result, str):
                summary_parts.append(f"[{dept}]: {result[:200]}")

        dept_summary = "\n".join(summary_parts[:20]) if summary_parts else "No department reports available"

        recent_decisions = db.get_recent_decisions(5)
        prev_decisions_str = ", ".join(d.get("decision_type", "") for d in recent_decisions) if recent_decisions else "none"

        briefing = self.think_json(
            "Ты CEO. Проанализируй отчёты всех департаментов и создай стратегическое резюме. Верни JSON:\n"
            "{\n"
            '  "key_insights": ["insight1", "insight2", "insight3"],\n'
            '  "priority_issues": ["issue1", "issue2"],\n'
            '  "next_cycle_focus": "название департамента который нужно усилить",\n'
            '  "recommended_action": "конкретное действие на следующую неделю",\n'
            '  "health_score": 0-100,\n'
            '  "summary": "2-3 предложения с итогом"\n'
            "}",
            context={
                "department_reports": dept_summary,
                "previous_decisions": prev_decisions_str,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            max_tokens=800,
        )

        if not isinstance(briefing, dict):
            briefing = {
                "key_insights": [],
                "priority_issues": [],
                "next_cycle_focus": "marketing",
                "recommended_action": "Continue current strategy",
                "health_score": 70,
                "summary": "CEO synthesis unavailable",
            }

        cycle_id = datetime.now(timezone.utc).isoformat()
        db.save_ceo_decision(
            cycle_id=cycle_id,
            decision_text=briefing.get("summary", ""),
            metadata=briefing,
        )
        logger.info("[CEO] Synthesis: health=%s focus=%s", briefing.get("health_score"), briefing.get("next_cycle_focus"))
        return briefing

    def generate_weekly_report(self) -> str:
        """Generate a weekly summary report from recent decisions and metrics."""
        recent_decisions = db.get_recent_decisions(20)
        dec_types = {}
        for d in recent_decisions:
            t = d.get("decision_type", "other")
            dec_types[t] = dec_types.get(t, 0) + 1

        dec_summary = "\n".join(f"- {t}: {c} раз" for t, c in dec_types.items()) if dec_types else "Нет решений"

        report = self.think(
            "Ты CEO. Составь краткий недельный отчёт (5-8 пунктов).\n"
            "Структура: что было сделано, ключевые метрики, проблемы, план на следующую неделю.\n"
            "Отвечай на русском, деловой стиль, конкретно.",
            context={"decisions_this_week": dec_summary},
            max_tokens=500,
        )
        return report or "Weekly report unavailable"

    def generate_monthly_report(self) -> dict:
        """Generate a monthly strategic report with KPIs and roadmap."""
        recent_decisions = db.get_recent_decisions(50)
        dec_types: dict[str, int] = {}
        for d in recent_decisions:
            t = d.get("decision_type", "other")
            dec_types[t] = dec_types.get(t, 0) + 1

        active_products = db.get_active_products()
        running_experiments = db.get_running_experiments()

        report = self.think_json(
            "Ты CEO. Составь ежемесячный стратегический отчёт. Верни JSON:\n"
            "{\n"
            '  "period": "Месяц ГГГГ",\n'
            '  "executive_summary": "2-3 предложения итога месяца",\n'
            '  "kpis": {"total_decisions": N, "experiments_run": N, "active_products": N},\n'
            '  "achievements": ["достижение 1", "достижение 2", "достижение 3"],\n'
            '  "challenges": ["проблема 1", "проблема 2"],\n'
            '  "next_month_goals": ["цель 1", "цель 2", "цель 3"],\n'
            '  "strategic_direction": "куда движемся в следующем месяце",\n'
            '  "health_trend": "improving|stable|declining"\n'
            "}",
            context={
                "decisions_by_type": dec_types,
                "active_products": len(active_products),
                "running_experiments": len(running_experiments),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            max_tokens=700,
        )

        if not isinstance(report, dict):
            report = {
                "period": datetime.now(timezone.utc).strftime("%B %Y"),
                "executive_summary": "Monthly report unavailable",
                "kpis": {"total_decisions": len(recent_decisions), "experiments_run": len(running_experiments), "active_products": len(active_products)},
                "achievements": [],
                "challenges": [],
                "next_month_goals": [],
                "strategic_direction": "Continue current strategy",
                "health_trend": "stable",
            }

        cycle_id = f"monthly_{datetime.now(timezone.utc).strftime('%Y_%m')}"
        db.save_ceo_decision(
            cycle_id=cycle_id,
            decision_text=report.get("executive_summary", ""),
            metadata={"type": "monthly_report", **report},
        )
        logger.info("[CEO] Monthly report: trend=%s direction=%s", report.get("health_trend"), report.get("strategic_direction", "")[:40])
        return report

    def track_decision_execution(self, decision_id: int) -> dict:
        """Check if a previous decision has been executed and its impact."""
        decisions = db.fetch_all(
            "SELECT * FROM decisions WHERE id=? LIMIT 1", (decision_id,)
        )
        if not decisions:
            return {"error": "Decision not found"}

        decision = decisions[0]
        status = self.think_json(
            "Оцени выполнение стратегического решения. Верни JSON:\n"
            '{"executed": true/false, "impact": "описание влияния", "completion_pct": 0-100, "blockers": []}',
            context={
                "decision_type": decision.get("decision_type"),
                "rationale": decision.get("rationale"),
                "payload": decision.get("payload"),
                "created_at": decision.get("created_at"),
            },
            max_tokens=300,
        )
        if isinstance(status, dict):
            db.run(
                "UPDATE decisions SET executed=? WHERE id=?",
                (1 if status.get("executed") else 0, decision_id),
            )
        return status or {"executed": False, "impact": "Unknown", "completion_pct": 0, "blockers": []}

    def propose_ab_experiment(self, context: dict) -> dict:
        """Propose a new A/B experiment based on current performance."""
        experiment = self.think_json(
            "Предложи конкретный A/B эксперимент для агентства моделей. Верни JSON:\n"
            "{\n"
            '  "name": "название эксперимента",\n'
            '  "hypothesis": "гипотеза что проверяем",\n'
            '  "variant_a": "контрольная версия",\n'
            '  "variant_b": "тестируемая версия",\n'
            '  "metric": "что измеряем (конверсия/CTR/retention)",\n'
            '  "duration_days": 14,\n'
            '  "expected_lift_pct": 15\n'
            "}",
            context=context,
            max_tokens=400,
        )
        if not isinstance(experiment, dict):
            experiment = {
                "name": "Homepage CTA Test",
                "hypothesis": "Изменение цвета кнопки увеличит CTR",
                "variant_a": "Золотая кнопка 'Забронировать'",
                "variant_b": "Белая кнопка 'Выбрать модель'",
                "metric": "CTR на booking страницу",
                "duration_days": 14,
                "expected_lift_pct": 20,
            }
        logger.info("[CEO] Proposed experiment: %s", experiment.get("name"))
        return experiment

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
