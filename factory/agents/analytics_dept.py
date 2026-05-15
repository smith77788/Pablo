"""📊 Analytics Department — Data Analyst, Conversion Analyst, Experiment Evaluator, KPI Tracker."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

_NOW = lambda: datetime.now(timezone.utc).isoformat()

logger = logging.getLogger(__name__)


class DataAnalyst(FactoryAgent):
    department = "analytics"
    role = "data_analyst"
    name = "data_analyst"
    system_prompt = """Ты — Data Analyst агентства моделей Nevesty Models.
Анализируешь данные, находишь паттерны и аномалии.
Даёшь actionable инсайты. Всё на русском."""

    def analyze_trends(self, metrics: dict) -> dict:
        return self.think_json(
            "Проанализируй метрики и найди тренды.\n"
            "Верни JSON:\n"
            '{"trends": [{"metric": "...", "direction": "up|down|stable", "insight": "..."}], '
            '"anomalies": ["..."], "recommendations": ["..."]}',
            context={"metrics": metrics},
            max_tokens=1200,
        ) or {}

    def run(self, context: dict | None) -> dict:
        """Heuristic run — returns data analysis insights."""
        ctx = context or {}
        kpis = ctx.get("nevesty_kpis", {})
        total_users = kpis.get("total_users", 0)
        total_orders = kpis.get("total_orders", 0)
        insights = [
            f"Зарегистрировано пользователей: {total_users}. Анализируй сегментацию для роста конверсии.",
            f"Всего заказов: {total_orders}. Сравни с предыдущим периодом для определения тренда.",
            "Отслеживай аномалии: резкий рост или падение > 20% требует немедленного анализа.",
        ]
        return {
            "insights": insights,
            "recommendations": ["Настроить дашборд метрик в реальном времени"],
            "timestamp": _NOW(),
        }


class ConversionAnalyst(FactoryAgent):
    department = "analytics"
    role = "conversion_analyst"
    name = "conversion_analyst"
    system_prompt = """Ты — Conversion Rate Optimizer для Telegram-бота агентства моделей.
Находишь узкие места в воронке, где теряются пользователи.
Предлагаешь конкретные правки для роста конверсии. Всё на русском."""

    def find_conversion_leaks(self, funnel_data: dict) -> dict:
        return self.think_json(
            "Найди утечки конверсии в воронке и предложи фиксы.\n"
            "Верни JSON:\n"
            '{"leaks": [{"stage": "...", "drop_rate": "X%", "fix": "конкретное действие"}], '
            '"quick_win": "самое важное исправление", "expected_lift": "X%"}',
            context={"funnel": funnel_data},
            max_tokens=1200,
        ) or {}

    def run(self, context: dict | None) -> dict:
        """Heuristic run — returns conversion funnel insights."""
        ctx = context or {}
        kpis = ctx.get("nevesty_kpis", {})
        orders = kpis.get("total_orders", 0)
        users = kpis.get("total_users", 1) or 1
        conv_rate = round(orders / users * 100, 2)
        insights = [
            f"Текущая конверсия: {conv_rate}%. Цель: 5%+. {'Норма' if conv_rate >= 5 else 'Ниже целевого — нужна оптимизация'}.",
            "Ключевые точки оттока: после просмотра каталога и перед оплатой — добавь CTA и упрости форму.",
            "A/B тест кнопки 'Забронировать' vs 'Выбрать модель' может дать +10-15% к конверсии.",
        ]
        return {
            "insights": insights,
            "recommendations": ["Добавить хитмап для анализа поведения пользователей"],
            "timestamp": _NOW(),
        }


class ExperimentEvaluator(FactoryAgent):
    department = "analytics"
    role = "experiment_evaluator"
    name = "experiment_evaluator"
    system_prompt = """Ты — Experiment Evaluator. Оцениваешь результаты A/B тестов.
Применяешь статистические методы для оценки значимости.
Правила: conversion > 5% → SCALE, < 2% → KILL, иначе → ITERATE. Всё на русском."""

    def evaluate(self, experiment: dict) -> dict:
        return self.think_json(
            "Оцени результат A/B теста. Верни JSON:\n"
            '{"decision": "scale|iterate|kill", "confidence": "high|medium|low", '
            '"reasoning": "...", "next_step": "конкретное действие"}',
            context={"experiment": experiment},
            max_tokens=800,
        ) or {}

    def run(self, context: dict | None) -> dict:
        """Heuristic run — returns experiment evaluation insights."""
        ctx = context or {}
        insights = [
            "Эксперименты с lift > 5% следует масштабировать, < 2% — остановить.",
            "Минимальный размер выборки для статзначимости: 1000 пользователей на вариант.",
            "Запускай не более 3 A/B тестов одновременно, чтобы избежать интерференции.",
        ]
        return {
            "insights": insights,
            "recommendations": ["Внедрить автоматический стоп-лосс для убыточных экспериментов"],
            "timestamp": _NOW(),
        }


class KPITracker(FactoryAgent):
    department = "analytics"
    role = "kpi_tracker"
    name = "kpi_tracker"
    system_prompt = """Ты — KPI Tracker для агентства моделей Nevesty Models.
Отслеживаешь ключевые метрики: заказы, конверсию, выручку, NPS.
Сигнализируешь при отклонениях от плана. Всё на русском."""

    def generate_kpi_report(self, metrics: dict, targets: dict) -> dict:
        return self.think_json(
            "Сгенерируй KPI-отчёт. Верни JSON:\n"
            '{"overall_health": "green|yellow|red", '
            '"kpis": [{"name": "...", "actual": "...", "target": "...", "status": "ok|at_risk|missed"}], '
            '"alert": "главная проблема или null", "action_needed": "..."}',
            context={"metrics": metrics, "targets": targets},
            max_tokens=1000,
        ) or {}

    def run(self, context: dict | None) -> dict:
        """Heuristic run — returns KPI tracking insights."""
        ctx = context or {}
        kpis = ctx.get("nevesty_kpis", {})
        orders = kpis.get("total_orders", 0)
        insights = [
            f"Заказы: {orders}. Цель — 100/месяц. {'✅ Выполнено' if orders >= 100 else '⚠️ Недовыполнение'}.",
            "Ключевые KPI: конверсия воронки, средний чек, NPS, время ответа на заявку.",
            "Настрой алерты при отклонении любого KPI > 15% от плана.",
        ]
        return {
            "insights": insights,
            "recommendations": ["Создать еженедельный KPI-дашборд для команды"],
            "timestamp": _NOW(),
        }


class AnalyticsDepartment:
    """Координатор аналитического департамента."""

    def __init__(self) -> None:
        self.analyst = DataAnalyst()
        self.conversion = ConversionAnalyst()
        self.evaluator = ExperimentEvaluator()
        self.kpi = KPITracker()

    def run_full_analysis(self, metrics: dict, experiments: list) -> dict:
        """Запускает все роли и возвращает сводный отчёт."""
        results = {}

        trends = self.analyst.analyze_trends(metrics)
        results["trends"] = trends

        funnel_data = {
            "visits": metrics.get("nevesty_models", {}).get("total_users", 0),
            "catalog_views": metrics.get("nevesty_models", {}).get("total_models", 0),
            "bookings_started": metrics.get("nevesty_models", {}).get("total_orders", 0),
        }
        results["conversion_leaks"] = self.conversion.find_conversion_leaks(funnel_data)

        exp_evaluations = []
        for exp in experiments[:3]:
            eval_result = self.evaluator.evaluate(exp)
            eval_result["experiment_id"] = exp.get("id")
            exp_evaluations.append(eval_result)
            if eval_result.get("decision") in ("scale", "kill"):
                result_val = eval_result["decision"]
                db.execute(
                    "UPDATE experiments SET status='concluded', result=?, concluded_at=?, notes=? WHERE id=?",
                    (result_val, datetime.now(timezone.utc).isoformat(),
                     eval_result.get("reasoning", "")[:300], exp["id"]),
                )
                logger.info("[Analytics Dept] Experiment %d → %s", exp["id"], result_val)
        results["experiment_evaluations"] = exp_evaluations

        targets = {"conversion_target": 5.0, "orders_target": 100}
        results["kpi_report"] = self.kpi.generate_kpi_report(metrics, targets)

        health = results["kpi_report"].get("overall_health", "yellow")
        score = {"green": 80, "yellow": 50, "red": 25}.get(health, 50)
        results["health_score"] = score
        results["recommended_focus"] = trends.get("recommendations", ["conversion"])[0] if trends.get("recommendations") else "conversion"

        return results

    def execute_task(self, task: str, context: dict | None) -> dict:
        """Heuristic department-level task execution — no LLM calls."""
        ctx = context or {}
        task_lower = (task or "").lower()

        roles_used = ["data_analyst", "kpi_tracker"]
        insights: list[str] = []

        analyst_result = self.analyst.run(ctx)
        insights.extend(analyst_result.get("insights", []))

        kpi_result = self.kpi.run(ctx)
        insights.extend(kpi_result.get("insights", []))

        if any(w in task_lower for w in ("conversion", "funnel", "воронка", "конверсия")):
            roles_used.append("conversion_analyst")
            conv_result = self.conversion.run(ctx)
            insights.extend(conv_result.get("insights", []))

        if any(w in task_lower for w in ("experiment", "a/b", "тест", "test")):
            roles_used.append("experiment_evaluator")
            exp_result = self.evaluator.run(ctx)
            insights.extend(exp_result.get("insights", []))

        kpis = ctx.get("nevesty_kpis", {})
        health = "yellow"
        if kpis.get("total_orders", 0) >= 100:
            health = "green"
        elif kpis.get("total_orders", 0) < 20:
            health = "red"

        health_score = {"green": 80, "yellow": 50, "red": 25}[health]

        return {
            "department": "analytics",
            "task": task,
            "roles_used": roles_used,
            "insights": insights,
            "health_score": health_score,
            "recommended_focus": "conversion" if "conversion_analyst" not in roles_used else "experiments",
            "timestamp": _NOW(),
        }
