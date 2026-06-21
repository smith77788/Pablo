"""Narrative Engine — coordinated cross-network content campaigns for trend creation."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import asyncpg
from aiogram import Bot

from services import bot_api
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# Углы подачи материала по индексу канала
_ANGLES = [
    "news",      # новостной
    "expert",    # экспертный комментарий
    "story",     # личная история / кейс
    "stats",     # статистика и факты
    "opinion",   # мнение / колонка
    "question",  # вопрос / дискуссия
    "review",    # обзор / анализ
    "trend",     # тренд / прогноз
]

_ANGLE_LABELS = {
    "news":     "Новостной",
    "expert":   "Экспертный",
    "story":    "История/Кейс",
    "stats":    "Статистика",
    "opinion":  "Мнение",
    "question": "Вопрос",
    "review":   "Обзор",
    "trend":    "Тренд",
}

_TYPE_LABELS = {
    "trend":     "Создание тренда",
    "launch":    "Запуск продукта",
    "awareness": "Повышение осведомлённости",
    "counter":   "Контр-нарратив",
}

_STATUS_LABELS = {
    "draft":     "Черновик",
    "active":    "Активна",
    "paused":    "Пауза",
    "completed": "Завершена",
    "cancelled": "Отменена",
}

_POST_STATUS_LABELS = {
    "pending":   "⏳ Ожидает",
    "published": "✅ Опубликован",
    "failed":    "❌ Ошибка",
    "cancelled": "🚫 Отменён",
}


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class NarrativeCampaign:
    id: int
    owner_id: int
    topic: str
    core_message: str
    campaign_type: str = "trend"   # trend | launch | awareness | counter
    channels: list[str] = field(default_factory=list)
    posts_count: int = 0
    spread_hours: int = 4
    status: str = "draft"          # draft | active | paused | completed | cancelled
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ── AI generation ─────────────────────────────────────────────────────────────


def _build_angle_prompt(
    topic: str,
    core_message: str,
    angle: str,
    campaign_type: str,
    channel_index: int,
    total_channels: int,
) -> str:
    """Строит промпт для конкретного угла подачи."""
    type_context = {
        "trend":     "создаём органический тренд вокруг темы",
        "launch":    "анонсируем запуск нового продукта/события",
        "awareness": "повышаем осведомлённость о важной теме",
        "counter":   "распространяем альтернативный взгляд на ситуацию",
    }.get(campaign_type, "работаем с темой")

    angle_instruction = {
        "news":     "Напиши как новостную заметку: факт, событие, краткий контекст. Без личных оценок, нейтральный тон, как у новостного канала.",
        "expert":   "Напиши как экспертный комментарий от лица специалиста. Используй профессиональную терминологию, дай глубокий анализ.",
        "story":    "Напиши как личную историю или реальный кейс. 'Наш клиент / знакомый / мы сами столкнулись с...' — живой нарратив, эмоции.",
        "stats":    "Напиши с упором на цифры и статистику. Приведи конкретные данные, проценты, сравнения. Источники можно упоминать обобщённо.",
        "opinion":  "Напиши как авторскую колонку или мнение редакции. Личная позиция, аргументы, призыв к размышлению.",
        "question": "Задай вопрос аудитории, открой дискуссию. Поставь проблему, пригласи к обсуждению в комментариях.",
        "review":   "Напиши как аналитический обзор: за и против, плюсы и минусы, итоговый вердикт.",
        "trend":    "Напиши про тренд и будущее. Что сейчас происходит, куда движется рынок/ситуация, что ждать дальше.",
    }.get(angle, "Напиши интересный пост по теме.")

    return (
        f"Ты — автор Telegram-канала. Мы {type_context}.\n\n"
        f"Тема: {topic}\n"
        f"Ключевое сообщение: {core_message}\n\n"
        f"Твоя задача — пост #{channel_index + 1} из {total_channels} в координированной кампании.\n"
        f"Угол подачи: {_ANGLE_LABELS.get(angle, angle)}\n\n"
        f"{angle_instruction}\n\n"
        f"Требования:\n"
        f"- Длина: 150–400 символов (оптимально для Telegram)\n"
        f"- Тон должен отличаться от других каналов кампании\n"
        f"- Не упоминай, что это часть кампании или что другие каналы тоже об этом пишут\n"
        f"- Используй подходящие эмодзи\n"
        f"- Только текст поста, без пояснений и комментариев\n"
    )


async def _call_ai(prompt: str, ai_provider) -> str:
    """Вызов AI с fallback на заглушку если провайдер не задан."""
    if ai_provider is None:
        return f"[Пост будет сгенерирован AI при наличии ключей]\n\n{prompt[:100]}..."

    try:
        import openai  # type: ignore
        client = openai.AsyncOpenAI(
            api_key=ai_provider.api_key,
            base_url=ai_provider.base_url,
        )
        model = ai_provider.models[0] if ai_provider.models else "gpt-3.5-turbo"
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.85,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("narrative_engine AI call failed: %s", e)
        return f"[AI недоступен: {type(e).__name__}]\n\nТема: {prompt[:80]}..."


async def generate_campaign_posts(
    topic: str,
    core_message: str,
    channels_count: int,
    spread_hours: int,
    ai_provider,
) -> list[dict]:
    """
    Генерирует channels_count разных постов на одну тему с разных углов.

    Возвращает список dict с ключами: angle, content, scheduled_offset_minutes
    """
    if channels_count <= 0:
        return []

    # Распределяем углы между каналами
    angles = [_ANGLES[i % len(_ANGLES)] for i in range(channels_count)]

    # Интервал между постами (равномерно в течение spread_hours)
    total_minutes = spread_hours * 60
    if channels_count > 1:
        interval_minutes = total_minutes // channels_count
    else:
        interval_minutes = 0

    # Генерируем посты параллельно
    tasks = []
    for i, angle in enumerate(angles):
        prompt = _build_angle_prompt(
            topic=topic,
            core_message=core_message,
            angle=angle,
            campaign_type="trend",
            channel_index=i,
            total_channels=channels_count,
        )
        tasks.append(_call_ai(prompt, ai_provider))

    contents = await asyncio.gather(*tasks, return_exceptions=True)

    posts = []
    for i, (angle, content) in enumerate(zip(angles, contents)):
        if isinstance(content, Exception):
            log.warning("narrative_engine post %d generation error: %s", i, content)
            content = f"[Ошибка генерации: {type(content).__name__}]"
        posts.append({
            "angle": angle,
            "content": str(content),
            "scheduled_offset_minutes": i * interval_minutes,
        })

    return posts


# ── DB operations ─────────────────────────────────────────────────────────────


async def create_campaign(
    pool: asyncpg.Pool,
    owner_id: int,
    topic: str,
    core_message: str,
    channel_usernames: list[str],
    spread_hours: int,
    campaign_type: str,
    ai_provider=None,
    bot_ids: list[int] | None = None,
) -> int:
    """
    Создаёт кампанию, генерирует посты и планирует время публикации.

    Возвращает ID созданной кампании.
    """
    if not channel_usernames:
        raise ValueError("Нужен хотя бы один канал")

    if bot_ids is None:
        bot_ids = [None] * len(channel_usernames)
    # Дополняем bot_ids если короче
    while len(bot_ids) < len(channel_usernames):
        bot_ids.append(None)

    # Генерация постов
    posts_data = await generate_campaign_posts(
        topic=topic,
        core_message=core_message,
        channels_count=len(channel_usernames),
        spread_hours=spread_hours,
        ai_provider=ai_provider,
    )

    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Создаём кампанию
            campaign_id = await conn.fetchval(
                """INSERT INTO narrative_campaigns
                   (owner_id, topic, core_message, campaign_type, spread_hours,
                    posts_total, status, started_at)
                   VALUES ($1, $2, $3, $4, $5, $6, 'active', NOW())
                   RETURNING id""",
                owner_id, topic, core_message, campaign_type,
                spread_hours, len(channel_usernames),
            )

            # Создаём посты
            for i, (username, post) in enumerate(zip(channel_usernames, posts_data)):
                scheduled_at = now + timedelta(
                    minutes=post["scheduled_offset_minutes"]
                )
                bot_id = bot_ids[i] if i < len(bot_ids) else None
                await conn.execute(
                    """INSERT INTO narrative_posts
                       (campaign_id, owner_id, channel_username, bot_id, angle,
                        content, scheduled_at, status)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')""",
                    campaign_id, owner_id, username, bot_id,
                    post["angle"], post["content"], scheduled_at,
                )

    log.info(
        "narrative_engine: created campaign %d for user %d, %d posts, spread %dh",
        campaign_id, owner_id, len(channel_usernames), spread_hours,
    )
    return campaign_id


async def get_campaign(pool: asyncpg.Pool, campaign_id: int, owner_id: int) -> dict | None:
    """Возвращает кампанию по ID с проверкой владельца."""
    row = await pool.fetchrow(
        "SELECT * FROM narrative_campaigns WHERE id=$1 AND owner_id=$2",
        campaign_id, owner_id,
    )
    return dict(row) if row else None


async def get_campaign_status(pool: asyncpg.Pool, campaign_id: int) -> dict:
    """
    Возвращает прогресс кампании: опубликовано X/N постов + детали каждого поста.
    """
    campaign = await pool.fetchrow(
        "SELECT * FROM narrative_campaigns WHERE id=$1",
        campaign_id,
    )
    if not campaign:
        return {"error": "Кампания не найдена"}

    posts = await pool.fetch(
        """SELECT id, channel_username, angle, status, scheduled_at, published_at, error_text
           FROM narrative_posts WHERE campaign_id=$1 ORDER BY scheduled_at""",
        campaign_id,
    )

    published = sum(1 for p in posts if p["status"] == "published")
    pending = sum(1 for p in posts if p["status"] == "pending")
    failed = sum(1 for p in posts if p["status"] == "failed")
    total = campaign["posts_total"] or len(posts)

    return {
        "campaign": dict(campaign),
        "posts": [dict(p) for p in posts],
        "published": published,
        "pending": pending,
        "failed": failed,
        "total": total,
        "progress_pct": round(published / total * 100) if total else 0,
    }


async def list_campaigns(
    pool: asyncpg.Pool,
    owner_id: int,
    status_filter: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Список кампаний пользователя."""
    if status_filter:
        rows = await pool.fetch(
            """SELECT * FROM narrative_campaigns
               WHERE owner_id=$1 AND status=$2
               ORDER BY created_at DESC LIMIT $3""",
            owner_id, status_filter, limit,
        )
    else:
        rows = await pool.fetch(
            """SELECT * FROM narrative_campaigns
               WHERE owner_id=$1
               ORDER BY created_at DESC LIMIT $2""",
            owner_id, limit,
        )
    return [dict(r) for r in rows]


async def get_campaign_posts(pool: asyncpg.Pool, campaign_id: int) -> list[dict]:
    """Список постов кампании."""
    rows = await pool.fetch(
        """SELECT * FROM narrative_posts
           WHERE campaign_id=$1 ORDER BY scheduled_at""",
        campaign_id,
    )
    return [dict(r) for r in rows]


async def pause_campaign(pool: asyncpg.Pool, campaign_id: int, owner_id: int) -> bool:
    """Ставит кампанию на паузу (pending посты остаются запланированными)."""
    row = await pool.fetchrow(
        """UPDATE narrative_campaigns SET status='paused'
           WHERE id=$1 AND owner_id=$2 AND status='active'
           RETURNING id""",
        campaign_id, owner_id,
    )
    return bool(row)


async def resume_campaign(pool: asyncpg.Pool, campaign_id: int, owner_id: int) -> bool:
    """Возобновляет паузу кампании."""
    row = await pool.fetchrow(
        """UPDATE narrative_campaigns SET status='active'
           WHERE id=$1 AND owner_id=$2 AND status='paused'
           RETURNING id""",
        campaign_id, owner_id,
    )
    return bool(row)


async def cancel_campaign(pool: asyncpg.Pool, campaign_id: int, owner_id: int) -> bool:
    """Отменяет кампанию и все pending посты."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE narrative_campaigns SET status='cancelled', completed_at=NOW()
                   WHERE id=$1 AND owner_id=$2 AND status IN ('draft','active','paused')
                   RETURNING id""",
                campaign_id, owner_id,
            )
            if not row:
                return False
            await conn.execute(
                """UPDATE narrative_posts SET status='cancelled'
                   WHERE campaign_id=$1 AND status='pending'""",
                campaign_id,
            )
    return True


# ── Execution loop ────────────────────────────────────────────────────────────


async def _publish_post(pool: asyncpg.Pool, http: aiohttp.ClientSession, post: dict) -> tuple[bool, str]:
    """
    Публикует один пост в канал через managed bot (по bot_id из поста).
    Возвращает (success, error_text).
    """
    channel = post["channel_username"]
    content = post["content"]
    bot_id = post.get("bot_id")

    if not bot_id:
        return False, "bot_id не задан для поста"

    try:
        from database.db import fetchrow_bot as _fetchrow_bot
        bot_row = await _fetchrow_bot(
            pool, "SELECT token FROM managed_bots WHERE bot_id=$1 AND is_active=TRUE", bot_id
        )
        if not bot_row:
            return False, f"managed bot {bot_id} не найден или неактивен"

        if not channel.startswith("@") and not channel.startswith("-"):
            channel = f"@{channel}"

        ok, retry = await bot_api.send_message(http, bot_row["token"], channel, content)
        if ok:
            return True, ""
        return False, f"Telegram API error (retry={retry})"
    except Exception as e:
        err = str(e)
        log.warning("narrative_engine: failed to publish post %d to %s: %s", post["id"], channel, err)
        return False, err[:512]


async def execute_pending_posts(pool: asyncpg.Pool, bot: Bot) -> int:
    """
    Публикует посты, время которых пришло (scheduled_at <= NOW()).
    Работает только для кампаний со статусом 'active'.
    Возвращает количество опубликованных постов.
    """
    # Берём pending посты активных кампаний
    posts = await pool.fetch(
        """SELECT np.*, nc.status AS campaign_status
           FROM narrative_posts np
           JOIN narrative_campaigns nc ON nc.id = np.campaign_id
           WHERE np.status = 'pending'
             AND np.scheduled_at <= NOW()
             AND nc.status = 'active'
           ORDER BY np.scheduled_at
           LIMIT 50""",
    )

    if not posts:
        return 0

    async with aiohttp.ClientSession() as http:
        return await _execute_with_session(pool, http, posts)


async def _execute_with_session(
    pool: asyncpg.Pool, http: aiohttp.ClientSession, posts: list
) -> int:
    published_count = 0
    for post in posts:
        post_dict = dict(post)
        success, error = await _publish_post(pool, http, post_dict)

        if success:
            await pool.execute(
                """UPDATE narrative_posts
                   SET status='published', published_at=NOW()
                   WHERE id=$1""",
                post_dict["id"],
            )
            await pool.execute(
                """UPDATE narrative_campaigns
                   SET posts_published = posts_published + 1
                   WHERE id=$1""",
                post_dict["campaign_id"],
            )
            published_count += 1
        else:
            await pool.execute(
                """UPDATE narrative_posts
                   SET status='failed', error_text=$2
                   WHERE id=$1""",
                post_dict["id"], error,
            )

        # Проверяем завершение кампании
        await _check_campaign_completion(pool, post_dict["campaign_id"])

        # Небольшая задержка между публикациями
        await asyncio.sleep(2)

    return published_count


async def _check_campaign_completion(pool: asyncpg.Pool, campaign_id: int) -> None:
    """Помечает кампанию как завершённую если все посты обработаны."""
    row = await pool.fetchrow(
        """SELECT
               COUNT(*) FILTER (WHERE status='pending') AS pending_cnt,
               COUNT(*) FILTER (WHERE status='published') AS pub_cnt,
               COUNT(*) AS total_cnt
           FROM narrative_posts WHERE campaign_id=$1""",
        campaign_id,
    )
    if not row:
        return

    if row["pending_cnt"] == 0 and row["total_cnt"] > 0:
        await pool.execute(
            """UPDATE narrative_campaigns
               SET status='completed', completed_at=NOW(),
                   posts_published=$2
               WHERE id=$1 AND status='active'""",
            campaign_id, row["pub_cnt"],
        )
        log.info("narrative_engine: campaign %d completed (%d published)", campaign_id, row["pub_cnt"])


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Фоновый цикл: каждые 15 минут публикует посты чьё время пришло."""
    log.info("narrative_engine: background loop started")
    while True:
        try:
            published = await execute_pending_posts(pool, bot)
            if published:
                log.info("narrative_engine: published %d narrative posts", published)
        except Exception as e:
            log_exc_swallow(log, f"narrative_engine loop error: {e}")
        await asyncio.sleep(15 * 60)  # 15 минут
