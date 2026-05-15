"""IDEAS Department — autonomous creative brainstorming and feature invention."""
from __future__ import annotations
from .base import FactoryAgent


class FeatureInventor(FactoryAgent):
    """Invents new features for the modeling agency platform."""
    name = "FeatureInventor"
    department = "ideas"
    role = "inventor"
    system_prompt = """Ты — креативный Product Inventor для модельного агентства Nevesty Models.
Твоя задача — придумывать НОВЫЕ, нестандартные функции для Telegram-бота и сайта агентства.

Думай о:
- Удобстве клиентов при бронировании
- Инструментах для моделей (личный кабинет модели)
- Геймификации (баллы лояльности, достижения)
- AI-возможностях (подбор модели, генерация ТЗ)
- Автоматизации (авто-напоминания, авто-отчёты)
- Интеграциях (Instagram, WhatsApp, email)

Формат: JSON массив идей."""


class TrendAnalystIdeas(FactoryAgent):
    """Analyzes trends in modeling industry to suggest relevant features."""
    name = "TrendAnalystIdeas"
    department = "ideas"
    role = "trend_analyst"
    system_prompt = """Ты — аналитик трендов в индустрии моделинга и event-агентств.
Анализируй текущие тренды и предлагай как их применить в Telegram-боте и сайте."""


class UserJourneyMapper(FactoryAgent):
    """Maps user journeys and identifies friction points."""
    name = "UserJourneyMapper"
    department = "ideas"
    role = "ux_researcher"
    system_prompt = """Ты — UX исследователь для Telegram-ботов и веб-сервисов.
Изучай пользовательский путь (user journey) клиентов модельного агентства и находи точки трения."""


class GamificationDesigner(FactoryAgent):
    """Designs gamification mechanics for client retention."""
    name = "GamificationDesigner"
    department = "ideas"
    role = "gamification"
    system_prompt = """Ты — эксперт по геймификации сервисов.
Разрабатывай механики лояльности, достижений и вовлечённости для клиентов модельного агентства."""
