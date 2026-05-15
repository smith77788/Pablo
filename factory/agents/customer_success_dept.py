"""🤝 Customer Success Department — онбординг, удержание, обратная связь, апселл."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent

logger = logging.getLogger(__name__)


class OnboardingSpecialist(FactoryAgent):
    department = "customer_success"
    role = "onboarding"
    name = "onboarding_specialist"
    system_prompt = """Ты — Onboarding Specialist агентства моделей Nevesty Models.
Улучшаешь процесс первого заказа клиента: от первого контакта до успешно проведённого мероприятия.
Снижаешь трение на каждом этапе, делаешь опыт клиента максимально комфортным.
Предлагаешь конкретные улучшения и скрипты для менеджеров.
Всё на русском языке."""

    def improve_onboarding(self, context: dict) -> dict:
        """Анализирует и улучшает процесс первого заказа."""
        try:
            return self.think_json(
                "Проанализируй процесс первого заказа в агентстве моделей и предложи улучшения.\n"
                "Верни JSON:\n"
                '{"onboarding_stages": ['
                '{"stage": "название этапа", '
                '"current_experience": "текущий опыт клиента", '
                '"pain_points": ["боль клиента"], '
                '"improvements": ["конкретное улучшение"], '
                '"success_metric": "как измерить улучшение"}], '
                '"welcome_sequence": ['
                '{"step": 1, "trigger": "когда отправить", '
                '"channel": "telegram|email|звонок", '
                '"message": "текст сообщения", '
                '"goal": "цель этого шага"}], '
                '"first_order_checklist": ["чек-пункт для клиента"], '
                '"onboarding_success_rate_target": "целевой показатель успешного онбординга в %", '
                '"quick_wins": ["быстрое улучшение которое можно внедрить сегодня"]}',
                context=context,
                max_tokens=1800,
            ) or {}
        except Exception as e:
            logger.error("[customer_success/onboarding] improve_onboarding error: %s", e)
            return {}


class RetentionAnalyst(FactoryAgent):
    department = "customer_success"
    role = "retention"
    name = "retention_analyst"
    system_prompt = """Ты — Retention Analyst агентства моделей Nevesty Models.
Анализируешь почему клиенты не возвращаются после первого заказа.
Находишь паттерны оттока и предлагаешь стратегии удержания.
Работаешь с данными: повторные заказы, интервалы между заказами, причины отказов.
Всё на русском языке."""

    def analyze_churn(self, context: dict) -> dict:
        """Анализирует отток клиентов и предлагает стратегии удержания."""
        try:
            return self.think_json(
                "Проанализируй отток клиентов агентства моделей и предложи стратегии удержания.\n"
                "Верни JSON:\n"
                '{"churn_analysis": {'
                '"estimated_churn_rate": "примерный % оттока", '
                '"churn_reasons": ['
                '{"reason": "причина оттока", "frequency": "частая|редкая|единичная", '
                '"preventable": true, "prevention_tactic": "как предотвратить"}], '
                '"at_risk_signals": ["сигнал что клиент может уйти"], '
                '"optimal_reorder_window": "через сколько дней ожидать следующий заказ"}, '
                '"retention_strategies": ['
                '{"strategy": "название стратегии", '
                '"target_segment": "для кого", '
                '"implementation": "как внедрить", '
                '"expected_impact": "ожидаемый эффект", '
                '"cost": "бесплатно|малые затраты|инвестиция"}], '
                '"loyalty_program_idea": "идея программы лояльности для агентства", '
                '"30_day_retention_plan": ["действие 1", "действие 2", "действие 3"]}',
                context=context,
                max_tokens=1800,
            ) or {}
        except Exception as e:
            logger.error("[customer_success/retention] analyze_churn error: %s", e)
            return {}


class FeedbackCollector(FactoryAgent):
    department = "customer_success"
    role = "feedback"
    name = "feedback_collector"
    system_prompt = """Ты — Feedback Collector агентства моделей Nevesty Models.
Анализируешь отзывы клиентов, выявляешь паттерны и системные проблемы.
Превращаешь неструктурированную обратную связь в конкретные улучшения.
Помогаешь сформулировать правильные вопросы для сбора обратной связи.
Всё на русском языке."""

    def analyze_feedback(self, context: dict) -> dict:
        """Анализирует отзывы и выявляет паттерны."""
        try:
            return self.think_json(
                "Проанализируй обратную связь клиентов агентства моделей и выяви паттерны.\n"
                "Верни JSON:\n"
                '{"sentiment_overview": {'
                '"positive_themes": ["позитивная тема 1", "позитивная тема 2"], '
                '"negative_themes": ["негативная тема 1", "негативная тема 2"], '
                '"neutral_themes": ["нейтральная тема"]}, '
                '"critical_issues": ['
                '{"issue": "системная проблема", "frequency": "как часто встречается", '
                '"impact": "высокий|средний|низкий", "fix_priority": 1}], '
                '"nps_estimate": "примерный NPS на основе отзывов", '
                '"feedback_collection_improvements": ['
                '{"channel": "telegram|форма|звонок", '
                '"question": "вопрос для сбора обратной связи", '
                '"timing": "когда задавать"}], '
                '"action_items": ['
                '{"action": "конкретное действие", "owner": "менеджер|бот|директор", '
                '"deadline": "срочно|на неделе|в следующем месяце"}]}',
                context=context,
                max_tokens=1800,
            ) or {}
        except Exception as e:
            logger.error("[customer_success/feedback] analyze_feedback error: %s", e)
            return {}


class UpsellAdvisor(FactoryAgent):
    department = "customer_success"
    role = "upsell"
    name = "upsell_advisor"
    system_prompt = """Ты — Upsell Advisor агентства моделей Nevesty Models.
Предлагаешь апселл и кросс-селл стратегии: более дорогие модели, расширенные пакеты, дополнительные услуги.
Делаешь это органично, фокусируясь на ценности для клиента, а не на продаже ради продажи.
Знаешь когда и как предложить апселл чтобы клиент сказал «да».
Всё на русском языке."""

    def suggest_upsell(self, context: dict) -> dict:
        """Предлагает стратегии апселла и кросс-селла."""
        try:
            return self.think_json(
                "Разработай стратегии апселла и кросс-селла для агентства моделей.\n"
                "Верни JSON:\n"
                '{"upsell_opportunities": ['
                '{"trigger": "когда предлагать", '
                '"current_offer": "что клиент выбрал", '
                '"upsell_offer": "что предложить вместо", '
                '"value_argument": "почему это лучше для клиента", '
                '"price_difference": "разница в цене", '
                '"conversion_script": "скрипт предложения апселла", '
                '"success_probability": "высокая|средняя|низкая"}], '
                '"cross_sell_options": ['
                '{"base_service": "основная услуга", '
                '"addon": "дополнительная услуга", '
                '"pitch": "как предложить"}], '
                '"upsell_timing_best_practices": ["когда апселл работает лучше всего"], '
                '"revenue_impact_estimate": "ожидаемый рост выручки от апселла в %", '
                '"forbidden_upsell_situations": ["когда апселл делать нельзя"]}',
                context=context,
                max_tokens=1800,
            ) or {}
        except Exception as e:
            logger.error("[customer_success/upsell] suggest_upsell error: %s", e)
            return {}


class CustomerSuccessDepartment:
    """Координатор департамента работы с клиентами."""

    def __init__(self):
        self.onboarding = OnboardingSpecialist()
        self.retention = RetentionAnalyst()
        self.feedback = FeedbackCollector()
        self.upsell = UpsellAdvisor()

    def execute_task(self, task: str, context: dict) -> dict:
        """Диспетчер по ключевым словам задачи."""
        task_lower = task.lower()
        result_data = {}
        roles_used = []

        try:
            if any(kw in task_lower for kw in ("онборд", "onboard", "первый заказ", "welcome", "новый клиент")):
                result_data["onboarding"] = self.onboarding.improve_onboarding(context)
                roles_used.append("onboarding")
        except Exception as e:
            logger.error("[CustomerSuccessDept] onboarding error: %s", e)

        try:
            if any(kw in task_lower for kw in ("retention", "удержан", "отток", "churn", "возврат", "повтор")):
                result_data["retention"] = self.retention.analyze_churn(context)
                roles_used.append("retention")
        except Exception as e:
            logger.error("[CustomerSuccessDept] retention error: %s", e)

        try:
            if any(kw in task_lower for kw in ("отзыв", "feedback", "nps", "опрос", "обратн", "survey")):
                result_data["feedback"] = self.feedback.analyze_feedback(context)
                roles_used.append("feedback")
        except Exception as e:
            logger.error("[CustomerSuccessDept] feedback error: %s", e)

        try:
            if any(kw in task_lower for kw in ("апселл", "upsell", "cross", "кросс", "допродаж", "premium")) \
                    or not roles_used:
                result_data["upsell"] = self.upsell.suggest_upsell(context)
                roles_used.append("upsell")
        except Exception as e:
            logger.error("[CustomerSuccessDept] upsell error: %s", e)

        output = {
            "department": "customer_success",
            "task": task,
            "result": result_data,
            "roles_used": roles_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info("[CustomerSuccessDept] Задача '%s' выполнена. Ролей задействовано: %d", task, len(roles_used))
        return output
