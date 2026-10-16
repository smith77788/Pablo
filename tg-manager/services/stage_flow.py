"""Авто-переходы CRM-стадий аккаунта (tg_accounts.stage).

Чистая логика принятия решения о новой стадии на событиях жизненного цикла
аккаунта. Модуль СПЕЦИАЛЬНО не имеет тяжёлых импортов (нет asyncpg / telethon /
aiohttp), чтобы решение можно было тестировать в песочнице без БД и сети.

Событийная воронка:
  - 'warmup_start' — стартовал/возобновлён план прогрева.
  - 'warmup_done'  — план прогрева дошёл до конца (успешно завершён).
  - 'banned'       — проверка здоровья пометила аккаунт как мёртвый
                     (banned / deactivated / session_expired).

Ключевое правило: авто-переход НЕ должен перетирать вручную выставленные
стадии (frozen / reserve / in_work и т.п.).
"""

from __future__ import annotations

# Локальная копия whitelist допустимых стадий из services.mini_app_api.ACCOUNT_STAGES.
# Дублируем намеренно, чтобы не создавать перекрёстную зависимость от тяжёлого
# модуля mini_app_api (там aiohttp и пр.). Если whitelist там меняется —
# синхронизировать вручную.
ACCOUNT_STAGES: tuple[str, ...] = (
    "new",
    "warming",
    "ready",
    "in_work",
    "resting",
    "frozen",
    "reserve",
)

# Допустимые имена событий жизненного цикла.
STAGE_EVENTS: tuple[str, ...] = ("warmup_start", "warmup_done", "banned")


def next_stage_on_event(current_stage: str | None, event: str) -> str | None:
    """Вернуть новую стадию для аккаунта или None, если менять не нужно.

    Args:
        current_stage: текущее значение tg_accounts.stage (может быть None/'' —
            пустая стадия трактуется как «ещё не размечен»).
        event: одно из STAGE_EVENTS.

    Returns:
        Новую стадию (str из ACCOUNT_STAGES) либо None, если переход не
        применяется (в т.ч. чтобы не перетирать ручные стадии).
    """
    cur = (current_stage or "").strip().lower()

    if event == "warmup_done":
        # Прогрев завершён → аккаунт готов к работе, но только если стадия
        # пуста или входит в естественный преджизненный набор {new, warming}.
        # Ручные frozen/reserve/in_work/resting НЕ трогаем.
        if cur in ("", "new", "warming"):
            return "ready"
        return None

    if event == "warmup_start":
        # Старт прогрева двигает только «сырой» аккаунт (пусто или new).
        if cur in ("", "new"):
            return "warming"
        return None

    if event == "banned":
        # Мёртвый аккаунт замораживаем всегда, кроме случая, когда он уже frozen
        # (идемпотентность + не плодим лишние записи).
        if cur == "frozen":
            return None
        return "frozen"

    # Неизвестное событие — ничего не делаем.
    return None


def stage_sources_for_event(event: str) -> tuple[str | None, list[str]]:
    """Разложить событие на (целевая_стадия, список_исходных_стадий).

    Используется для построения ОДНОГО идемпотентного SQL-UPDATE вида
    ``SET stage=<target> WHERE ... AND текущая_стадия IN (<sources>)`` без
    предварительного чтения строки. Единый источник истины — next_stage_on_event:
    перебираем все возможные текущие стадии и собираем те, из которых событие
    ведёт в целевую стадию.

    Пустая стадия кодируется как '' (в SQL сопоставляется с NULL и '').

    Все три поддерживаемых события ведут в ЕДИНСТВЕННУЮ целевую стадию, поэтому
    возвращаем один target. Если событие неизвестно — (None, []).
    """
    candidates: list[str] = [""] + list(ACCOUNT_STAGES)
    target: str | None = None
    sources: list[str] = []
    for cur in candidates:
        nxt = next_stage_on_event(cur, event)
        if nxt is None:
            continue
        if target is None:
            target = nxt
        elif target != nxt:
            # Защита от будущих событий с несколькими целями: одиночный UPDATE
            # тогда некорректен — сигнализируем отказом.
            raise ValueError(
                f"событие {event!r} ведёт в несколько стадий "
                f"({target!r} и {nxt!r}); нужен отдельный UPDATE на стадию"
            )
        sources.append(cur)
    return target, sources
