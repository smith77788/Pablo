"""Content Department — generates SEO descriptions and content for models."""
from __future__ import annotations
import os
import sqlite3
import json
from datetime import datetime, timedelta
from typing import Optional

from factory.agents.base import FactoryAgent


def _db_path() -> str:
    import os
    return os.environ.get('DB_PATH', '/home/user/Pablo/nevesty-models/data.db')


class ModelDescriptionAgent:
    """Generates compelling bio/description for a model based on their parameters."""

    PROMPT_TEMPLATE = """You are a copywriter for a premium modeling agency.
Generate a compelling, professional description (2-3 sentences, ~100 words) for this model in Russian:

Name: {name}
Age: {age}
Height: {height} cm
Category: {category}
City: {city}
Hair: {hair_color}
Eyes: {eye_color}

Write a warm, professional description that highlights their uniqueness.
Focus on professionalism and approachability.
Output ONLY the description text, no labels or JSON."""

    def generate_for_model(self, model: dict) -> str:
        """Generate description using Claude API or return a template."""
        try:
            import anthropic
            import os
            client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
            prompt = self.PROMPT_TEMPLATE.format(
                name=model.get('name', ''),
                age=model.get('age', ''),
                height=model.get('height', ''),
                category=model.get('category', ''),
                city=model.get('city', ''),
                hair_color=model.get('hair_color', ''),
                eye_color=model.get('eye_color', ''),
            )
            msg = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=300,
                messages=[{'role': 'user', 'content': prompt}],
            )
            return msg.content[0].text.strip()
        except Exception:
            # Fallback template if API unavailable
            cat_map = {'fashion': 'фэшн', 'commercial': 'коммерческой', 'events': 'событийной'}
            cat = cat_map.get(model.get('category', ''), 'модельной')
            return (
                f"{model.get('name', 'Модель')} — профессиональная модель в сфере {cat} съёмки"
                f" из {model.get('city', 'Москвы')}. "
                f"Рост {model.get('height', '')} см, возраст {model.get('age', '')} лет. "
                f"Опытный профессионал, готовый к самым разным проектам."
            )

    def update_models_without_bio(self, max_models: int = 5) -> list[dict]:
        """Find models with empty bio and generate descriptions for them."""
        results = []
        try:
            conn = sqlite3.connect(_db_path())
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, age, height, category, city, hair_color, eye_color "
                "FROM models WHERE (bio IS NULL OR bio = '') AND archived=0 LIMIT ?",
                (max_models,)
            )
            models = [dict(row) for row in cur.fetchall()]

            for model in models:
                description = self.generate_for_model(model)
                cur.execute("UPDATE models SET bio=? WHERE id=?", (description, model['id']))
                results.append({'id': model['id'], 'name': model['name'], 'bio': description})

            conn.commit()
            conn.close()
        except Exception as e:
            results.append({'error': str(e)})
        return results


class FAQContentAgent:
    """Generates FAQ entries from common client questions."""

    COMMON_QUESTIONS = [
        ("Сколько стоят услуги агентства?", "Стоимость зависит от типа съёмки, продолжительности и выбранной модели. Свяжитесь с нами для получения индивидуального предложения."),
        ("Как заказать модель?", "Нажмите кнопку «Забронировать» на странице понравившейся модели или заполните форму на нашем сайте. Наш менеджер свяжется с вами в течение 1 часа."),
        ("Работаете ли вы в выходные?", "Да, мы работаем 7 дней в неделю. Менеджер доступен с 9:00 до 22:00 по московскому времени."),
        ("Есть ли минимальное время съёмки?", "Минимальная продолжительность работы — 2 часа. Рекомендуем планировать не менее 4 часов для комфортной съёмки."),
        ("Можно ли увидеть портфолио модели?", "Полное портфолио доступно в нашем каталоге на сайте и в боте. Также вы можете запросить дополнительные материалы у менеджера."),
    ]

    def populate_faq_if_empty(self) -> list[dict]:
        """Add default FAQ entries if FAQ table is empty."""
        results = []
        try:
            conn = sqlite3.connect(_db_path())
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM faq WHERE active=1")
            count = cur.fetchone()[0]

            if count == 0:
                for i, (q, a) in enumerate(self.COMMON_QUESTIONS):
                    cur.execute(
                        "INSERT OR IGNORE INTO faq (question, answer, category, sort_order, active) VALUES (?,?,?,?,1)",
                        (q, a, 'general', i * 10)
                    )
                    results.append({'question': q[:50]})
                conn.commit()
            else:
                results.append({'status': f'FAQ already has {count} entries'})
            conn.close()
        except Exception as e:
            results.append({'error': str(e)})
        return results


class WeeklySummaryAgent(FactoryAgent):
    """Generates weekly business summary and sends to admin via bot webhook."""

    department = "content"
    role = "weekly_summary"

    def generate_summary(self, db_path: str = None) -> dict:
        """Generate a weekly performance summary from the database."""
        summary = {
            "period": f"{(datetime.now() - timedelta(days=7)).strftime('%d.%m')} – {datetime.now().strftime('%d.%m.%Y')}",
            "orders_week": 0,
            "revenue_week": 0,
            "new_clients": 0,
            "top_model": None,
            "conversion": 0,
        }

        try:
            actual_db = db_path or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "nevesty-models", "data.db"
            )
            if not os.path.exists(actual_db):
                return summary

            conn = sqlite3.connect(actual_db)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # Orders this week
            c.execute("SELECT COUNT(*) as n FROM orders WHERE created_at >= datetime('now', '-7 days')")
            row = c.fetchone()
            summary["orders_week"] = row["n"] if row else 0

            # Revenue this week (completed orders)
            c.execute("""SELECT SUM(CAST(REPLACE(REPLACE(budget,'₽',''),' ','') AS INTEGER)) as s
                         FROM orders WHERE status='completed' AND created_at >= datetime('now', '-7 days')
                         AND budget IS NOT NULL AND budget GLOB '[0-9]*'""")
            row = c.fetchone()
            summary["revenue_week"] = int(row["s"] or 0) if row else 0

            # New clients this week
            c.execute("SELECT COUNT(DISTINCT client_chat_id) as n FROM orders WHERE created_at >= datetime('now', '-7 days')")
            row = c.fetchone()
            summary["new_clients"] = row["n"] if row else 0

            # Top model by orders this week
            c.execute("""SELECT m.name, COUNT(*) as cnt FROM orders o
                         JOIN models m ON o.model_id = m.id
                         WHERE o.created_at >= datetime('now', '-7 days')
                         GROUP BY o.model_id ORDER BY cnt DESC LIMIT 1""")
            row = c.fetchone()
            if row:
                summary["top_model"] = f"{row['name']} ({row['cnt']} заказов)"

            # Conversion
            c.execute("SELECT COUNT(*) as n FROM orders WHERE status IN ('new','pending') AND created_at >= datetime('now', '-7 days')")
            row = c.fetchone()
            total_new = row["n"] if row else 0
            c.execute("SELECT COUNT(*) as n FROM orders WHERE status='confirmed' AND created_at >= datetime('now', '-7 days')")
            row = c.fetchone()
            confirmed = row["n"] if row else 0
            summary["conversion"] = round(confirmed / total_new * 100) if total_new > 0 else 0

            conn.close()
        except Exception as e:
            summary["error"] = str(e)

        return summary

    def format_telegram_message(self, summary: dict) -> str:
        lines = [
            f"📊 *Недельный отчёт Factory*",
            f"_Период: {summary['period']}_",
            "",
            f"📋 Заявок за неделю: *{summary['orders_week']}*",
            f"💰 Выручка: *{summary['revenue_week']:,} ₽*".replace(",", " "),
            f"👤 Новых клиентов: *{summary['new_clients']}*",
            f"🔄 Конверсия: *{summary['conversion']}%*",
        ]
        if summary.get("top_model"):
            lines.append(f"⭐ Топ-модель: *{summary['top_model']}*")
        return "\n".join(lines)


class ContentDepartment:
    def __init__(self):
        self.description_agent = ModelDescriptionAgent()
        self.faq_agent = FAQContentAgent()

    def run_cycle(self) -> dict:
        """Run one content generation cycle."""
        results = {}

        # Generate descriptions for models without bio
        desc_results = self.description_agent.update_models_without_bio(max_models=3)
        results['model_descriptions'] = desc_results

        # Populate FAQ if empty
        faq_results = self.faq_agent.populate_faq_if_empty()
        results['faq'] = faq_results

        # On Mondays, generate weekly summary
        if datetime.now().weekday() == 0:  # Monday
            weekly_agent = WeeklySummaryAgent()
            weekly_data = weekly_agent.generate_summary()
            results["weekly_summary"] = weekly_data

        return results
