"""AI Commenting — генерация и постинг КОНТЕКСТНЫХ комментариев под пост.

В отличие от прогревочного `activity_engine._act_comment` (случайные шаблоны из
`_COMMENT_TEXTS`), здесь комментарий генерирует LLM (`spintax_ai.complete`) по
тексту конкретного поста — естественный, на языке поста, по теме. Используется
для вовлечения/сигналов ранжирования в обсуждениях целевых каналов.

Границы: постинг в чужие обсуждения — рисковая массовая операция, поэтому идёт
через op_worker (лимиты/FloodWait/потоки) и content_safety-гард, как Growth Agent.
Отдельный движок (не трогаем activity_engine — зона account-ops).
"""
from __future__ import annotations

import asyncio
import logging
import random
import re

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15.0
_ACTION_TIMEOUT = 20.0
_MAX_COMMENT_LEN = 280

# Тон комментария → инструкция для LLM.
COMMENT_TONES = {
    "friendly": "дружелюбный, живой, как обычный участник",
    "expert": "экспертный, по делу, с конкретикой",
    "question": "в виде уместного вопроса по теме поста",
    "support": "поддерживающий, короткая позитивная реакция",
    "neutral": "нейтральный, естественный",
}


def build_comment_prompt(post_text: str, niche: str = "", tone: str = "neutral") -> tuple[str, str]:
    """Собрать (system, user) для LLM. Чистая функция — тестируется без сети.

    Комментарий должен быть коротким, на языке поста, по теме, без ссылок/хэштегов
    и без раскрытия, что это бот/реклама."""
    tone_instr = COMMENT_TONES.get(tone, COMMENT_TONES["neutral"])
    niche_ctx = f" Тематика сообщества: {niche.strip()}." if niche and niche.strip() else ""
    system = (
        "Ты — обычный живой участник Telegram-чата."
        f"{niche_ctx} Пиши ОДИН короткий комментарий (1–2 предложения) к посту ниже. "
        f"Тон: {tone_instr}. На ТОМ ЖЕ языке, что и пост. Без ссылок, без хэштегов, "
        "без упоминания рекламы/ботов/ИИ, без кавычек вокруг ответа. Только текст комментария."
    )
    user = f"Пост:\n{(post_text or '').strip()[:1500]}\n\nТвой комментарий:"
    return system, user


def sanitize_comment(text: str) -> str:
    """Почистить ответ LLM: убрать обрамляющие кавычки/префиксы, схлопнуть пробелы,
    обрезать по длине. Чистая функция."""
    t = (text or "").strip()
    # убрать обрамляющие кавычки
    if len(t) >= 2 and t[0] in "\"'«“" and t[-1] in "\"'»”":
        t = t[1:-1].strip()
    # убрать ведущие «Комментарий:», «Ответ:» и т.п.
    t = re.sub(r"^(комментарий|ответ|comment)\s*[:\-—]\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:_MAX_COMMENT_LEN]


async def generate_comment(post_text: str, niche: str = "", tone: str = "neutral") -> str:
    """Сгенерировать комментарий к посту через LLM. Пустая строка при сбое/пустоте."""
    from services import spintax_ai

    system, user = build_comment_prompt(post_text, niche, tone)
    try:
        raw = await spintax_ai.complete(system, user)
    except Exception as exc:
        log.warning("ai_comment generate failed: %s", exc)
        return ""
    return sanitize_comment(raw)


async def post_ai_comment(
    session_string: str,
    _acc: dict | None,
    channel_ref: str,
    niche: str = "",
    tone: str = "neutral",
    low_risk: bool = False,
) -> dict:
    """Подключиться, найти привязанную к каналу группу обсуждений, взять недавний
    пост, сгенерировать AI-комментарий и опубликовать его как ответ к посту.
    Возвращает {ok, comment, error}."""
    from telethon.tl.functions.channels import GetFullChannelRequest
    from services.account_manager import _make_client

    client = _make_client(session_string, _acc, low_risk=low_risk)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await asyncio.wait_for(client.get_entity(channel_ref), timeout=_ACTION_TIMEOUT)
        if not getattr(entity, "broadcast", False):
            return {"ok": False, "comment": None, "error": "не канал (нет обсуждений)"}
        full = await asyncio.wait_for(client(GetFullChannelRequest(entity)), timeout=_ACTION_TIMEOUT)
        linked_id = getattr(full.full_chat, "linked_chat_id", None)
        if not linked_id:
            return {"ok": False, "comment": None, "error": "у канала нет группы обсуждений"}
        msgs = await asyncio.wait_for(client.get_messages(entity, limit=15), timeout=_ACTION_TIMEOUT)
        candidates = [m for m in (msgs or []) if getattr(m, "replies", None) is not None
                      and (getattr(m, "message", "") or "").strip()]
        if not candidates:
            return {"ok": False, "comment": None, "error": "нет постов с обсуждением"}
        post = random.choice(candidates[:5])
        comment = await generate_comment(getattr(post, "message", "") or "", niche, tone)
        if not comment:
            return {"ok": False, "comment": None, "error": "LLM не дал комментарий (проверьте AI-ключи)"}
        discussion = await asyncio.wait_for(client.get_entity(linked_id), timeout=_ACTION_TIMEOUT)
        await asyncio.wait_for(
            client.send_message(discussion, comment, comment_to=post.id),
            timeout=_ACTION_TIMEOUT,
        )
        return {"ok": True, "comment": comment, "error": None}
    except Exception as exc:
        return {"ok": False, "comment": None, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
