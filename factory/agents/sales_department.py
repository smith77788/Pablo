"""
Sales Department — Lead qualification, proposal writing, follow-up, pricing.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any


class SalesDepartment:
    """Sales Department with simple heuristic-based methods (no API calls)."""

    # ------------------------------------------------------------------ #
    # qualify_lead                                                         #
    # ------------------------------------------------------------------ #

    def qualify_lead(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze order and return {score, tier, notes}.

        Heuristics:
          - budget > 100 000  → score 80+, tier 'premium'
          - budget 30 000–100 000 → score 50–79, tier 'standard'
          - budget < 30 000   → score <50, tier 'economy'
          - corporate event   → +10
          - date within 30 days → +15 (urgency bonus)
        """
        budget = order_data.get("budget", 0) or 0
        event_type = (order_data.get("event_type") or "").lower()
        event_date_raw = order_data.get("date") or order_data.get("event_date")

        # Base score / tier from budget
        if budget > 100_000:
            score = 80
            tier = "premium"
        elif budget >= 30_000:
            score = 50
            tier = "standard"
        else:
            score = 20
            tier = "economy"

        notes_parts: list[str] = []

        # Corporate bonus
        if "корпорат" in event_type or "corporate" in event_type:
            score += 10
            notes_parts.append("корпоративное мероприятие (+10)")

        # Urgency bonus — date within 30 days
        if event_date_raw:
            try:
                if isinstance(event_date_raw, str):
                    event_date = datetime.fromisoformat(event_date_raw).date()
                else:
                    event_date = event_date_raw
                days_until = (event_date - datetime.now(timezone.utc).date()).days
                if 0 <= days_until <= 30:
                    score += 15
                    notes_parts.append(f"срочно — через {days_until} дн. (+15)")
            except (ValueError, TypeError):
                pass

        # Cap at 100
        score = min(score, 100)

        # Re-assign tier after bonuses (premium threshold 80+)
        if score >= 80:
            tier = "premium"
        elif score >= 50:
            tier = "standard"
        else:
            tier = "economy"

        notes = "; ".join(notes_parts) if notes_parts else "стандартная оценка"
        return {"score": score, "tier": tier, "notes": notes}

    # ------------------------------------------------------------------ #
    # generate_proposal                                                    #
    # ------------------------------------------------------------------ #

    def generate_proposal(self, order_data: Dict[str, Any]) -> str:
        """Return a template proposal text based on order data."""
        client = order_data.get("client_name") or order_data.get("name") or "Уважаемый клиент"
        event_type = order_data.get("event_type") or "мероприятие"
        budget = order_data.get("budget")
        budget_str = f"{budget:,} ₽" if budget else "по запросу"
        event_date = order_data.get("date") or order_data.get("event_date") or "по согласованию"
        model_count = order_data.get("model_count") or 1

        return (
            f"Коммерческое предложение\n"
            f"{'=' * 40}\n"
            f"Клиент: {client}\n"
            f"Тип мероприятия: {event_type}\n"
            f"Дата: {event_date}\n"
            f"Бюджет: {budget_str}\n"
            f"Количество моделей: {model_count}\n\n"
            f"Уважаемый(ая) {client},\n\n"
            f"Агентство Nevesty Models рады предложить вам профессиональных моделей "
            f"для вашего {event_type}.\n\n"
            f"Мы подберём {model_count} модель(ей) в соответствии с вашими требованиями "
            f"и обеспечим полное сопровождение на всех этапах.\n\n"
            f"Стоимость услуг: {budget_str}\n\n"
            f"Для подтверждения заявки свяжитесь с нашим менеджером.\n"
            f"С уважением, команда Nevesty Models"
        )

    # ------------------------------------------------------------------ #
    # get_followup_schedule                                                #
    # ------------------------------------------------------------------ #

    def get_followup_schedule(self, order_id: int, status: str) -> List[Dict[str, Any]]:
        """Return list of follow-up dicts {day_offset, message} based on status."""
        now = datetime.now(timezone.utc)
        status = (status or "").lower()

        schedules: dict[str, list[dict]] = {
            "new": [
                {
                    "date": (now + timedelta(days=1)).isoformat(),
                    "day_offset": 1,
                    "message": f"Заявка #{order_id}: подтверждение получения заявки, уточнение деталей.",
                },
                {
                    "date": (now + timedelta(days=3)).isoformat(),
                    "day_offset": 3,
                    "message": f"Заявка #{order_id}: отправка коммерческого предложения.",
                },
                {
                    "date": (now + timedelta(days=7)).isoformat(),
                    "day_offset": 7,
                    "message": f"Заявка #{order_id}: напоминание, если нет ответа.",
                },
            ],
            "processing": [
                {
                    "date": (now + timedelta(days=2)).isoformat(),
                    "day_offset": 2,
                    "message": f"Заявка #{order_id}: статус обработки, запрос уточнений.",
                },
                {
                    "date": (now + timedelta(days=5)).isoformat(),
                    "day_offset": 5,
                    "message": f"Заявка #{order_id}: промежуточный отчёт.",
                },
            ],
            "completed": [
                {
                    "date": (now + timedelta(days=1)).isoformat(),
                    "day_offset": 1,
                    "message": f"Заявка #{order_id}: запрос отзыва о сотрудничестве.",
                },
                {
                    "date": (now + timedelta(days=30)).isoformat(),
                    "day_offset": 30,
                    "message": f"Заявка #{order_id}: предложение повторного сотрудничества.",
                },
            ],
        }

        return schedules.get(status, [
            {
                "date": (now + timedelta(days=3)).isoformat(),
                "day_offset": 3,
                "message": f"Заявка #{order_id}: стандартный follow-up.",
            }
        ])

    # ------------------------------------------------------------------ #
    # suggest_pricing                                                      #
    # ------------------------------------------------------------------ #

    def suggest_pricing(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return {min_price, max_price, recommended} based on event_type and model_count."""
        event_type = (params.get("event_type") or "").lower()
        model_count = max(1, int(params.get("model_count") or 1))

        # Base price per model per event type
        base_prices: dict[str, tuple[int, int]] = {
            "корпоратив": (15_000, 35_000),
            "corporate": (15_000, 35_000),
            "свадьба": (20_000, 50_000),
            "wedding": (20_000, 50_000),
            "фотосессия": (10_000, 25_000),
            "photoshoot": (10_000, 25_000),
            "показ": (25_000, 60_000),
            "fashion show": (25_000, 60_000),
            "промо": (8_000, 20_000),
            "promo": (8_000, 20_000),
        }

        # Find matching key
        base_min, base_max = 12_000, 30_000  # default
        for key, (bmin, bmax) in base_prices.items():
            if key in event_type:
                base_min, base_max = bmin, bmax
                break

        min_price = base_min * model_count
        max_price = base_max * model_count
        recommended = int((min_price + max_price) / 2)

        return {
            "min_price": min_price,
            "max_price": max_price,
            "recommended": recommended,
        }
