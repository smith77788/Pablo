"""Content Generator — produces ready-to-post Telegram channel content."""
from __future__ import annotations
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from factory.agents.base import FactoryAgent

logger = logging.getLogger(__name__)


NEVESTY_DB = Path(__file__).parent.parent.parent / "nevesty-models" / "data.db"


class ContentGenerator(FactoryAgent):
    name = "content_generator"
    department = "creative"
    role = "ContentGenerator"
    system_prompt = """Ты — опытный SMM-менеджер модельного агентства Nevesty Models.
Создаёшь привлекательный контент для Telegram-канала агентства.
Стиль: профессиональный, элегантный, с нотками эксклюзивности.
Всегда пишешь на русском языке. Используешь эмодзи умеренно.
Посты должны быть вовлекающими и побуждать к записи на мероприятие."""

    def _get_recent_stats(self) -> dict:
        """Get recent business stats to inform content."""
        stats = {}
        if not NEVESTY_DB.exists():
            return stats
        try:
            conn = sqlite3.connect(str(NEVESTY_DB))
            conn.row_factory = sqlite3.Row
            stats['completed_count'] = conn.execute(
                "SELECT COUNT(*) as n FROM orders WHERE status='completed'"
            ).fetchone()["n"]
            stats['top_models'] = [
                dict(r) for r in conn.execute(
                    "SELECT m.name, COUNT(o.id) as cnt FROM orders o JOIN models m ON o.model_id=m.id "
                    "WHERE o.status='completed' GROUP BY m.id ORDER BY cnt DESC LIMIT 3"
                ).fetchall()
            ]
            stats['categories'] = [
                dict(r) for r in conn.execute(
                    "SELECT event_type, COUNT(*) as cnt FROM orders GROUP BY event_type ORDER BY cnt DESC LIMIT 3"
                ).fetchall()
            ]
            conn.close()
        except Exception as e:
            logger.warning("Cannot read DB: %s", e)
        return stats

    def generate_post(self, post_type: str = "general") -> dict:
        """Generate a single Telegram channel post.

        post_type options: general, model_spotlight, event_promotion,
                          tips, case_study, seasonal
        """
        stats = self._get_recent_stats()

        prompt = f"""Создай пост для Telegram-канала модельного агентства Nevesty Models.

Тип поста: {post_type}
Статистика агентства: {json.dumps(stats, ensure_ascii=False, default=str)}
Дата: {datetime.now(timezone.utc).strftime('%B %Y')}

Создай 1 готовый пост. Верни JSON:
{{
  "text": "полный текст поста с эмодзи и форматированием (Markdown)",
  "hashtags": ["список", "хэштегов"],
  "image_prompt": "описание идеального фото для этого поста",
  "call_to_action": "призыв к действию в конце поста",
  "post_type": "{post_type}",
  "character_count": 0
}}

Требования:
- Длина: 200-800 символов
- Включи призыв к действию (запись, контакт менеджера)
- Используй цифры и факты где возможно
- Стиль: premium, элегантный, вовлекающий"""

        result = self.think_json(prompt, max_tokens=1024)
        return result if isinstance(result, dict) else {}

    def generate_weekly_content_plan(self) -> list:
        """Generate a 7-post weekly content plan."""
        stats = self._get_recent_stats()

        post_types = ["model_spotlight", "case_study", "tips", "event_promotion",
                      "general", "seasonal", "model_spotlight"]

        prompt = f"""Создай план контента на неделю для Telegram-канала Nevesty Models.

Статистика: {json.dumps(stats, ensure_ascii=False, default=str)}
Типы постов на каждый день: {', '.join(post_types)}

Верни JSON-массив из 7 объектов:
[{{
  "day": "Понедельник",
  "post_type": "model_spotlight",
  "topic": "конкретная тема",
  "key_message": "главная идея",
  "best_time": "10:00"
}}, ...]"""

        result = self.think_json(prompt, max_tokens=1500)
        return result if isinstance(result, list) else []

    def run(self) -> dict:
        """Generate today's content suggestions."""
        posts = []
        for post_type in ["general", "model_spotlight", "tips"]:
            try:
                post = self.generate_post(post_type)
                if post and post.get("text"):
                    posts.append(post)
            except Exception as e:
                logger.error("Content generation error for %s: %s", post_type, e)

        weekly_plan = []
        try:
            weekly_plan = self.generate_weekly_content_plan()
        except Exception as e:
            logger.error("Weekly plan error: %s", e)

        return {
            "generated_posts": posts,
            "weekly_plan": weekly_plan,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


class TelegramPostAgent(FactoryAgent):
    """Generates engaging Telegram channel posts for the modeling agency."""
    name = "TelegramPostAgent"
    department = "content"
    role = "telegram_post"
    system_prompt = """Ты — SMM-специалист модельного агентства Nevesty Models.
Создаёшь вирусные и engaging посты для Telegram-канала.
Стиль: профессиональный, но живой. Без лишних эмодзи (max 3 на пост).
Длина поста: 150-300 символов. Всегда заканчивай CTA (призыв к действию)."""

    def generate_post(self, post_type: str = "promo", context: dict = None) -> dict:
        """Generate a Telegram post of a specific type."""
        post_types = {
            "promo": "Промо пост про услуги агентства и бронирование моделей",
            "model_spotlight": "Spotlight пост о конкретной модели (без имени — только параметры и специализация)",
            "event_case": "Пост об успешном мероприятии/кейсе",
            "tips": "Полезный совет для клиентов агентства",
            "announcement": "Анонс специального предложения или акции",
        }
        prompt = f"""Напиши Telegram пост типа: {post_types.get(post_type, post_type)}

Требования:
- 150-300 символов
- Естественный, не рекламный тон
- Заканчивай призывом к действию
- Учитывай контекст: {context or {}}

Верни JSON: {{"text": "текст поста", "hashtags": ["тег1", "тег2"], "type": "{post_type}"}}"""

        result = self.think_json(prompt, context=context or {}, max_tokens=300)
        if not isinstance(result, dict) or "text" not in result:
            result = {
                "text": "Нужна профессиональная модель для вашего мероприятия? Мы подберём идеального кандидата в течение 2 часов. Пишите! 📋",
                "hashtags": ["#невестымодели", "#агентствомоделей", "#корпоратив"],
                "type": post_type
            }
        return result

    def generate_weekly_content_plan(self, context: dict = None) -> list[dict]:
        """Generate a 7-post weekly content plan."""
        prompt = """Составь план на 7 постов для Telegram-канала модельного агентства.
Чередуй типы: promo, model_spotlight, event_case, tips, announcement.
Верни JSON массив: [{"day": "Пн", "type": "promo", "topic": "описание темы"}, ...]"""
        result = self.think_json(prompt, context=context or {}, max_tokens=500)
        if isinstance(result, list):
            return result[:7]
        return [
            {"day": "Пн", "type": "promo", "topic": "Открытие недели — услуги агентства"},
            {"day": "Ср", "type": "model_spotlight", "topic": "Fashion-модель категории"},
            {"day": "Пт", "type": "event_case", "topic": "Успешный корпоратив"},
            {"day": "Вс", "type": "tips", "topic": "Как выбрать модель для мероприятия"},
        ]


class FAQGeneratorAgent(FactoryAgent):
    """Generates and updates FAQ answers based on platform data."""
    name = "FAQGeneratorAgent"
    department = "content"
    role = "faq_generator"
    system_prompt = """Ты — эксперт по контент-маркетингу модельного агентства.
Создаёшь чёткие, информативные ответы на вопросы клиентов.
Тон: профессиональный, дружелюбный."""

    def generate_faq_item(self, question: str, context: dict = None) -> dict:
        """Generate a FAQ answer for a given question."""
        prompt = f"""Напиши ответ на вопрос клиента: "{question}"
Контекст агентства: {context or {}}
Длина ответа: 50-150 слов. Конкретно и полезно.
Верни JSON: {{"question": "{question}", "answer": "ответ"}}"""
        result = self.think_json(prompt, max_tokens=200)
        if isinstance(result, dict) and "answer" in result:
            return result
        return {"question": question, "answer": "Обратитесь к нашему менеджеру для получения подробной информации."}

    def generate_full_faq(self) -> list[dict]:
        """Generate a complete FAQ for the modeling agency."""
        questions = [
            "Как заказать модель для мероприятия?",
            "Сколько стоят услуги агентства?",
            "Работаете ли вы по всей России?",
            "Сколько времени займёт подтверждение заявки?",
            "Можно ли посмотреть портфолио модели?",
            "Есть ли минимальный заказ по времени?",
            "Как происходит оплата?",
            "Можно ли заказать нескольких моделей?",
        ]
        return [self.generate_faq_item(q) for q in questions]


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 9.1 — Content Generation Factory (heuristic, no API key required)
# ─────────────────────────────────────────────────────────────────────────────

class ChannelPostGenerator(FactoryAgent):
    """Generates Telegram channel post templates."""
    name = "channel_post_generator"
    department = "creative"
    role = "content"
    system_prompt = "You are a creative content writer for a modeling agency."

    POST_TEMPLATES = [
        "🌟 Знакомьтесь с нашей топ-моделью!\n\n{model_intro}\n\n📞 Забронировать: @nevesty_models_bot",
        "✨ {promo_text}\n\n💼 Узнайте подробности в нашем каталоге!\n🤖 @nevesty_models_bot",
        "📸 {event_text}\n\n🎯 Для бронирования: @nevesty_models_bot",
        "🔥 Акция этой недели!\n\n{offer_text}\n\n⚡ @nevesty_models_bot",
    ]

    def generate_post(self, context: dict | None = None) -> str:
        """Returns a Telegram channel post template."""
        import random
        ctx = context or {}
        model_name = ctx.get('model_name', 'Мария')
        category = ctx.get('category', 'fashion')

        category_texts = {
            'fashion': 'специализируется на fashion-съёмках и показах',
            'commercial': 'работает в коммерческой рекламе и промо',
            'events': 'украшает корпоративные события и выставки',
        }
        spec = category_texts.get(category, 'профессиональная модель')

        templates = {
            'intro': f"{model_name} — {spec}. Опыт работы с ведущими брендами.",
            'promo': "Ищете модель для вашего проекта? Большой выбор профессионалов!",
            'event': "Ваше мероприятие будет незабываемым с нашими моделями!",
            'offer': "Скидка 10% при бронировании на следующую неделю!",
        }

        template = random.choice(self.POST_TEMPLATES)
        return template.format(
            model_intro=templates['intro'],
            promo_text=templates['promo'],
            event_text=templates['event'],
            offer_text=templates['offer'],
        )

    def run(self, context: dict | None = None) -> dict:
        import datetime
        posts = [self.generate_post(context) for _ in range(3)]
        return {
            "insights": ["Generated 3 post templates for Telegram channel"],
            "recommendations": posts,
            "timestamp": datetime.datetime.now().isoformat(),
        }


class ModelDescriptionWriter(FactoryAgent):
    """Generates professional model descriptions from parameters."""
    name = "model_description_writer"
    department = "creative"
    role = "copywriter"

    INTROS = [
        "{name} — профессиональная модель с богатым портфолио.",
        "Знакомьтесь: {name}, яркий представитель модельного бизнеса.",
        "{name} — воплощение стиля и профессионализма.",
    ]

    def generate_description(self, model: dict) -> str:
        """Generate description from model dict (name, height, age, category, city)."""
        import random
        name = model.get('name', 'Модель')
        height = model.get('height', '')
        age = model.get('age', '')
        category = model.get('category', 'fashion')
        city = model.get('city', 'Москва')

        cat_map = {
            'fashion': 'fashion-индустрии',
            'commercial': 'коммерческой рекламе',
            'events': 'event-индустрии',
        }
        cat_text = cat_map.get(category, 'модельном бизнесе')

        intro = random.choice(self.INTROS).format(name=name)
        params = []
        if height:
            params.append(f"рост {height} см")
        if age:
            params.append(f"возраст {age} лет")
        params_str = ", ".join(params)

        desc = f"{intro} Специализируется в {cat_text}."
        if params_str:
            desc += f" Параметры: {params_str}."
        desc += f" Работает в {city}."
        return desc

    def run(self, context: dict | None = None) -> dict:
        import datetime
        ctx = context or {}
        models = ctx.get('models', [
            {'name': 'Анна', 'height': 175, 'age': 24, 'category': 'fashion', 'city': 'Москва'}
        ])
        descriptions = [self.generate_description(m) for m in models[:5]]
        return {
            "insights": [f"Generated {len(descriptions)} model descriptions"],
            "recommendations": descriptions,
            "timestamp": datetime.datetime.now().isoformat(),
        }


class FAQGenerator(FactoryAgent):
    """Generates FAQ answers for the bot."""
    name = "faq_generator"
    department = "creative"
    role = "support"

    FAQ_BASE = {
        "Как забронировать модель?": (
            "Нажмите кнопку '📋 Забронировать' в каталоге, выберите модель и заполните форму. "
            "Наш менеджер свяжется с вами в течение часа."
        ),
        "Сколько стоит аренда?": (
            "Стоимость зависит от типа мероприятия и длительности. "
            "Ориентировочные цены: от 5000₽/час. Точная стоимость обсуждается с менеджером."
        ),
        "Как долго рассматривается заявка?": (
            "Мы отвечаем на заявки в течение 1-2 часов в рабочее время (10:00-20:00 МСК)."
        ),
        "Можно ли отменить бронирование?": (
            "Да, отмена возможна не позднее чем за 24 часа до начала мероприятия."
        ),
        "В каких городах работаете?": (
            "Основные города: Москва, Санкт-Петербург, Краснодар. "
            "Выезд в другие города обсуждается индивидуально."
        ),
    }

    def generate_faq(self) -> list[dict]:
        return [{"question": q, "answer": a} for q, a in self.FAQ_BASE.items()]

    def run(self, context: dict | None = None) -> dict:
        import datetime
        faq = self.generate_faq()
        return {
            "insights": [f"Generated {len(faq)} FAQ entries"],
            "recommendations": [f"Q: {f['question']}\nA: {f['answer']}" for f in faq],
            "timestamp": datetime.datetime.now().isoformat(),
        }


class ContentGenerationDepartment:
    """Orchestrates content generation agents."""

    def __init__(self):
        self.post_generator = ChannelPostGenerator()
        self.description_writer = ModelDescriptionWriter()
        self.faq_generator = FAQGenerator()

    def execute_task(self, task: str, context: dict | None = None) -> dict:
        import datetime
        results = {}

        if any(k in task.lower() for k in ['post', 'channel', 'telegram', 'публик']):
            results['posts'] = self.post_generator.run(context)

        if any(k in task.lower() for k in ['description', 'model', 'опис', 'модел']):
            results['descriptions'] = self.description_writer.run(context)

        if any(k in task.lower() for k in ['faq', 'question', 'питань', 'вопрос']):
            results['faq'] = self.faq_generator.run(context)

        if not results:
            results['posts'] = self.post_generator.run(context)
            results['descriptions'] = self.description_writer.run(context)
            results['faq'] = self.faq_generator.run(context)

        return {
            "department": "content_generation",
            "task": task,
            "results": results,
            "agents_used": list(results.keys()),
            "timestamp": datetime.datetime.now().isoformat(),
        }
