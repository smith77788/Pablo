"""ai_action_router.
СТАТУС: НЕ ПОДКЛЮЧЁН (проверено 2026-07-27). Маршрутизация фразы в действие по
regex-словарю. AI-ассистент использует свой путь (`bot/utils/ai_tools.py` +
`services/intent_planner`), который умеет больше.
"""

from __future__ import annotations
import json
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

ACTION_MAP = {
    "рассылк|broadcast|разослать": "broadcast",
    "канал|channel": "channel_manage",
    "бот|bot": "bot_manage",
    "аккаунт|account": "account_manage",
    " Strike|зачистк|report": "strike",
    "парсер|parser": "parse_audience",
    "прогрев|warmup": "warmup",
    "аналитик|statistics|статистик": "analytics",
    "CRM|контакт|contact": "crm",
    "подписк|subscription|тариф": "subscription",
}


def parse_intent(text: str) -> dict:
    text_lower = text.lower().strip()
    for pattern, action in ACTION_MAP.items():
        if re.search(pattern, text_lower):
            return {
                "action": action,
                "original_text": text,
                "confidence": 0.8,
                "suggestion": f"Вы хотите {action}. Подтвердите действие.",
            }
    return {
        "action": "unknown",
        "original_text": text,
        "confidence": 0.0,
        "suggestion": "Не удалось определить действие. Попробуйте: 'рассылка', 'канал', 'бот', 'Strike', 'прогрев'.",
    }


def get_action_keyboard(intent: dict) -> list:
    if intent["action"] == "unknown":
        return []
    actions = {
        "broadcast": [{"text": "📢 Новая рассылка", "action": "open_broadcast"}],
        "channel_manage": [{"text": "📡 Каналы", "action": "open_channels"}],
        "bot_manage": [{"text": "🤖 Боты", "action": "open_bots"}],
        "account_manage": [{"text": "📱 Аккаунты", "action": "open_accounts"}],
        "strike": [{"text": "⚔️ Strike", "action": "open_strike"}],
        "parse_audience": [{"text": "🔍 Парсер", "action": "open_parser"}],
        "warmup": [{"text": "🌱 Прогрев", "action": "open_warmup"}],
        "analytics": [{"text": "📊 Аналитика", "action": "open_analytics"}],
        "crm": [{"text": "📋 CRM", "action": "open_crm"}],
        "subscription": [{"text": "💳 Подписка", "action": "open_subscription"}],
    }
    return actions.get(intent["action"], [])
