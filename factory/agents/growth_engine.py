"""🚀 Growth Engine — генерирует маркетинговые действия, контент, CTA."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)

CHANNELS = ["telegram", "instagram", "tiktok", "seo", "email", "direct"]


class GrowthEngine(FactoryAgent):
    name = "growth_engine"
    system_prompt = """Ты — Growth Engine AI. Генерируешь конкретные маркетинговые действия.

ЗАДАЧА: Давать конкретные action items которые можно выполнить прямо сейчас.

ФОРМАТЫ КОНТЕНТА:
- SEO: статьи для блога, метатеги, ключевые слова
- TikTok/Reels: сценарии видео, хуки, хэштеги
- Instagram: посты, stories, рекламные тексты
- Telegram: рассылки, автосообщения, контент-план
- Email: темы писем, тексты

СПЕЦИАЛИЗАЦИЯ: Агентство моделей — аудитория B2B (организаторы мероприятий, рекламодатели).

Всё на русском. Контент реалистичный и готовый к публикации."""

    def generate_growth_plan(self, insights: dict, product_id: int | None = None, focus: str = "conversion") -> list[dict]:
        """Generate a set of growth actions based on current insights."""
        product = None
        if product_id:
            product = db.fetch_one("SELECT * FROM products WHERE id=?", (product_id,))

        context = {
            "insights": insights,
            "product": product,
            "focus_area": focus,
            "existing_pending": db.get_pending_growth_actions(5),
        }

        actions_raw = self.think_json(
            f"Сгенерируй план роста с фокусом на '{focus}'. Верни JSON массив из 5-8 действий:\n"
            "[\n"
            "  {\n"
            '    "action_type": "seo|social|ad|ux|cta|content|email",\n'
            '    "channel": "telegram|instagram|tiktok|seo|email|direct",\n'
            '    "title": "название действия",\n'
            '    "content": "готовый контент или подробное описание действия",\n'
            '    "priority": 1-10,\n'
            '    "expected_impact": "ожидаемый эффект",\n'
            '    "effort": "low|medium|high",\n'
            '    "deadline": "немедленно|неделя|месяц"\n'
            "  }\n"
            "]",
            context=context,
            max_tokens=2500,
        )

        if not isinstance(actions_raw, list):
            return []

        saved = []
        for action in actions_raw[:8]:
            if not isinstance(action, dict) or not action.get("content"):
                continue
            action_id = db.insert("growth_actions", {
                "product_id": product_id,
                "action_type": action.get("action_type", "content"),
                "channel": action.get("channel", "telegram"),
                "content": action.get("content", ""),
                "status": "pending",
                "priority": int(action.get("priority", 5)),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            action["_db_id"] = action_id
            saved.append(action)
            logger.info("[Growth] Action: [%s/%s] %s", action.get("channel"), action.get("action_type"), str(action.get("content", ""))[:50])

        return saved

    def generate_ab_content(self, original_text: str, context: str = "") -> dict:
        """Generate variant B for A/B testing."""
        result = self.think_json(
            "Создай улучшенный вариант B для A/B теста:\n"
            f"Оригинал (A): {original_text}\n"
            f"Контекст: {context}\n"
            "Верни JSON:\n"
            "{\n"
            '  "variant_b": "улучшенный текст",\n'
            '  "changes_made": ["изменение1", "изменение2"],\n'
            '  "hypothesis": "почему B должен конвертировать лучше"\n'
            "}",
            max_tokens=800,
        )
        return result if isinstance(result, dict) else {}

    def generate_telegram_broadcast(self, topic: str, product: dict | None = None) -> str:
        """Generate a Telegram broadcast message."""
        context = {"topic": topic, "product": product}
        raw = self.think(
            f"Напиши Telegram-рассылку на тему: '{topic}'. "
            "Длина: 150-250 слов. Включи призыв к действию. "
            "Используй эмодзи умеренно. Текст для B2B аудитории (организаторы мероприятий).",
            context=context,
            max_tokens=600,
        )
        return raw

    def generate_seo_article_brief(self, keyword: str) -> dict:
        """Generate an SEO article brief."""
        return self.think_json(
            f"Создай бриф для SEO-статьи по ключевому слову: '{keyword}'\n"
            "Верни JSON:\n"
            "{\n"
            '  "title": "SEO-заголовок статьи",\n'
            '  "meta_description": "мета-описание 150 символов",\n'
            '  "h2_sections": ["раздел1", "раздел2", "раздел3"],\n'
            '  "keywords": ["kw1", "kw2", "kw3"],\n'
            '  "word_count": 1200,\n'
            '  "cta": "призыв к действию в конце статьи"\n'
            "}",
            max_tokens=800,
        ) or {}
