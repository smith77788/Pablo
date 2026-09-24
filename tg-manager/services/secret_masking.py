"""Маскирование секретов в тексте, который увидит человек или лог.

Один источник правды на весь проект: токены ботов, строки сессий Telethon,
строки подключения к БД и прокси с логином-паролем не должны попадать ни в
логи, ни в сообщения бота, ни в ответы API. Раньше каждое место решало само —
и решало по-разному: где-то печаталось начало токена, где-то хвост, а хвост
токена бота это и есть его секретная часть.

Функции чистые и без зависимостей: их можно звать откуда угодно, в том числе
из обработчика ошибки, где падать уже нельзя.
"""
from __future__ import annotations

import re

_MASK = "***"

# Токен бота: <числовой id>:<35+ символов секрета>. Идентификатор бота не
# секрет (он виден в @username через getMe), секрет — только часть после
# двоеточия, поэтому её и прячем, оставляя id пригодным для поиска в логах.
_BOT_TOKEN_RE = re.compile(r"\b(\d{6,12}):([A-Za-z0-9_-]{30,})")

# Строка сессии Telethon (StringSession): версия «1» и длинный base64.
# Это ПОЛНЫЙ доступ к аккаунту — в тексте не место даже куску.
_SESSION_RE = re.compile(r"\b1[A-Za-z0-9+/=_-]{80,}")

# Строка подключения/прокси с логином и паролем: scheme://user:pass@host
_URL_CRED_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)([^\s:/@]+):([^\s@]+)@")


def mask_bot_token(token: str | None) -> str:
    """Токен бота → «<id>:***». По id можно найти бота, секрет не раскрыт.

    Не логировать ни начало, ни конец секретной части: и то и другое сужает
    перебор, а хвост — это прямо кусок ключа.
    """
    if not token:
        return _MASK
    m = _BOT_TOKEN_RE.match(str(token).strip())
    if m:
        return f"{m.group(1)}:{_MASK}"
    return _MASK


def mask_session(session_str: str | None) -> str:
    """Строка сессии → «***». Куски строки сессии наружу не отдаём никогда."""
    return _MASK if session_str else _MASK


def redact_secrets(text: str | None, limit: int = 2000) -> str:
    """Вычистить секреты из произвольного текста (ошибка, ответ API, лог).

    Ставится на узких местах — там, где текст уходит наружу, — чтобы утечка не
    зависела от того, вспомнил ли автор конкретного места про маскирование.
    """
    if not text:
        return ""
    s = str(text)[:limit]
    s = _SESSION_RE.sub(_MASK, s)
    s = _BOT_TOKEN_RE.sub(lambda m: f"{m.group(1)}:{_MASK}", s)
    s = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:{_MASK}@", s)
    return s


# Ключи, значение которых наружу не уходит НИКОГДА, чем бы оно ни выглядело.
# Это имена колонок с секретами (`tg_accounts.session_str`, пароль 2FA,
# api_hash) и их обычные написания.
#
# Чего здесь намеренно НЕТ и почему:
#   * `token` — под этим ключом мини-апп получает свой токен входа; токен бота
#     ловится ниже по форме значения;
#   * `session` — под этим ключом владелец выгружает СВОЮ сессию (кнопка
#     «экспорт сессии», owner-scoped) и должен получить её целиком;
#   * `proxy_url` — прокси владельца возвращается его же приложению, и оно
#     отправляет URL назад, когда проверяет сессию через этот прокси; замена
#     пароля на «***» сломала бы проверку.
_SECRET_KEYS = frozenset({
    "session_str", "session_string", "string_session",
    "api_hash", "app_hash",
    "password", "passwd", "twofa_password", "two_fa_password",
    "password_2fa", "cloud_password",
    "bot_token", "token_plain", "secret", "admin_secret",
})

# Минимальная длина строки, в которой вообще может уместиться секрет по форме:
# 6 цифр + двоеточие + 30 символов. Короче — не проверяем, и это делает проход
# по ответу почти бесплатным: подавляющая часть полей короткая.
_MIN_SECRET_LEN = 37

# Потолок вложенности: ответы API — плоские структуры, а цикл в данных (или
# просто очень глубокая вложенность) не должен превращаться в рекурсию без дна.
_MAX_SCRUB_DEPTH = 12


def _scrub_str(value: str) -> str:
    """Вычистить токен бота ПО ФОРМЕ значения, где бы он ни лежал.

    Строку сессии здесь НЕ ищем намеренно. Её шаблон — «1» плюс 80+ символов
    base64, а ответы этого API несут base64-картинки (QR входа в аккаунт
    отдаётся как data:image/png;base64,...). Внутри такого блока `+` и `/`
    создают границы слова, и шаблон сессии совпал бы с куском картинки — QR
    приехал бы битым. Сессия закрыта по ИМЕНИ ключа (_SECRET_KEYS), а форма
    токена бота с base64 не пересекается: двоеточия в base64 не бывает.
    """
    if len(value) < _MIN_SECRET_LEN or ":" not in value:
        return value
    return _BOT_TOKEN_RE.sub(lambda m: f"{m.group(1)}:{_MASK}", value)


def scrub_payload(data, _depth: int = 0):
    """Последний барьер перед отдачей данных наружу: секретов в ответе нет.

    Ставится в единственной двери ответа API, а не в 500 обработчиках. Смысл
    именно в этом: обработчик, который завтра напишут через `SELECT *` по
    таблице с секретами, не сможет отдать их клиенту, даже если автор о
    секретах не подумал.

    Возвращает исходный объект, если чистить нечего — большие ответы не
    копируются лишний раз.
    """
    if _depth > _MAX_SCRUB_DEPTH:
        return data
    if isinstance(data, str):
        return _scrub_str(data)
    if isinstance(data, dict):
        changed = False
        out = {}
        for key, val in data.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                new = _MASK if val not in (None, "", b"") else val
            else:
                new = scrub_payload(val, _depth + 1)
            if new is not val:
                changed = True
            out[key] = new
        return out if changed else data
    if isinstance(data, (list, tuple)):
        items = [scrub_payload(v, _depth + 1) for v in data]
        if any(n is not o for n, o in zip(items, data)):
            return type(data)(items) if isinstance(data, tuple) else items
        return data
    return data
