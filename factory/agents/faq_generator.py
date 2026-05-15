"""FAQ Generator Agent - generates and improves FAQ content heuristically."""
from __future__ import annotations
import sqlite3
from typing import Any


FAQ_TEMPLATES = {
    'booking': {
        'questions': [
            'Как забронировать модель?',
            'Сколько времени занимает оформление заявки?',
            'Можно ли отменить бронирование?',
        ],
        'answer_prefix': 'Для бронирования модели ',
    },
    'pricing': {
        'questions': [
            'Сколько стоит аренда модели?',
            'Какой минимальный бюджет?',
            'Есть ли скидки?',
        ],
        'answer_prefix': 'Стоимость услуг зависит от ',
    },
    'catalog': {
        'questions': [
            'Как найти модель по параметрам?',
            'Сколько моделей в каталоге?',
            'Как связаться с моделью напрямую?',
        ],
        'answer_prefix': 'В нашем каталоге ',
    },
    'general': {
        'questions': [
            'Работаете ли вы в выходные?',
            'Есть ли у вас офис?',
            'Как долго работает агентство?',
        ],
        'answer_prefix': 'Агентство работает ',
    },
}

ANSWER_IMPROVEMENTS = [
    ('обратитесь', 'напишите нам в Telegram или'),
    ('свяжитесь', 'напишите нам — мы ответим быстро,'),
    ('подробнее', 'подробнее (обычно в течение 1 рабочего дня)'),
]


class FAQGenerator:
    """Generates and improves FAQ content heuristically."""

    def generate_answers(self, category: str) -> list[dict[str, str]]:
        """Generate FAQ entries for a given category."""
        tmpl = FAQ_TEMPLATES.get(category, FAQ_TEMPLATES['general'])
        result = []
        for q in tmpl['questions']:
            answer = self._build_answer(category, q, tmpl['answer_prefix'])
            result.append({'question': q, 'answer': answer, 'category': category})
        return result

    def _build_answer(self, category: str, question: str, prefix: str) -> str:
        if category == 'booking':
            return (
                f"{prefix}воспользуйтесь нашим Telegram-ботом — он проведёт вас "
                "через все шаги за 2 минуты. Укажите дату, тип мероприятия и бюджет."
            )
        elif category == 'pricing':
            return (
                f"{prefix}типа мероприятия, продолжительности и опыта модели. "
                "Средний диапазон: от 5 000 до 50 000 ₽. Оставьте заявку — "
                "менеджер пришлёт точный расчёт."
            )
        elif category == 'catalog':
            return (
                f"{prefix}более 50 профессиональных моделей. Фильтруйте по городу, "
                "параметрам и категории. Напрямую связаться нельзя — оставьте заявку."
            )
        else:
            return (
                f"{prefix}по будням 9:00–21:00, по выходным 10:00–18:00 (МСК). "
                "В Telegram отвечаем быстрее всего."
            )

    def improve_answer(self, answer: str) -> str:
        """Apply heuristic improvements to an existing answer."""
        result = answer
        for old, new in ANSWER_IMPROVEMENTS:
            result = result.replace(old, new)
        return result

    def suggest_questions(self, existing_questions: list[str]) -> list[str]:
        """Suggest new FAQ questions not already covered."""
        all_questions = []
        for tmpl in FAQ_TEMPLATES.values():
            all_questions.extend(tmpl['questions'])
        existing_lower = {q.lower() for q in existing_questions}
        return [q for q in all_questions if q.lower() not in existing_lower]

    def read_from_db(self, db_path: str) -> list[dict[str, Any]]:
        """Read existing FAQ entries from DB."""
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, question, answer, category FROM faq WHERE active=1 ORDER BY sort_order"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def run(self, db_path: str) -> dict[str, Any]:
        """Full FAQ generation cycle."""
        existing = self.read_from_db(db_path)
        existing_questions = [r['question'] for r in existing]
        suggestions = self.suggest_questions(existing_questions)
        improved = [
            {**r, 'improved_answer': self.improve_answer(r['answer'])}
            for r in existing
            if r.get('answer')
        ]
        new_by_category: dict[str, list] = {}
        for cat in FAQ_TEMPLATES:
            new_by_category[cat] = self.generate_answers(cat)
        return {
            'existing_count': len(existing),
            'suggestions': suggestions,
            'improved_count': len(improved),
            'new_by_category': new_by_category,
        }
