"""💼 Sales Department — квалификация лидов, коммерческие предложения, follow-up, ценообразование."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent

logger = logging.getLogger(__name__)


class LeadQualifier(FactoryAgent):
    department = "sales"
    role = "lead_qualifier"
    name = "lead_qualifier"
    system_prompt = """Ты — Lead Qualifier в агентстве моделей Nevesty Models.
Твоя задача: анализировать входящие заявки и клиентские данные.
Определяй приоритет клиента (высокий/средний/низкий) на основе бюджета, типа события, срочности.
Предлагай конкретные действия для конвертации каждого лида.
Всё на русском языке."""

    def qualify(self, orders_data: dict) -> dict:
        return self.think_json(
            f"Проанализируй заявки и оцени приоритет клиентов: {orders_data}"
        )

    def analyze_leads(self, context: dict) -> dict:
        """Квалифицирует входящие заявки и расставляет приоритеты."""
        try:
            return self.think_json(
                "Проанализируй входящие заявки агентства моделей и расставь приоритеты.\n"
                "Верни JSON:\n"
                '{"leads": ['
                '{"segment": "VIP|стандарт|бюджет", "priority": "высокий|средний|низкий", '
                '"budget_estimate": "...", "event_type": "корпоратив|свадьба|фотосессия|другое", '
                '"urgency": "срочно|стандарт|гибко", "next_action": "позвонить|написать|отложить", '
                '"conversion_probability": "высокая|средняя|низкая"}], '
                '"hot_leads_count": 3, '
                '"recommended_daily_focus": "на чём сфокусироваться сегодня", '
                '"qualification_notes": "общие наблюдения по качеству входящих лидов"}',
                context=context,
                max_tokens=1500,
            ) or {}
        except Exception as e:
            logger.error("[sales/lead_qualifier] analyze_leads error: %s", e)
            return {}

    def run(self, context: dict) -> dict:
        """Универсальный метод запуска агента. Возвращает insights, recommendations, priority."""
        try:
            result = self.think_json(
                "Ты — Lead Qualifier агентства моделей. Проанализируй контекст бизнеса.\n"
                "Верни JSON:\n"
                '{"insights": ["инсайт 1 о качестве лидов", "инсайт 2 о конверсии"], '
                '"recommendations": ["рекомендация 1 по работе с лидами", "рекомендация 2"], '
                '"priority": 8}',
                context=context,
                max_tokens=1000,
            ) or {}
            return {
                "insights": result.get("insights", []),
                "recommendations": result.get("recommendations", []),
                "priority": result.get("priority", 7),
            }
        except Exception as e:
            logger.error("[sales/lead_qualifier] run error: %s", e)
            return {"insights": [], "recommendations": [], "priority": 7}


class ProposalWriter(FactoryAgent):
    department = "sales"
    role = "proposal_writer"
    name = "proposal_writer"
    system_prompt = """Ты — Proposal Writer в агентстве моделей Nevesty Models.
Генерируешь персонализированные коммерческие предложения для потенциальных клиентов.
Учитываешь тип мероприятия, бюджет, пожелания клиента.
Пишешь убедительно, конкретно, с акцентом на ценность услуги.
Всё на русском языке."""

    def generate_proposal(self, context: dict) -> dict:
        """Генерирует структуру коммерческого предложения."""
        try:
            return self.think_json(
                "Создай шаблон коммерческого предложения для потенциального клиента агентства моделей.\n"
                "Верни JSON:\n"
                '{"proposal_title": "заголовок КП", '
                '"opening_hook": "цепляющее первое предложение", '
                '"value_proposition": "почему Nevesty Models — лучший выбор", '
                '"packages": ['
                '{"name": "название пакета", "price_range": "от X до Y руб", '
                '"includes": ["что входит"], "best_for": "для кого подходит"}], '
                '"social_proof": "отзыв или статистика", '
                '"call_to_action": "что предлагаем сделать клиенту", '
                '"urgency_trigger": "причина решить сейчас", '
                '"follow_up_timing": "когда перезвонить если нет ответа"}',
                context=context,
                max_tokens=1500,
            ) or {}
        except Exception as e:
            logger.error("[sales/proposal_writer] generate_proposal error: %s", e)
            return {}

    def run(self, context: dict) -> dict:
        """Универсальный метод запуска агента. Возвращает insights, recommendations, priority."""
        try:
            result = self.think_json(
                "Ты — Proposal Writer агентства моделей. Проанализируй контекст бизнеса.\n"
                "Верни JSON:\n"
                '{"insights": ["инсайт 1 о коммерческих предложениях", "инсайт 2 о конверсии КП"], '
                '"recommendations": ["рекомендация 1 по улучшению КП", "рекомендация 2 по сегментации"], '
                '"priority": 7}',
                context=context,
                max_tokens=1000,
            ) or {}
            return {
                "insights": result.get("insights", []),
                "recommendations": result.get("recommendations", []),
                "priority": result.get("priority", 7),
            }
        except Exception as e:
            logger.error("[sales/proposal_writer] run error: %s", e)
            return {"insights": [], "recommendations": [], "priority": 7}


class FollowUpSpecialist(FactoryAgent):
    department = "sales"
    role = "followup"
    name = "followup_specialist"
    system_prompt = """Ты — Follow-Up Specialist в агентстве моделей Nevesty Models.
Отслеживаешь незакрытые заявки и предлагаешь конкретные действия для их закрытия.
Умеешь деликатно напоминать о себе, не раздражая клиента.
Знаешь, когда нужно дожимать, а когда отпустить.
Всё на русском языке."""

    def plan_followups(self, context: dict) -> dict:
        """Планирует follow-up действия по незакрытым заявкам."""
        try:
            return self.think_json(
                "Составь план follow-up действий для незакрытых заявок агентства моделей.\n"
                "Верни JSON:\n"
                '{"followup_actions": ['
                '{"days_since_contact": "X дней без ответа", '
                '"channel": "telegram|whatsapp|звонок|email", '
                '"message_template": "шаблон сообщения", '
                '"tone": "дружелюбный|деловой|срочный", '
                '"expected_response_rate": "высокая|средняя|низкая"}], '
                '"lost_lead_criteria": "когда считать лид потерянным", '
                '"reactivation_script": "как попробовать вернуть холодный лид", '
                '"weekly_followup_goal": "цель по follow-up контактам в неделю"}',
                context=context,
                max_tokens=1200,
            ) or {}
        except Exception as e:
            logger.error("[sales/followup] plan_followups error: %s", e)
            return {}

    def run(self, context: dict) -> dict:
        """Универсальный метод запуска агента. Возвращает insights, recommendations, priority."""
        try:
            result = self.think_json(
                "Ты — Follow-Up Specialist агентства моделей. Проанализируй контекст бизнеса.\n"
                "Верни JSON:\n"
                '{"insights": ["инсайт 1 о незакрытых заявках", "инсайт 2 о причинах потери лидов"], '
                '"recommendations": ["рекомендация 1 по follow-up стратегии", "рекомендация 2 по реактивации"], '
                '"priority": 6}',
                context=context,
                max_tokens=1000,
            ) or {}
            return {
                "insights": result.get("insights", []),
                "recommendations": result.get("recommendations", []),
                "priority": result.get("priority", 6),
            }
        except Exception as e:
            logger.error("[sales/followup] run error: %s", e)
            return {"insights": [], "recommendations": [], "priority": 6}


class PricingNegotiator(FactoryAgent):
    department = "sales"
    role = "pricing"
    name = "pricing_negotiator"
    system_prompt = """Ты — Pricing Negotiator в агентстве моделей Nevesty Models.
Анализируешь ценообразование агентства и предлагаешь гибкие условия для разных сегментов.
Знаешь, как защитить маржу и при этом не упустить клиента.
Предлагаешь пакеты, скидки, бонусы которые работают.
Всё на русском языке."""

    def optimize_pricing(self, context: dict) -> dict:
        """Анализирует ценообразование и предлагает гибкие условия."""
        try:
            return self.think_json(
                "Проанализируй стратегию ценообразования агентства моделей и предложи улучшения.\n"
                "Верни JSON:\n"
                '{"current_pricing_assessment": "оценка текущих цен", '
                '"price_anchoring": "как правильно презентовать цены", '
                '"discount_strategy": {'
                '"max_discount_pct": 15, '
                '"conditions": ["при каких условиях давать скидку"], '
                '"alternatives_to_discount": ["что предложить вместо скидки"]}, '
                '"package_recommendations": ['
                '{"name": "...", "price": "...", "margin": "высокая|средняя|низкая", '
                '"positioning": "для кого"}], '
                '"seasonal_pricing": "рекомендации по сезонным ценам", '
                '"negotiation_scripts": ['
                '{"objection": "дорого", "response": "скрипт ответа"}]}',
                context=context,
                max_tokens=1500,
            ) or {}
        except Exception as e:
            logger.error("[sales/pricing] optimize_pricing error: %s", e)
            return {}

    def run(self, context: dict) -> dict:
        """Универсальный метод запуска агента. Возвращает insights, recommendations, priority."""
        try:
            result = self.think_json(
                "Ты — Pricing Negotiator агентства моделей. Проанализируй контекст бизнеса.\n"
                "Верни JSON:\n"
                '{"insights": ["инсайт 1 о текущем ценообразовании", "инсайт 2 о маржинальности"], '
                '"recommendations": ["рекомендация 1 по ценовой политике", "рекомендация 2 по пакетам"], '
                '"priority": 8}',
                context=context,
                max_tokens=1000,
            ) or {}
            return {
                "insights": result.get("insights", []),
                "recommendations": result.get("recommendations", []),
                "priority": result.get("priority", 8),
            }
        except Exception as e:
            logger.error("[sales/pricing] run error: %s", e)
            return {"insights": [], "recommendations": [], "priority": 8}


class SalesDepartment:
    """Координатор отдела продаж."""

    def __init__(self):
        self.qualifier = LeadQualifier()
        self.proposal = ProposalWriter()
        self.followup = FollowUpSpecialist()
        self.pricing = PricingNegotiator()

    def execute_task(self, task: str, context: dict) -> dict:
        """Диспетчер по ключевым словам задачи."""
        task_lower = task.lower()
        result_data = {}
        roles_used = []

        try:
            if any(kw in task_lower for kw in ("лид", "lead", "заявк", "квалиф", "qualify", "приоритет")):
                result_data["leads"] = self.qualifier.analyze_leads(context)
                roles_used.append("lead_qualifier")
        except Exception as e:
            logger.error("[SalesDept] lead qualifier error: %s", e)

        try:
            if any(kw in task_lower for kw in ("предложен", "proposal", "кп", "коммерч", "оффер", "offer")):
                result_data["proposal"] = self.proposal.generate_proposal(context)
                roles_used.append("proposal_writer")
        except Exception as e:
            logger.error("[SalesDept] proposal error: %s", e)

        try:
            if any(kw in task_lower for kw in ("follow", "напомин", "незакрыт", "followup", "дожим")):
                result_data["followup"] = self.followup.plan_followups(context)
                roles_used.append("followup")
        except Exception as e:
            logger.error("[SalesDept] followup error: %s", e)

        try:
            if any(kw in task_lower for kw in ("цен", "price", "pricing", "скидк", "discount", "пакет", "переговор")) \
                    or not roles_used:
                result_data["pricing"] = self.pricing.optimize_pricing(context)
                roles_used.append("pricing")
        except Exception as e:
            logger.error("[SalesDept] pricing error: %s", e)

        output = {
            "department": "sales",
            "task": task,
            "result": result_data,
            "roles_used": roles_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info("[SalesDept] Задача '%s' выполнена. Ролей задействовано: %d", task, len(roles_used))
        return output
