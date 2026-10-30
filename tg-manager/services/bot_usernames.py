"""Username'ы ботов: основной «…bot» и дополнительный с новым суффиксом.

С октября 2026 Telegram разрешил боту второй, дополнительный @username с
окончанием agent / ai / app / game / gay (только Telegram Premium у владельца
бота). Правила, которые держим здесь, чтобы весь код проверял одинаково:

  * основной username по-прежнему обязан оканчиваться на «bot» — его требует
    /newbot у @BotFather и ссылка Manager Mode, поэтому создание ботов
    валидирует ТОЛЬКО основной;
  * дополнительный — один на бота, не заменяет основной; у владельца-аккаунта
    таких не больше двух (лимит держит Telegram, мы лишь подсказываем);
  * «gay» Telegram не показывает в выпадающем списке, но принимает вручную —
    поэтому принимаем его в проверке, а в подсказках интерфейса не предлагаем.

Распознавание «это username бота» (разбор кнопок /mybots, ввод оператора)
принимает оба вида, иначе бот, у которого виден дополнительный username,
выпадал бы как «не бот».
"""
from __future__ import annotations

import re

MAIN_SUFFIX = "bot"
# Порядок = порядок показа в интерфейсе.
EXTRA_SUFFIXES: tuple[str, ...] = ("app", "ai", "agent", "game", "gay")
UI_EXTRA_SUFFIXES: tuple[str, ...] = ("app", "ai", "agent", "game")
EXTRA_PER_BOT = 1
EXTRA_PER_ACCOUNT = 2

_CHARSET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def normalize(username: str | None) -> str:
    """Убрать пробелы, «@» и ссылку t.me/ — без смены регистра."""
    u = (username or "").strip()
    for pfx in ("https://", "http://"):
        if u.lower().startswith(pfx):
            u = u[len(pfx):]
    if u.lower().startswith(("t.me/", "telegram.me/")):
        u = u.split("/", 1)[1].split("?", 1)[0].split("/", 1)[0]
    return u.lstrip("@").strip()


def _charset_error(u: str) -> str | None:
    if not u:
        return "пустой username"
    if len(u) < 5 or len(u) > 32:
        return "username должен быть 5–32 символа"
    if not _CHARSET_RE.match(u):
        return ("username может содержать только латиницу, цифры и «_», "
                "начинаться с буквы")
    return None


def extra_suffix_of(username: str | None) -> str | None:
    """Новый суффикс username'а (app/ai/…) или None.

    «…bot» — всегда основной, даже если перед ним стоит «ai» («mainaibot»).
    """
    low = normalize(username).lower()
    if not low or low.endswith(MAIN_SUFFIX):
        return None
    # Длинные первыми: «…agent» не должен совпасть с чем-то короче по ошибке.
    for suf in sorted(EXTRA_SUFFIXES, key=len, reverse=True):
        if low.endswith(suf) and len(low) > len(suf):
            return suf
    return None


def is_bot_username(username: str | None) -> bool:
    """Похоже ли на username бота: «…bot» или «…app/ai/agent/game/gay»."""
    u = normalize(username)
    if _charset_error(u):
        return False
    return u.lower().endswith(MAIN_SUFFIX) or extra_suffix_of(u) is not None


def validate_main(username: str | None) -> tuple[bool, str]:
    """Основной username создаваемого бота. (True, norm) | (False, причина)."""
    u = normalize(username)
    err = _charset_error(u)
    if err:
        return False, err
    if not u.lower().endswith(MAIN_SUFFIX):
        if extra_suffix_of(u):
            return False, ("основной username бота по-прежнему должен "
                           "оканчиваться на «bot»; «" + extra_suffix_of(u)
                           + "» задаётся вторым, дополнительным username'ом")
        return False, "username должен заканчиваться на «bot»"
    return True, u


def validate_extra(username: str | None) -> tuple[bool, str]:
    """Дополнительный username с новым суффиксом. (True, norm) | (False, причина)."""
    u = normalize(username)
    err = _charset_error(u)
    if err:
        return False, err
    if u.lower().endswith(MAIN_SUFFIX):
        return False, ("это основной username («…bot»); дополнительный "
                       "должен оканчиваться на " + ", ".join(UI_EXTRA_SUFFIXES))
    if not extra_suffix_of(u):
        return False, ("дополнительный username должен оканчиваться на "
                       + ", ".join(UI_EXTRA_SUFFIXES))
    return True, u
