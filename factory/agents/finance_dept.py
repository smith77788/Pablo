"""💰 Finance Department — финансовый анализ, прогнозы, оптимизация расходов, ценообразование."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)


class RevenueForecaster(FactoryAgent):
    department = "finance"
    role = "revenue_forecaster"
    name = "revenue_forecaster"
    system_prompt = """Ты — Revenue Forecaster агентства моделей Nevesty Models.
Твоя задача — прогнозировать выручку агентства на следующий месяц на основе текущих данных.
Агентство зарабатывает на комиссии с заказов моделей для мероприятий (корпоративы, свадьбы, фотосессии, показы).
Анализируешь сезонность (праздники, лето/зима), количество активных моделей, конверсию заявок в заказы,
средний чек и частоту повторных заказов. Даёшь реалистичные прогнозы с верхней и нижней границей.
Всё на русском языке."""

    def run(self, context: dict) -> dict:
        """Универсальный метод запуска агента."""
        try:
            forecast = self.forecast_revenue(context)
            return {
                "insights": [
                    f"Прогноз выручки: {forecast.get('forecast_rub', '?')} руб.",
                    f"Диапазон: {forecast.get('forecast_low_rub', '?')}–{forecast.get('forecast_high_rub', '?')} руб.",
                ],
                "recommendations": forecast.get("recommended_actions", []),
                "priority": 9,
                "forecast": forecast,
            }
        except Exception as e:
            logger.error("[finance/revenue_forecaster] run error: %s", e)
            return {"insights": [], "recommendations": [], "priority": 9, "forecast": {}}

    def forecast_revenue(self, context: dict) -> dict:
        """Прогноз выручки на следующий месяц."""
        try:
            return self.think_json(
                "Сделай прогноз выручки агентства моделей Nevesty Models на следующий месяц.\n"
                "Учти текущие метрики из контекста, сезонность и тренды.\n"
                "Верни JSON:\n"
                '{"forecast_rub": 150000, '
                '"forecast_low_rub": 100000, '
                '"forecast_high_rub": 220000, '
                '"key_assumptions": ["предположение 1", "предположение 2"], '
                '"revenue_drivers": [{"driver": "корпоративные мероприятия", "contribution_pct": 60, "trend": "рост|стабильно|снижение"}], '
                '"risks": ["риск 1: спад праздников в феврале"], '
                '"recommended_actions": ["действие для увеличения выручки"], '
                '"monthly_breakdown": {"week1": 30000, "week2": 40000, "week3": 45000, "week4": 35000}}',
                context=context,
                max_tokens=1500,
            ) or {}
        except Exception as e:
            logger.error("[finance/revenue_forecaster] forecast_revenue error: %s", e)
            return {}


class CostOptimizer(FactoryAgent):
    department = "finance"
    role = "cost_optimizer"
    name = "cost_optimizer"
    system_prompt = """Ты — Cost Optimizer агентства моделей Nevesty Models.
Твоя задача — находить возможности оптимизации расходов агентства без потери качества сервиса.
Анализируешь типичные статьи расходов: реклама и маркетинг, зарплата менеджеров, хостинг бота,
фотосессии моделей, SMS/уведомления клиентам, юридические и бухгалтерские услуги.
Ищешь дублирующиеся траты, слишком дорогие сервисы, возможности автоматизации вместо ручного труда.
Предлагаешь конкретные меры с оценкой экономии в рублях. Всё на русском языке."""

    def run(self, context: dict) -> dict:
        """Универсальный метод запуска агента."""
        try:
            savings = self.find_cost_savings(context)
            quick_wins = [w.get("action", "") for w in savings.get("quick_wins", [])]
            return {
                "insights": [
                    f"Потенциальная экономия: {savings.get('total_potential_savings_rub', '?')} руб.",
                    f"Срок окупаемости: {savings.get('payback_period_months', '?')} мес.",
                ],
                "recommendations": quick_wins[:3],
                "priority": 7,
                "forecast": {"total_savings_rub": savings.get("total_potential_savings_rub", 0)},
            }
        except Exception as e:
            logger.error("[finance/cost_optimizer] run error: %s", e)
            return {"insights": [], "recommendations": [], "priority": 7, "forecast": {}}

    def find_cost_savings(self, context: dict) -> dict:
        """Находит возможности оптимизации расходов."""
        try:
            return self.think_json(
                "Найди возможности оптимизации расходов агентства моделей Nevesty Models.\n"
                "Верни JSON:\n"
                '{"cost_analysis": ['
                '{"category": "маркетинг", "current_spend_rub": 30000, "optimized_spend_rub": 20000, '
                '"savings_rub": 10000, "method": "как именно оптимизировать", "risk": "низкий|средний|высокий"}], '
                '"total_potential_savings_rub": 25000, '
                '"quick_wins": [{"action": "конкретное действие", "savings_rub": 5000, "effort": "малый|средний|большой"}], '
                '"automation_savings": [{"process": "процесс", "current_cost_hrs": 10, "automation_tool": "инструмент"}], '
                '"do_not_cut": ["расходы которые нельзя резать — объяснение"], '
                '"payback_period_months": 2}',
                context=context,
                max_tokens=1500,
            ) or {}
        except Exception as e:
            logger.error("[finance/cost_optimizer] find_cost_savings error: %s", e)
            return {}


class PricingStrategist(FactoryAgent):
    department = "finance"
    role = "pricing_strategist"
    name = "pricing_strategist"
    system_prompt = """Ты — Pricing Strategist агентства моделей Nevesty Models.
Анализируешь ценообразование на рынке агентств моделей в России: Москва, Санкт-Петербург, регионы.
Знаешь типичные ставки: от 3000 руб/час для начинающих до 30000+ руб/день для топ-моделей.
Типы мероприятий: корпоративы, свадьбы, выставки, промо-акции, показы мод, фотосессии.
Анализируешь позиционирование (эконом, средний, премиум сегмент) и предлагаешь корректировку цен
для максимизации прибыли и конкурентоспособности. Всё на русском языке."""

    def run(self, context: dict) -> dict:
        """Универсальный метод запуска агента."""
        try:
            pricing = self.analyze_pricing(context)
            recs = pricing.get("pricing_recommendations", [])
            rec_texts = [f"{r.get('service')}: {r.get('recommended_price_rub')} руб." for r in recs[:2]]
            return {
                "insights": [
                    f"Сегмент: {pricing.get('market_analysis', {}).get('segment', '?')}",
                    f"Потенциальный доп. доход: {pricing.get('revenue_impact_estimate_rub', '?')} руб./мес.",
                ],
                "recommendations": rec_texts,
                "priority": 8,
                "forecast": {"revenue_impact_rub": pricing.get("revenue_impact_estimate_rub", 0)},
            }
        except Exception as e:
            logger.error("[finance/pricing_strategist] run error: %s", e)
            return {"insights": [], "recommendations": [], "priority": 8, "forecast": {}}

    def analyze_pricing(self, context: dict) -> dict:
        """Анализирует ценообразование и предлагает корректировки."""
        try:
            return self.think_json(
                "Проанализируй ценообразование агентства моделей Nevesty Models и предложи корректировки.\n"
                "Сравни с рыночными ставками конкурентов в России.\n"
                "Верни JSON:\n"
                '{"market_analysis": {'
                '"segment": "эконом|средний|премиум", '
                '"competitor_rates": [{"tier": "начинающая", "market_range_rub": "3000-7000/час", "our_rate_rub": 5000}]}, '
                '"pricing_recommendations": ['
                '{"service": "корпоратив 4 часа", "current_price_rub": 20000, "recommended_price_rub": 25000, '
                '"rationale": "почему", "expected_impact": "описание влияния на спрос"}], '
                '"pricing_strategy": "стратегия ценообразования (ценность, динамическое, пакетное)", '
                '"bundle_opportunities": [{"bundle_name": "Корпоратив Плюс", "includes": ["модель", "фотограф"], "price_rub": 45000}], '
                '"seasonal_adjustments": [{"period": "Новый год", "multiplier": 1.5, "rationale": "пиковый спрос"}], '
                '"revenue_impact_estimate_rub": 30000}',
                context=context,
                max_tokens=1500,
            ) or {}
        except Exception as e:
            logger.error("[finance/pricing_strategist] analyze_pricing error: %s", e)
            return {}


class BudgetPlanner(FactoryAgent):
    department = "finance"
    role = "budget_planner"
    name = "budget_planner"
    system_prompt = """Ты — Budget Planner агентства моделей Nevesty Models.
Планируешь бюджет агентства на маркетинг, операции, развитие на квартал вперёд.
Знаешь специфику агентства: переменные расходы зависят от количества заказов,
основные каналы продвижения — Instagram, ВКонтакте, Telegram, SEO, сарафанное радио.
Распределяешь бюджет между каналами с учётом ROI каждого, создаёшь резервы на непредвиденное.
Даёшь конкретные цифры в рублях. Всё на русском языке."""

    def run(self, context: dict) -> dict:
        """Универсальный метод запуска агента."""
        try:
            budget = self.plan_budget(context)
            mkt = budget.get("marketing_budget", {})
            ops = budget.get("operations_budget", {})
            return {
                "insights": [
                    f"Общий бюджет квартала: {budget.get('total_budget_rub', '?')} руб.",
                    f"Маркетинг: {mkt.get('total_rub', '?')} руб., операции: {ops.get('total_rub', '?')} руб.",
                ],
                "recommendations": [
                    f"Резерв: {budget.get('reserve_rub', '?')} руб. на непредвиденное",
                ] + [a for a in budget.get("assumptions", [])[:2]],
                "priority": 6,
                "forecast": {
                    "total_budget_rub": budget.get("total_budget_rub", 0),
                    "marketing_rub": mkt.get("total_rub", 0),
                    "operations_rub": ops.get("total_rub", 0),
                    "reserve_rub": budget.get("reserve_rub", 0),
                },
            }
        except Exception as e:
            logger.error("[finance/budget_planner] run error: %s", e)
            return {"insights": [], "recommendations": [], "priority": 6, "forecast": {}}

    def plan_budget(self, context: dict) -> dict:
        """Планирует бюджет на маркетинг и операции."""
        try:
            return self.think_json(
                "Составь бюджетный план для агентства моделей Nevesty Models на следующий квартал.\n"
                "Верни JSON:\n"
                '{"total_budget_rub": 200000, '
                '"marketing_budget": {'
                '"total_rub": 80000, '
                '"channels": [{"channel": "Instagram", "budget_rub": 25000, "expected_roi": 3.0, "kpi": "50 новых заявок"}]}, '
                '"operations_budget": {'
                '"total_rub": 70000, '
                '"items": [{"item": "хостинг и сервисы", "monthly_rub": 5000, "quarterly_rub": 15000}]}, '
                '"development_budget": {"total_rub": 30000, "items": ["улучшение бота", "фотосессии моделей"]}, '
                '"reserve_rub": 20000, '
                '"monthly_cashflow": [{"month": "Январь", "income_rub": 120000, "expenses_rub": 65000, "net_rub": 55000}], '
                '"budget_alerts": [{"threshold_rub": 15000, "category": "маркетинг", "action": "пересмотреть каналы"}], '
                '"assumptions": ["допущение 1", "допущение 2"]}',
                context=context,
                max_tokens=1800,
            ) or {}
        except Exception as e:
            logger.error("[finance/budget_planner] plan_budget error: %s", e)
            return {}


class FinanceDepartment:
    """Координатор финансового департамента."""

    def __init__(self):
        self.forecaster = RevenueForecaster()
        self.optimizer = CostOptimizer()
        self.pricing = PricingStrategist()
        self.planner = BudgetPlanner()

    def execute_task(self, task: str, context: dict) -> dict:
        """Диспетчер по ключевым словам задачи."""
        task_lower = task.lower()
        result_data = {}
        roles_used = []

        try:
            if any(kw in task_lower for kw in ("прогноз", "выручк", "revenue", "forecast", "доход")):
                result_data["revenue_forecast"] = self.forecaster.forecast_revenue(context)
                roles_used.append("revenue_forecaster")
        except Exception as e:
            logger.error("[FinanceDept] forecaster task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("расход", "затрат", "экономи", "cost", "оптимиз")):
                result_data["cost_savings"] = self.optimizer.find_cost_savings(context)
                roles_used.append("cost_optimizer")
        except Exception as e:
            logger.error("[FinanceDept] optimizer task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("цен", "прайс", "pricing", "тариф", "конкурент")):
                result_data["pricing"] = self.pricing.analyze_pricing(context)
                roles_used.append("pricing_strategist")
        except Exception as e:
            logger.error("[FinanceDept] pricing task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("бюджет", "план", "budget", "квартал", "распределен")) \
                    or not roles_used:
                result_data["budget_plan"] = self.planner.plan_budget(context)
                roles_used.append("budget_planner")
        except Exception as e:
            logger.error("[FinanceDept] planner task error: %s", e)

        output = {
            "department": "finance",
            "task": task,
            "result": result_data,
            "roles_used": roles_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info("[FinanceDept] Задача '%s' выполнена. Ролей задействовано: %d", task, len(roles_used))
        return output
