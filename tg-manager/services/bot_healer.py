"""Само-лечение сети ботов — вернуть молчащего бота в строй до звонка владельцу.

`auto_responder` уже отличает «токен отозван» (зовите человека) от «конфликт
получения обновлений» и раз в ~5 минут пишет владельцу о сломанных. Но одну
поломку продукт умеет чинить сам, а звал человека зря: КОНФЛИКТ обновлений —
это почти всегда зависший вебхук. Управляемые боты здесь работают на polling,
а не на вебхуке (об этом прямо сказано в экране вебхука), и снять зависший
вебхук — санкционированное действие: тот же `deleteWebhook` продукт делает
руками из меню бота и автоматически при импорте бота.

Самолечение делает ровно это, но САМО и ДО эскалации: перед проходом
`notify_broken_bots` смотрим ботов в состоянии CONFLICT, спрашиваем
`getWebhookInfo`; если вебхук РЕАЛЬНО установлен — снимаем его и возвращаем бота
на polling, сбрасывая счётчик поломок. Владелец при этом ничего не видит —
поломка устранена раньше, чем о ней бы сообщили.

Осознанные рамки, чтобы не трогать чужое:
* лечим ТОЛЬКО CONFLICT — единственный класс, который снимается вебхуком;
  отозванный токен (UNAUTHORIZED) и флуд так не чинятся и остаются человеку;
* снимаем вебхук ТОЛЬКО если он реально стоит. Конфликт без вебхука — это второй
  поллер (другая копия), его `deleteWebhook` не чинит, и мы не мутируем зря;
* решение (лечить/нет и чем) — чистая функция, её и проверяют тесты; сеть и БД —
  тонкие обёртки. Fail-open: сбой лечения не роняет цикл и не мешает эскалации.
"""
from __future__ import annotations

import logging

from services.bot_health import CONFLICT

log = logging.getLogger(__name__)

# План лечения по типу ошибки.
HEAL_DELETE_WEBHOOK = "delete_webhook"


def plan_recovery(kind: str, webhook_url: str | None) -> str | None:
    """Что делать с молчащим ботом. Чистая функция — сердце решения.

    Возвращает код действия или None (само не лечится / нечего снимать).
    """
    if (kind or "").lower() != CONFLICT:
        return None
    # Вебхук снимаем, только если он реально установлен. Пустой url при конфликте
    # = второй getUpdates-поллер, вебхук тут ни при чём — не мутируем.
    if webhook_url and str(webhook_url).strip():
        return HEAL_DELETE_WEBHOOK
    return None


# ── Обёртки над сетью и БД (тонкие; логика — выше) ──────────────────────────

async def _broken_conflict_bots(pool) -> list[dict]:
    """Активные боты в состоянии CONFLICT, о которых ещё НЕ сообщили владельцу
    (лечим до эскалации). Токен нужен для вызова Bot API."""
    try:
        rows = await pool.fetch(
            """SELECT bot_id, added_by, token, username, first_name, fail_streak
                 FROM managed_bots
                WHERE is_active AND fail_streak > 0
                  AND lower(last_error) = $1
                  AND dead_notified_at IS NULL
                LIMIT 50""", CONFLICT)
        return [dict(r) for r in (rows or [])]
    except Exception as e:
        log.debug("bot_healer: выборка не удалась: %s", e)
        return []


async def _mark_healed(pool, bot_id: int) -> None:
    """Снять жалобу так же, как успешный опрос: счётчик, ошибка, метки — в ноль."""
    try:
        await pool.execute(
            """UPDATE managed_bots
                  SET fail_streak=0, last_error=NULL, last_ok_at=now(),
                      dead_notified_at=NULL
                WHERE bot_id=$1""", bot_id)
    except Exception:
        log.debug("bot_healer: отметка лечения бота %s не записана", bot_id)


async def heal_broken_bots(pool, http) -> int:
    """Один проход самолечения. Возвращает число вернувшихся в строй ботов.

    Fail-open: сбой на одном боте не мешает остальным и не роняет вызывающий цикл.
    """
    from services import bot_api
    healed = 0
    for r in await _broken_conflict_bots(pool):
        token = r.get("token")
        if not token:
            continue
        try:
            info = await bot_api.get_webhook_info(http, token)
        except Exception:
            log.debug("bot_healer: getWebhookInfo failed bot=%s", r["bot_id"])
            continue
        plan = plan_recovery(CONFLICT, (info or {}).get("url"))
        if plan != HEAL_DELETE_WEBHOOK:
            continue                       # конфликт без вебхука — не наше лечение
        try:
            res = await bot_api.delete_webhook(http, token)
        except Exception:
            log.debug("bot_healer: deleteWebhook failed bot=%s", r["bot_id"])
            continue
        if not (res or {}).get("ok"):
            continue
        await _mark_healed(pool, r["bot_id"])
        healed += 1
        try:
            from services.organism import spine
            await spine.emit(pool, r.get("added_by"), "bot_self_healed",
                             {"bot_id": r["bot_id"], "action": plan})
        except Exception:
            pass
        log.info("bot_healer: бот %s возвращён в строй (снят зависший вебхук)",
                 r["bot_id"])
    return healed
