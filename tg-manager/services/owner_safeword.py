"""Секретное слово владельца против фишинга (волна 4 «Защиты аккаунтов»).

Фишинг против владельца Infragram выглядит так: приходит сообщение «тревога
безопасности, перейдите по ссылке и подтвердите вход», похожее на наше. Наши
тревоги (вход с нового устройства, сброс пароля 2FA, слив данных) шлёт бот —
и подделать их вид несложно.

Слово — классическая защита от этого: владелец задаёт короткую фразу, которую
знает только он, и КАЖДАЯ наша тревога её показывает. Сообщение без слова —
подделка, по ссылке из него переходить нельзя. Поддельщик слова не знает.

Слово хранится как есть (его надо ПОКАЗАТЬ в сообщении — это не пароль, а
метка подлинности) и видно владельцу на экране «Мои входы». По желанию:
не задано — тревоги идут как раньше, а на «Защите» висит подсказка его задать.
"""
from __future__ import annotations

import html
import logging

log = logging.getLogger(__name__)

MAX_LEN = 48


def normalize_phrase(raw) -> str | None:
    """Слово — 2…48 видимых символов. Схлопываем пробелы; пусто/длинно → None."""
    s = " ".join(str(raw or "").split())
    return s if 2 <= len(s) <= MAX_LEN else None


def alert_suffix(phrase: str | None) -> str:
    """Хвост к тревоге: «🔑 Ваше слово: X». Пустая фраза — пустой хвост.

    Чистая функция: сайт отправки достаёт фразу (get) и добавляет этот хвост к
    собранному тексту. Так build_alert остаётся без БД и проверяется отдельно.
    """
    if not phrase:
        return ""
    return f"\n\n🔑 Ваше слово: <b>{html.escape(phrase)}</b>\nСообщения без него — подделка."


# ── База ─────────────────────────────────────────────────────────────────────

async def get(pool, owner_id: int) -> str | None:
    try:
        return await pool.fetchval(
            "SELECT phrase FROM owner_safeword WHERE owner_id=$1", int(owner_id))
    except Exception as exc:
        log.warning("owner_safeword.get owner=%s: %s", owner_id, exc)
        return None


async def line(pool, owner_id: int) -> str:
    """Готовый хвост к тревоге для этого владельца (пустой, если слова нет)."""
    return alert_suffix(await get(pool, owner_id))


async def set_phrase(pool, owner_id: int, raw) -> dict:
    phrase = normalize_phrase(raw)
    if not phrase:
        return {"ok": False, "error": "Слово — от 2 до 48 символов"}
    try:
        await pool.execute(
            """INSERT INTO owner_safeword(owner_id, phrase, updated_at)
               VALUES($1, $2, now())
               ON CONFLICT (owner_id) DO UPDATE SET phrase=$2, updated_at=now()""",
            int(owner_id), phrase)
    except Exception as exc:
        log.warning("owner_safeword.set owner=%s: %s", owner_id, exc)
        return {"ok": False, "error": "Не удалось сохранить слово"}
    return {"ok": True, "phrase": phrase}


async def clear(pool, owner_id: int) -> dict:
    try:
        await pool.execute("DELETE FROM owner_safeword WHERE owner_id=$1", int(owner_id))
    except Exception as exc:
        log.warning("owner_safeword.clear owner=%s: %s", owner_id, exc)
        return {"ok": False, "error": "Не удалось снять слово"}
    return {"ok": True}


async def suggested(pool, owner_id: int) -> bool:
    """Подсказать задать слово: есть аккаунты, слова ещё нет."""
    try:
        if await get(pool, owner_id):
            return False
        return bool(await pool.fetchval(
            "SELECT 1 FROM tg_accounts WHERE owner_id=$1 LIMIT 1", int(owner_id)))
    except Exception as exc:
        log.warning("owner_safeword.suggested owner=%s: %s", owner_id, exc)
        return False
