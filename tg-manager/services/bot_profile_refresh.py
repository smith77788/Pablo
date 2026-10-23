"""Имя и @username бота приводятся к тому, что сейчас в Telegram.

Разрыв, который это закрывает
-----------------------------
`managed_bots.first_name` и `managed_bots.username` записывались ОДИН раз — при
подключении бота, из `getMe`. Больше их не трогал никто. Переименовали бота в
@BotFather (а имя бота меняется именно там, не через Infragram) — и список
ботов навсегда показывал старое имя. То же со ссылкой: смена @username делала
подпись на карточке неверной, и перейти по ней было нельзя.

Почему это чинится отдельным проходом, а не «при случае»
-------------------------------------------------------
`getMe` — самый дешёвый метод Bot API и единственный источник правды об имени.
Опрашивать его на каждом круге поллинга незачем: имя меняется раз в месяцы.
Поэтому здесь — редкий фоновый проход: бот, которого не спрашивали больше
суток, опрашивается снова, пачкой не больше нескольких десятков за раз.

Рамки
-----
* **Ничего не меняем в Telegram.** `getMe` только читает.
* Бот с отозванным токеном молча пропускается: его поломкой занимается
  `auto_responder`/`bot_healer`, дублировать эскалацию здесь незачем.
* Fail-open: сбой на одном боте не мешает остальным и не роняет вызывающий цикл.
* Решение «что записать» — чистая функция `changed_fields`, её и проверяют
  тесты; сеть и база — тонкие обёртки вокруг неё.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Как часто перепроверять имя бота. Имя меняют редко, а getMe дешёвый, но
# бессмысленный запрос — это всё равно запрос.
REFRESH_AFTER_HOURS = 24
# Сколько ботов за один проход. При 82 ботах полный круг занимает три прохода —
# то есть меньше часа, а не «никогда», как было.
BATCH = 40


def changed_fields(stored: dict, fresh: dict | None) -> dict:
    """Что именно изменилось в профиле бота. Пусто — писать нечего.

    Чистая функция: сюда приходит строка из базы и ответ getMe, отсюда выходит
    словарь колонок под запись.

    Пустое значение от Telegram НЕ затирает известное: getMe всегда отдаёт и
    first_name, и username, но подстраховка дешевле, чем список ботов,
    обнулённый одним неудачным ответом. Исключение — username: его можно снять,
    и пустой username от живого бота это правда... но у ботов @username снять
    нельзя в принципе, поэтому и здесь пустое не пишем.
    """
    if not isinstance(fresh, dict) or not fresh:
        return {}
    out: dict = {}
    new_name = str(fresh.get("first_name") or "").strip()
    new_user = str(fresh.get("username") or "").strip().lstrip("@")
    old_name = str((stored or {}).get("first_name") or "").strip()
    old_user = str((stored or {}).get("username") or "").strip().lstrip("@")
    if new_name and new_name != old_name:
        out["first_name"] = new_name
    if new_user and new_user != old_user:
        out["username"] = new_user
    return out


async def _due_bots(pool, limit: int = BATCH) -> list[dict]:
    """Боты, чей профиль давно не сверяли. Fail-open: нет колонки — нет прохода."""
    try:
        rows = await pool.fetch(
            """SELECT bot_id, token, first_name, username
                 FROM managed_bots
                WHERE is_active = TRUE
                  AND token IS NOT NULL
                  AND (profile_checked_at IS NULL
                       OR profile_checked_at < now() - ($1 || ' hours')::interval)
                ORDER BY profile_checked_at NULLS FIRST, bot_id
                LIMIT $2""",
            str(REFRESH_AFTER_HOURS), limit,
        )
        return [dict(r) for r in (rows or [])]
    except Exception as e:
        log.debug("bot_profile_refresh: выборка не удалась: %s", e)
        return []


async def _stamp(pool, bot_id: int, fields: dict) -> None:
    """Записать изменения и отметку сверки одним запросом.

    Отметка ставится ВСЕГДА, даже когда ничего не поменялось: без неё один и тот
    же бот попадал бы в каждую выборку и вытеснял остальных.
    """
    sets = ["profile_checked_at = now()"]
    args: list = [bot_id]
    for col in ("first_name", "username"):
        if col in fields:
            args.append(fields[col])
            sets.append(f"{col} = ${len(args)}")
    try:
        await pool.execute(
            f"UPDATE managed_bots SET {', '.join(sets)} WHERE bot_id = $1", *args
        )
    except Exception as e:
        log.debug("bot_profile_refresh: бот %s не обновлён: %s", bot_id, e)


async def refresh_bot_profiles(pool, http) -> int:
    """Один проход сверки. Возвращает число ботов, у которых профиль изменился."""
    from services import bot_api

    changed = 0
    for row in await _due_bots(pool):
        token = row.get("token")
        if not token:
            continue
        try:
            fresh = await bot_api.get_me(http, token)
        except Exception as e:
            log.debug("bot_profile_refresh: getMe бота %s не прошёл: %s", row["bot_id"], e)
            fresh = None
        if fresh is None:
            # Токен отозван или Telegram недоступен. Эскалация — не наша забота,
            # но отметку ставим: иначе мёртвый бот займёт выборку навсегда.
            await _stamp(pool, int(row["bot_id"]), {})
            continue
        fields = changed_fields(row, fresh)
        await _stamp(pool, int(row["bot_id"]), fields)
        if fields:
            changed += 1
            log.info(
                "bot_profile_refresh: бот %s обновлён: %s",
                row["bot_id"], ", ".join(sorted(fields)),
            )
    return changed
