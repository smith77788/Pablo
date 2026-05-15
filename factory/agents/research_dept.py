"""🔬 Research Department — исследование рынка, анализ конкурентов, тренды, инсайты для Nevesty Models."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)


class MarketResearcher(FactoryAgent):
    department = "research"
    role = "market_researcher"
    name = "market_researcher"
    system_prompt = """Ты — Market Researcher агентства моделей Nevesty Models.
Исследуешь рынок агентств моделей в России: объём рынка, динамика роста, ключевые сегменты клиентов.
Знаешь, что основные заказчики — event-агентства, корпорации (корпоративы), свадебные организаторы,
фотографы и бренды для промо-акций. Анализируешь географию: Москва, СПб, города-миллионники.
Выявляешь незакрытые потребности клиентов и рыночные ниши. Всё на русском языке."""

    def research_market(self, context: dict) -> dict:
        """Исследует рынок агентств моделей в России."""
        try:
            return self.think_json(
                "Проведи исследование рынка агентств моделей в России применительно к Nevesty Models.\n"
                "Верни JSON:\n"
                '{"market_size": {'
                '"total_rub_bln": 12.5, '
                '"growth_rate_pct": 8, '
                '"online_share_pct": 35}, '
                '"key_segments": ['
                '{"segment": "Корпоративные мероприятия", "market_share_pct": 45, '
                '"avg_check_rub": 50000, "frequency": "ежеквартально", "pain_points": ["длительный поиск", "несоответствие портфолио"]}], '
                '"geographic_analysis": ['
                '{"city": "Москва", "demand_level": "высокий", "competition_level": "высокий", "opportunity": "описание"}], '
                '"unmet_needs": ["потребность которую рынок не закрывает"], '
                '"market_trends": ["онлайн-бронирование растёт", "спрос на инфлюенсеров растёт"], '
                '"target_audience_profile": {'
                '"primary": "event-менеджер 28-45 лет", '
                '"decision_driver": "репутация агентства и скорость подбора", '
                '"avg_budget_rub": 40000}, '
                '"market_opportunities": [{"opportunity": "B2B подписка", "potential_rub": 500000, "difficulty": "средняя"}]}',
                context=context,
                max_tokens=1800,
            ) or {}
        except Exception as e:
            logger.error("[research/market_researcher] research_market error: %s", e)
            return {}


class CompetitorAnalyst(FactoryAgent):
    department = "research"
    role = "competitor_analyst"
    name = "competitor_analyst"
    system_prompt = """Ты — Competitor Analyst агентства моделей Nevesty Models.
Анализируешь конкурентов на рынке агентств моделей в России.
Знаешь крупные агентства: Avant Models, Best Models, Icon Models, L'Agence, региональные игроки.
Оцениваешь их сильные и слабые стороны: портфолио, цены, скорость работы, клиентский сервис,
онлайн-присутствие, уникальные предложения. Находишь конкурентные преимущества для Nevesty Models.
Всё на русском языке."""

    def analyze_competitors(self, context: dict) -> dict:
        """Анализирует конкурентов и их позиции на рынке."""
        try:
            return self.think_json(
                "Проведи анализ конкурентов агентства моделей Nevesty Models на российском рынке.\n"
                "Верни JSON:\n"
                '{"competitors": ['
                '{"name": "Avant Models", '
                '"strengths": ["большое портфолио", "известный бренд"], '
                '"weaknesses": ["высокие цены", "медленный отклик"], '
                '"pricing": "выше рынка", '
                '"target_segment": "премиум", '
                '"online_presence": "сильное", '
                '"unique_selling_point": "топовые модели из подиумных агентств"}], '
                '"competitive_landscape": "описание конкурентной среды", '
                '"nevesty_advantages": ["наше преимущество 1 — быстрое онлайн-бронирование"], '
                '"nevesty_gaps": ["наш недостаток который надо закрыть"], '
                '"differentiation_opportunities": ['
                '{"opportunity": "Telegram-бот для мгновенного бронирования", '
                '"competitors_doing_this": false, '
                '"implementation_effort": "средний", '
                '"competitive_impact": "высокий"}], '
                '"recommended_positioning": "как позиционировать Nevesty против конкурентов"}',
                context=context,
                max_tokens=1800,
            ) or {}
        except Exception as e:
            logger.error("[research/competitor_analyst] analyze_competitors error: %s", e)
            return {}


class TrendSpotter(FactoryAgent):
    department = "research"
    role = "trend_spotter"
    name = "trend_spotter"
    system_prompt = """Ты — Trend Spotter агентства моделей Nevesty Models.
Отслеживаешь актуальные тренды в fashion-индустрии, event-бизнесе и digital-маркетинге.
Следишь за: новыми форматами мероприятий (иммерсивные шоу, онлайн-ивенты), трендами в подборе
моделей (diversity, инклюзивность, нестандартные типажи), изменениями в Instagram и TikTok алгоритмах,
ростом influencer-маркетинга, трендами на корпоративах в России (2024-2025).
Переводишь тренды в конкретные рекомендации для бизнеса. Всё на русском языке."""

    def spot_trends(self, context: dict) -> dict:
        """Отслеживает тренды в fashion и event индустрии."""
        try:
            return self.think_json(
                "Определи актуальные тренды в fashion и event индустрии, релевантные для агентства моделей Nevesty Models.\n"
                "Верни JSON:\n"
                '{"fashion_trends": ['
                '{"trend": "Модели с нестандартными типажами", '
                '"relevance": "высокая", '
                '"action_for_nevesty": "расширить пул моделей", '
                '"timeframe": "уже сейчас"}], '
                '"event_trends": ['
                '{"trend": "Иммерсивные корпоративы", '
                '"growth_rate": "рост 40% в год", '
                '"opportunity": "предложить тематические образы моделей", '
                '"audience": "крупные корпорации"}], '
                '"digital_trends": ['
                '{"platform": "Telegram", '
                '"trend": "боты для B2B сервисов", '
                '"impact": "высокий", '
                '"recommendation": "развивать Telegram-бот как основной канал"}], '
                '"seasonal_opportunities": ['
                '{"period": "Декабрь-Январь", '
                '"event_type": "корпоративы", '
                '"demand_multiplier": 2.5, '
                '"preparation": "что подготовить заранее"}], '
                '"emerging_opportunities": ["Бренды ищут модель с аудиторией 5к-50к — nano-influencer"], '
                '"trends_to_avoid": ["тренд который уходит и не стоит вкладывать"]}',
                context=context,
                max_tokens=1800,
            ) or {}
        except Exception as e:
            logger.error("[research/trend_spotter] spot_trends error: %s", e)
            return {}


class InsightSynthesizer(FactoryAgent):
    department = "research"
    role = "insight_synthesizer"
    name = "insight_synthesizer"
    system_prompt = """Ты — Insight Synthesizer агентства моделей Nevesty Models.
Синтезируешь инсайты из всех источников: данные рынка, анализ конкурентов, тренды, метрики бизнеса.
Превращаешь разрозненную информацию в чёткие, actionable рекомендации для команды.
Умеешь выделять главное, расставлять приоритеты, формулировать конкретные следующие шаги.
Мыслишь стратегически, но даёшь тактические рекомендации с конкретными KPI. Всё на русском языке."""

    def synthesize_insights(self, context: dict) -> dict:
        """Синтезирует инсайты из всех источников в actionable рекомендации."""
        try:
            return self.think_json(
                "Синтезируй инсайты из всех доступных данных о рынке и бизнесе Nevesty Models.\n"
                "Переведи в конкретные рекомендации с приоритетами и ожидаемым эффектом.\n"
                "Верни JSON:\n"
                '{"key_insights": ['
                '{"insight": "70% клиентов приходят повторно — высокий LTV", '
                '"source": "аналитика заказов", '
                '"importance": "критическая", '
                '"implication": "инвестировать в удержание, а не привлечение"}], '
                '"priority_actions": ['
                '{"rank": 1, '
                '"action": "Запустить реферальную программу для постоянных клиентов", '
                '"department": "marketing", '
                '"effort": "средний", '
                '"expected_revenue_impact_rub": 50000, '
                '"timeframe_weeks": 2, '
                '"success_metric": "10 рефералов в первый месяц"}], '
                '"strategic_recommendations": ['
                '{"area": "Позиционирование", '
                '"recommendation": "Сфокусироваться на B2B сегменте — выше средний чек", '
                '"rationale": "обоснование"}], '
                '"quick_wins": [{"action": "действие", "effort_hrs": 2, "impact": "конкретный результат"}], '
                '"risks_identified": [{"risk": "риск", "probability": "высокая|средняя|низкая", "mitigation": "как снизить"}], '
                '"north_star_metric": "главная метрика для фокуса команды на квартал"}',
                context=context,
                max_tokens=2000,
            ) or {}
        except Exception as e:
            logger.error("[research/insight_synthesizer] synthesize_insights error: %s", e)
            return {}


class ResearchDepartment:
    """Координатор исследовательского департамента."""

    def __init__(self):
        self.market = MarketResearcher()
        self.competitors = CompetitorAnalyst()
        self.trends = TrendSpotter()
        self.synthesizer = InsightSynthesizer()

    def execute_task(self, task: str, context: dict) -> dict:
        """Диспетчер по ключевым словам задачи."""
        task_lower = task.lower()
        result_data = {}
        roles_used = []

        try:
            if any(kw in task_lower for kw in ("рынок", "market", "сегмент", "аудитори", "спрос")):
                result_data["market_research"] = self.market.research_market(context)
                roles_used.append("market_researcher")
        except Exception as e:
            logger.error("[ResearchDept] market task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("конкурент", "competitor", "сравнени", "позиционирован")):
                result_data["competitor_analysis"] = self.competitors.analyze_competitors(context)
                roles_used.append("competitor_analyst")
        except Exception as e:
            logger.error("[ResearchDept] competitor task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("тренд", "trend", "fashion", "мода", "event", "ивент")):
                result_data["trends"] = self.trends.spot_trends(context)
                roles_used.append("trend_spotter")
        except Exception as e:
            logger.error("[ResearchDept] trends task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("инсайт", "insight", "синтез", "рекоменд", "выводы")) \
                    or not roles_used:
                result_data["insights"] = self.synthesizer.synthesize_insights(context)
                roles_used.append("insight_synthesizer")
        except Exception as e:
            logger.error("[ResearchDept] synthesizer task error: %s", e)

        output = {
            "department": "research",
            "task": task,
            "result": result_data,
            "roles_used": roles_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info("[ResearchDept] Задача '%s' выполнена. Ролей задействовано: %d", task, len(roles_used))
        return output
