"""
Creative Department — Copywriting, visual concepts, brand voice, storytelling.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from factory.agents.base import FactoryAgent

DB_PATH = Path(__file__).parent.parent.parent / "nevesty-models" / "data.db"


def _read_db(query: str, params=()) -> list[dict]:
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


class CopywriterAgent(FactoryAgent):
    department = 'creative'
    role = 'Copywriter'
    name = 'Анастасия'
    system_prompt = (
        "Ты — Анастасия, Copywriter агентства моделей Nevesty Models. "
        "Пишешь маркетинговые тексты: слоганы, рекламные объявления, описания моделей. "
        "Стиль: элегантный, притягивающий, продающий без явной рекламы. "
        "Всё на русском языке."
    )

    def build_prompt(self, context: dict) -> str:
        models = _read_db(
            "SELECT name, category FROM models WHERE available=1 ORDER BY featured DESC LIMIT 5"
        )
        model_list = "\n".join(
            f"- {r['name']} ({r['category']})" for r in models
        ) if models else "модели агентства"

        recent_orders = context.get('recent_orders', 'No data')

        return (
            f"You are {self.name}, Copywriter for a premium modeling agency Nevesty Models.\n\n"
            f"Available models:\n{model_list}\n\n"
            f"Recent orders context: {recent_orders}\n\n"
            "Generate marketing copy:\n"
            "1. Write 3 taglines for the agency (short, memorable, elegant)\n"
            "2. Write 2 ad texts for social media (Instagram/Telegram, up to 100 words each)\n"
            "3. Write a compelling description for one of the top models\n"
            "4. Suggest 2 headline options for a promotional campaign\n\n"
            "All copy must be in Russian. Style: elegant, aspirational, action-driving."
        )

    def run(self, context: dict | None = None) -> dict:
        context = context or {}
        prompt = self.build_prompt(context)
        result = self.think(prompt, context=context, max_tokens=700)
        return {'role': self.role, 'department': self.department, 'result': result}


class VisualConceptorAgent(FactoryAgent):
    department = 'creative'
    role = 'VisualConceptor'
    name = 'Артём'
    system_prompt = (
        "Ты — Артём, Visual Conceptor агентства моделей Nevesty Models. "
        "Разрабатываешь идеи визуальных концепций для фотосессий и мероприятий. "
        "Знаешь тренды моды и визуального сторителлинга. "
        "Всё на русском языке."
    )

    def build_prompt(self, context: dict) -> str:
        events = _read_db(
            "SELECT event_type, COUNT(*) as cnt FROM orders GROUP BY event_type ORDER BY cnt DESC LIMIT 5"
        )
        event_types = ", ".join(r['event_type'] for r in events) if events else "корпоратив, фотосессия, свадьба"

        recent_orders = context.get('recent_orders', 'No data')

        return (
            f"You are {self.name}, Visual Conceptor for a premium modeling agency.\n\n"
            f"Most requested event types: {event_types}\n\n"
            f"Recent orders context: {recent_orders}\n\n"
            "Generate visual concept ideas:\n"
            "1. Describe 2 photoshoot concepts with: theme, location, lighting, styling, mood\n"
            "2. Suggest a visual concept for an upcoming corporate event (location, props, model styling)\n"
            "3. Recommend 3 trending visual styles for modeling agency content in 2025-2026\n"
            "4. Propose a content calendar theme for the next month\n\n"
            "All descriptions in Russian. Be specific and inspiring. Max 300 words."
        )

    def run(self, context: dict | None = None) -> dict:
        context = context or {}
        prompt = self.build_prompt(context)
        result = self.think(prompt, context=context, max_tokens=700)
        return {'role': self.role, 'department': self.department, 'result': result}


class BrandVoiceKeeperAgent(FactoryAgent):
    department = 'creative'
    role = 'BrandVoiceKeeper'
    name = 'Мария'
    system_prompt = (
        "Ты — Мария, Brand Voice Keeper агентства моделей Nevesty Models. "
        "Следишь за единым стилем коммуникации агентства. "
        "Аудируешь все тексты на соответствие бренд-голосу и предлагаешь улучшения. "
        "Всё на русском языке."
    )

    def build_prompt(self, context: dict) -> str:
        recent_orders = context.get('recent_orders', 'No data')

        return (
            f"You are {self.name}, Brand Voice Keeper for Nevesty Models modeling agency.\n\n"
            f"Business context: {recent_orders}\n\n"
            "Audit brand communication consistency and suggest improvements:\n"
            "1. Define the agency's brand voice in 3-5 key attributes\n"
            "2. Identify 3 common communication mistakes to avoid (with examples of wrong vs right)\n"
            "3. Audit the following hypothetical copy for brand consistency and suggest improvements:\n"
            '   - Telegram post: "У нас есть модели для любых мероприятий. Пишите!"\n'
            '   - Instagram bio: "Модельное агентство Nevesty Models. Москва."\n'
            "4. Create a brief brand voice guide (tone, forbidden words, must-have phrases)\n\n"
            "Respond in Russian. Be specific and actionable. Max 300 words."
        )

    def run(self, context: dict | None = None) -> dict:
        context = context or {}
        prompt = self.build_prompt(context)
        result = self.think(prompt, context=context, max_tokens=700)
        return {'role': self.role, 'department': self.department, 'result': result}


class StorytellingAgent(FactoryAgent):
    department = 'creative'
    role = 'Storytelling'
    name = 'Ольга'
    system_prompt = (
        "Ты — Ольга, Storytelling Specialist агентства моделей Nevesty Models. "
        "Создаёшь захватывающие истории и кейсы из реальных заказов и опыта агентства. "
        "Умеешь находить эмоциональный крючок в каждой истории. "
        "Всё на русском языке."
    )

    def build_prompt(self, context: dict) -> str:
        completed = _read_db(
            """SELECT event_type, COUNT(*) as cnt FROM orders
               WHERE status = 'completed'
               GROUP BY event_type ORDER BY cnt DESC LIMIT 5"""
        )
        completed_str = "\n".join(
            f"- {r['event_type']}: {r['cnt']} завершённых заказов" for r in completed
        ) if completed else "Нет данных о завершённых заказах"

        recent_orders = context.get('recent_orders', 'No data')

        return (
            f"You are {self.name}, Storytelling Specialist for a premium modeling agency.\n\n"
            "Completed orders by event type:\n"
            f"{completed_str}\n\n"
            f"Recent orders context: {recent_orders}\n\n"
            "Create compelling stories and case studies:\n"
            "1. Write a success story based on a completed corporate event order "
            "(include: situation, challenge, solution, result, emotional hook)\n"
            "2. Create a client testimonial story that could be used on the website\n"
            "3. Draft a 'behind the scenes' story about how the agency prepares for a photoshoot\n"
            "4. Suggest 3 story angles that would resonate most with potential clients\n\n"
            "All stories in Russian. Engaging, emotional, authentic. Max 350 words total."
        )

    def run(self, context: dict | None = None) -> dict:
        context = context or {}
        prompt = self.build_prompt(context)
        result = self.think(prompt, context=context, max_tokens=800)
        return {'role': self.role, 'department': self.department, 'result': result}


class CreativeDepartment:
    """Creative Department: 4 agents for copywriting, visual concepts, brand voice, storytelling."""

    def __init__(self):
        self.agents = [
            CopywriterAgent(),
            VisualConceptorAgent(),
            BrandVoiceKeeperAgent(),
            StorytellingAgent(),
        ]

    def run_cycle(self, context: dict | None = None) -> dict:
        context = context or {}
        results = {}
        for agent in self.agents:
            try:
                results[agent.role] = agent.run(context)  # type: ignore[attr-defined]
            except Exception as e:
                results[agent.role] = {"error": str(e)}
        return results
