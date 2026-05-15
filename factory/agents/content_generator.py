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
