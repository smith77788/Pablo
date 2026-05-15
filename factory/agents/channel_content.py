"""
Channel Content Agent — generates Telegram channel post templates for Nevesty Models agency.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
import random


POST_FORMATS = ["case_study", "model_spotlight", "tips", "promotion", "behind_scenes", "stats"]

SEASONAL_THEMES: Dict[int, str] = {
    1: "Новогодние корпоративы",
    2: "Романтические события (14 февраля)",
    3: "Весенние показы",
    4: "Выставки и форумы",
    5: "Майские праздники",
    6: "Свадебный сезон",
    7: "Летние промо",
    8: "Бэк-ту-скул кампании",
    9: "Осенние Fashion weeks",
    10: "Корпоративы октябрь",
    11: "Pre-Christmas съёмки",
    12: "Новогодние корпоративы",
}

EVENT_TYPE_HASHTAGS: Dict[str, List[str]] = {
    "корпоратив": ["#корпоратив", "#корпоративноемероприятие", "#модели", "#event"],
    "фотосессия": ["#фотосессия", "#фото", "#модели", "#photoshoot"],
    "показ": ["#показмод", "#fashion", "#runway", "#подиум"],
    "промо": ["#промоутеры", "#promo", "#реклама", "#промо"],
    "свадьба": ["#свадьба", "#wedding", "#свадебноеагентство"],
    "default": ["#модели", "#агентствомоделей", "#nevestymodels"],
}


class ChannelContentGenerator:
    """Generates ready-to-post Telegram channel content."""

    AGENCY_NAME = "Nevesty Models"
    AGENCY_HANDLE = "@nevesty_models"

    def generate_model_spotlight_post(self, model_data: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a 'Model of the Week' spotlight post."""
        name = model_data.get("name", "Наша модель")
        age = model_data.get("age", "")
        height = model_data.get("height", "")
        category = model_data.get("category", "fashion")
        city = model_data.get("city", "Москва")
        order_count = model_data.get("order_count", 0)

        category_emoji = {"fashion": "👗", "commercial": "📺", "events": "🎉"}.get(category, "✨")
        height_str = f"{height} см" if height else ""
        age_str = f"{age} лет" if age else ""
        params = ", ".join(filter(None, [age_str, height_str, city]))

        text = (
            f"✨ <b>Модель недели — {name}</b>\n\n"
            f"{category_emoji} Специализация: {category}\n"
            f"📍 {params}\n"
            f"📋 Выполнено заказов: {order_count}\n\n"
            f"💼 Доступна для:\n"
            f"• Корпоративных мероприятий\n"
            f"• Фотосессий и рекламных съёмок\n"
            f"• Показов и презентаций\n\n"
            f"📲 Забронировать: {self.AGENCY_HANDLE}\n\n"
            f"#моделинедели #модели #агентствомоделей #{city.lower().replace(' ', '')} #nevestymodels"
        )
        return {
            "format": "model_spotlight",
            "text": text,
            "char_count": len(text),
            "model_name": name,
            "recommended_time": "вт/чт 18:00-20:00",
        }

    def generate_case_study_post(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a case study post from a completed order."""
        event_type = order_data.get("event_type", "мероприятие")
        city = order_data.get("city", "Москва")
        model_count = order_data.get("model_count", 1)
        duration_h = order_data.get("duration_hours", 4)
        month = datetime.now(timezone.utc).strftime("%B")

        hashtags = EVENT_TYPE_HASHTAGS.get(event_type.lower(), EVENT_TYPE_HASHTAGS["default"])
        hashtag_str = " ".join(hashtags[:4])

        text = (
            f"📸 <b>Кейс: {event_type} в {city}</b>\n\n"
            f"В {month} наши модели блестяще отработали очередное мероприятие.\n\n"
            f"🔢 Детали проекта:\n"
            f"• Тип: {event_type}\n"
            f"• Город: {city}\n"
            f"• Моделей: {model_count}\n"
            f"• Длительность: {duration_h} часа\n\n"
            f"✅ Клиент остался доволен профессионализмом команды!\n\n"
            f"Нужны модели для вашего события?\n"
            f"📲 Пишите: {self.AGENCY_HANDLE}\n\n"
            f"{hashtag_str} #nevestymodels"
        )
        return {
            "format": "case_study",
            "text": text,
            "char_count": len(text),
            "event_type": event_type,
            "recommended_time": "пн/ср/пт 12:00-14:00",
        }

    def generate_tips_post(self, topic: Optional[str] = None) -> Dict[str, Any]:
        """Generate a tips/educational post."""
        topics = {
            "choosing_model": {
                "title": "Как выбрать модель для вашего мероприятия",
                "tips": [
                    "Определите тип события: корпоратив, промо или показ",
                    "Учитывайте параметры: рост, возраст, опыт",
                    "Проверьте портфолио и отзывы",
                    "Заранее согласуйте дресс-код и сценарий",
                    "Бронируйте за 2–4 недели до события",
                ],
            },
            "event_prep": {
                "title": "5 шагов для идеального мероприятия с моделями",
                "tips": [
                    "Чёткое техзадание для модели за 3 дня",
                    "Согласованный дресс-код и образ",
                    "Инструктаж на месте за 30 минут до старта",
                    "Продуманный маршрут/сценарий",
                    "Контакт менеджера на протяжении всего события",
                ],
            },
        }
        selected = topics.get(topic or "", topics["choosing_model"])
        tips_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(selected["tips"]))

        text = (
            f"💡 <b>{selected['title']}</b>\n\n"
            f"{tips_text}\n\n"
            f"✨ {self.AGENCY_NAME} — профессиональный подход к каждому проекту.\n\n"
            f"📲 Консультация: {self.AGENCY_HANDLE}\n\n"
            f"#советы #мероприятие #модели #агентствомоделей #nevestymodels"
        )
        return {
            "format": "tips",
            "text": text,
            "char_count": len(text),
            "topic": topic or "choosing_model",
            "recommended_time": "ср 10:00-12:00",
        }

    def generate_promotion_post(self, discount_pct: int = 15, valid_days: int = 7) -> Dict[str, Any]:
        """Generate a promotional post with discount."""
        seasonal_theme = SEASONAL_THEMES.get(datetime.now(timezone.utc).month, "мероприятий")

        text = (
            f"🎉 <b>Специальное предложение!</b>\n\n"
            f"Готовитесь к {seasonal_theme.lower()}?\n\n"
            f"🔥 Скидка <b>{discount_pct}%</b> на бронирование моделей!\n\n"
            f"⏰ Акция действует ещё {valid_days} {'дней' if valid_days > 4 else 'дня'}\n\n"
            f"📋 Что включено:\n"
            f"• Подбор модели под ваш бриф\n"
            f"• Согласование образа\n"
            f"• Менеджерское сопровождение\n\n"
            f"📲 Для получения скидки напишите: {self.AGENCY_HANDLE}\n"
            f"Укажите промокод: <code>CHANNEL{discount_pct}</code>\n\n"
            f"#скидка #акция #модели #nevestymodels"
        )
        return {
            "format": "promotion",
            "text": text,
            "char_count": len(text),
            "discount_pct": discount_pct,
            "promo_code": f"CHANNEL{discount_pct}",
            "valid_days": valid_days,
            "recommended_time": "пт 15:00-17:00",
        }

    def generate_stats_post(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a monthly stats/achievement post."""
        orders = stats.get("total_orders", 0)
        models = stats.get("active_models", 0)
        cities = stats.get("cities_served", 1)
        rating = stats.get("avg_rating", 5.0)
        month_name = datetime.now(timezone.utc).strftime("%B")

        text = (
            f"📊 <b>Итоги {month_name} — {self.AGENCY_NAME}</b>\n\n"
            f"🗓 Этот месяц был насыщенным!\n\n"
            f"📈 Наши результаты:\n"
            f"✅ Выполнено заказов: {orders}\n"
            f"💃 Активных моделей: {models}\n"
            f"🌆 Городов: {cities}\n"
            f"⭐ Средний рейтинг: {rating:.1f}/5\n\n"
            f"Спасибо нашим клиентам за доверие! 🙏\n\n"
            f"Готовы работать ещё лучше в следующем месяце!\n"
            f"📲 {self.AGENCY_HANDLE}\n\n"
            f"#итоги #статистика #модели #nevestymodels"
        )
        return {
            "format": "stats",
            "text": text,
            "char_count": len(text),
            "month": month_name,
            "recommended_time": "1-е число месяца 10:00",
        }

    def get_content_calendar(self, weeks: int = 4) -> List[Dict[str, Any]]:
        """Return a content calendar with scheduled post formats."""
        calendar = []
        formats_cycle = ["model_spotlight", "tips", "case_study", "promotion", "stats", "model_spotlight"]
        for week in range(weeks):
            for day_offset, post_format in [(1, formats_cycle[week % len(formats_cycle)]),
                                             (4, "case_study" if week % 2 == 0 else "tips")]:
                calendar.append({
                    "week": week + 1,
                    "day": f"неделя {week + 1}, день {day_offset}",
                    "format": post_format,
                    "recommended_time": "12:00-14:00" if day_offset == 1 else "18:00-20:00",
                })
        return calendar[:weeks * 2]
