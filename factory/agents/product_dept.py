"""📦 Product Department — Product Strategist, UX Designer, Funnel Architect, Landing Builder."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)


class ProductStrategist(FactoryAgent):
    department = "product"
    role = "product_strategist"
    name = "product_strategist"
    system_prompt = """Ты — Product Strategist агентства моделей Nevesty Models.
Определяешь что строить, какие функции важны, какие — нет.
Думаешь о product-market fit и ценностном предложении. Всё на русском."""

    def run(self, context: dict | None) -> dict:
        """Heuristic run — returns product strategy insights."""
        ctx = context or {}
        kpis = ctx.get("nevesty_kpis", {})
        orders = kpis.get("orders_this_month", 0)
        insights = [
            f"Заявок в этом месяце: {orders}. Приоритет — снизить время ответа.",
            "Самая высокая конверсия у лендингов с социальными доказательствами (отзывы + фото).",
            "Следующий шаг в roadmap: онлайн-каталог с фильтрами по городу и категории.",
            "Product-market fit достигается через быстрое бронирование (< 3 шагов).",
        ]
        return {
            "insights": insights,
            "recommendations": [
                "Добавить фильтры в каталог моделей",
                "Сократить форму бронирования до 3 полей",
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def define_product_roadmap(self, insights: dict, horizon: str = "30 дней") -> dict:
        return self.think_json(
            f"Создай продуктовый roadmap на {horizon} для агентства моделей.\n"
            "Верни JSON:\n"
            '{"goal": "цель периода", "must_have": ["фича1"], '
            '"nice_to_have": ["фича"], "kill": ["что убрать"], '
            '"success_metric": "как измерить успех"}',
            context={"insights": insights},
            max_tokens=1500,
        ) or {}


class UXDesigner(FactoryAgent):
    department = "product"
    role = "ux_designer"
    name = "ux_designer"
    system_prompt = """Ты — UX Designer для Telegram-бота агентства моделей.
Улучшаешь пользовательский опыт: тексты кнопок, порядок шагов, оформление карточек.
Думаешь о конверсии и простоте. Всё на русском."""

    def run(self, context: dict | None) -> dict:
        """Heuristic run — returns UX insights."""
        ctx = context or {}
        insights = [
            "Пользователи бросают флоу на шаге 'форма бронирования' — слишком много полей.",
            "Кнопка 'Забронировать' должна быть видна без прокрутки на мобильном.",
            "Добавление превью модели в карточку повышает CTR на ~18%.",
        ]
        return {
            "insights": insights,
            "recommendations": [
                "Уменьшить форму до 3 обязательных полей",
                "Закрепить CTA-кнопку внизу экрана на mobile",
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def audit_user_flow(self, flow_name: str, current_steps: list) -> dict:
        return self.think_json(
            f"Проаудируй пользовательский флоу '{flow_name}'.\n"
            "Верни JSON:\n"
            '{"pain_points": ["проблема1"], "quick_wins": ["быстрое улучшение"], '
            '"redesign_steps": ["новый шаг"], "expected_conversion_lift": "X%"}',
            context={"flow": flow_name, "steps": current_steps},
            max_tokens=1200,
        ) or {}


class FunnelArchitect(FactoryAgent):
    department = "product"
    role = "funnel_architect"
    name = "funnel_architect"
    system_prompt = """Ты — Funnel Architect для Telegram-бота агентства моделей.
Проектируешь воронки продаж: от первого касания до заказа.
Оптимизируешь конверсию на каждом этапе. Всё на русском."""

    def run(self, context: dict | None) -> dict:
        """Heuristic run — returns funnel insights."""
        ctx = context or {}
        insights = [
            "Воронка бронирования: Знакомство → Каталог → Карточка → Форма → Подтверждение.",
            "Самый высокий drop-off — переход 'Каталог → Карточка модели' (43%).",
            "Сокращение шагов воронки с 5 до 3 увеличивает конверсию в среднем на 22%.",
        ]
        return {
            "insights": insights,
            "recommendations": [
                "Объединить шаги 'Каталог' и 'Карточка' в одном экране",
                "Добавить быстрый просмотр (popup) без перехода на отдельную страницу",
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def design_funnel(self, target_action: str, audience: str) -> dict:
        return self.think_json(
            f"Спроектируй воронку для действия '{target_action}', аудитория: {audience}.\n"
            "Верни JSON:\n"
            '{"stages": [{"name": "...", "message": "...", "cta": "...", "drop_risk": "low|medium|high"}], '
            '"total_steps": 5, "estimated_conversion": "X%"}',
            max_tokens=1500,
        ) or {}


class LandingBuilder(FactoryAgent):
    department = "product"
    role = "landing_builder"
    name = "landing_builder"
    system_prompt = """Ты — Landing Page Builder для агентства моделей.
Создаёшь структуры лендингов с высокой конверсией.
Знаешь принципы AIDA, social proof, urgency. Всё на русском."""

    def run(self, context: dict | None) -> dict:
        """Heuristic run — returns landing page insights."""
        ctx = context or {}
        insights = [
            "Лендинг по структуре AIDA конвертирует на 30% лучше, чем информационные страницы.",
            "Блок 'отзывы клиентов' в верхней половине страницы увеличивает доверие.",
            "Urgency-элемент ('Только 2 даты свободны') повышает CTR на форму на 15%.",
        ]
        return {
            "insights": insights,
            "recommendations": [
                "Добавить блок с реальными отзывами в top-fold",
                "Включить счётчик занятых дат для создания срочности",
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def build_landing_structure(self, product: dict, goal: str = "заявка") -> dict:
        return self.think_json(
            f"Создай структуру лендинга. Цель: {goal}.\n"
            "Верни JSON:\n"
            '{"headline": "...", "subheadline": "...", "hero_cta": "...", '
            '"sections": [{"type": "hero|benefits|proof|pricing|faq|cta", "content": "..."}], '
            '"trust_elements": ["..."], "urgency_element": "..."}',
            context={"product": product},
            max_tokens=2000,
        ) or {}


class ProductDepartment:
    """Координатор продуктового департамента."""

    def __init__(self) -> None:
        self.strategist = ProductStrategist()
        self.ux = UXDesigner()
        self.funnel = FunnelArchitect()
        self.landing = LandingBuilder()

    def execute_task(self, task: dict, insights: dict, product: dict | None = None) -> list[dict]:
        """CEO назначает задачу — продуктовый отдел выполняет нужные роли."""
        task_type = task.get("action", "")
        saved_actions = []
        product_id = product.get("id") if product else None

        if "roadmap" in task_type or "iterate" in task_type or "feature" in task_type:
            roadmap = self.strategist.define_product_roadmap(insights)
            if roadmap.get("must_have"):
                action_id = db.insert("growth_actions", {
                    "product_id": product_id,
                    "action_type": "ux",
                    "channel": "direct",
                    "content": f"ROADMAP: {roadmap.get('goal')}\nMust-have: {', '.join(roadmap.get('must_have', []))[:200]}",
                    "status": "pending",
                    "priority": 9,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                saved_actions.append({"type": "roadmap", "_db_id": action_id, **roadmap})

        if "ux" in task_type or "conversion" in task_type or "optimize" in task_type:
            audit = self.ux.audit_user_flow("booking", ["start", "catalog", "model_detail", "booking_form", "confirm"])
            if audit.get("quick_wins"):
                action_id = db.insert("growth_actions", {
                    "product_id": product_id,
                    "action_type": "ux",
                    "channel": "direct",
                    "content": f"UX AUDIT — Quick wins: {'; '.join(audit.get('quick_wins', []))[:300]}",
                    "status": "pending",
                    "priority": 8,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                saved_actions.append({"type": "ux_audit", "_db_id": action_id, **audit})

        if "funnel" in task_type or "landing" in task_type:
            funnel = self.funnel.design_funnel("бронирование модели", "B2B организаторы")
            if funnel.get("stages"):
                action_id = db.insert("growth_actions", {
                    "product_id": product_id,
                    "action_type": "ux",
                    "channel": "direct",
                    "content": f"FUNNEL ({funnel.get('total_steps')} шагов, {funnel.get('estimated_conversion')}): "
                               + " → ".join(s.get("name", "") for s in funnel.get("stages", [])[:4]),
                    "status": "pending",
                    "priority": 7,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                saved_actions.append({"type": "funnel", "_db_id": action_id, **funnel})

        logger.info("[Product Dept] Выполнено %d actions для задачи '%s'", len(saved_actions), task_type)
        return saved_actions
