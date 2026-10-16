"""Варьирование формы Telegram-ссылок при массовой публикации (анти-детект).

ЗАЧЕМ. Когда одна и та же ссылка на канал (`https://t.me/target`) публикуется
в десятки каналов почти одновременно, Telegram видит координированный «буст»:
множество каналов → один target за короткое время, идентичным текстом. Это
прямая сигнатура искусственного продвижения — и целевой канал (на который ведут
ссылки) быстро сносят. spintax варьирует ОКРУЖАЮЩИЙ текст, но саму ссылку не
трогает: во всех постах она посимвольно одинакова, и это самый явный паттерн.

ЧТО ДЕЛАЕМ. Детерминированно (seed = id канала-публикатора) переписываем КАЖДУЮ
ссылку в одну из эквивалентных форм, НЕ меняя адрес назначения:

    https://t.me/name · t.me/name · @name · telegram.me/name
    https://t.me/+HASH · t.me/+HASH · https://t.me/joinchat/HASH · t.me/joinchat/HASH

Так одинаковый пост даёт разное текстовое представление одной и той же ссылки —
посимвольный паттерн «один и тот же URL во всех постах» рассыпается. Это снижает
сигнатуру, но НЕ отменяет графовый анализ Telegram: полную защиту даёт только
сочетание с разносом во времени и прогревом целевого канала (см. вызывающий код).

БЕЗОПАСНОСТЬ. Чистая строковая функция. Нет ссылок — возвращает текст как есть
(no-op). Содержимое HTML-тегов (`<a href=...>`) не трогаем, чтобы не сломать
parse_mode=html. Адрес (name/hash) сохраняется дословно — ссылка ведёт туда же.
"""
from __future__ import annotations

import hashlib
import re

# HTML-тег целиком: его внутренности (в т.ч. href) не варьируем — иначе можно
# сломать разметку. Варьируем только «голые» ссылки в видимом тексте.
_TAG_RE = re.compile(r"<[^>]+>")

# Приватная инвайт-ссылка: .../+HASH или .../joinchat/HASH. Проверяем ПЕРВОЙ,
# иначе публичный паттерн съел бы «joinchat» как юзернейм.
_INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:joinchat/|\+)([A-Za-z0-9_-]{12,})",
    re.IGNORECASE,
)
# Публичная ссылка t.me/name (но не +/joinchat). Username Telegram: начинается с
# буквы, 4–31 символа дальше (итого 5–32), буквы/цифры/подчёркивание.
_PUBLIC_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?!joinchat/|\+)([A-Za-z][A-Za-z0-9_]{3,31})",
    re.IGNORECASE,
)
# Упоминание @name как ссылка на канал/бот. Не внутри email/пути: перед @ не
# должно быть «словесного» символа.
_MENTION_RE = re.compile(r"(?<![\w@./])@([A-Za-z][A-Za-z0-9_]{3,31})\b")


def _forms_public(name: str) -> list[str]:
    return [
        f"https://t.me/{name}",
        f"t.me/{name}",
        f"@{name}",
        f"telegram.me/{name}",
        f"https://telegram.me/{name}",
    ]


def _forms_invite(h: str) -> list[str]:
    return [
        f"https://t.me/+{h}",
        f"t.me/+{h}",
        f"https://t.me/joinchat/{h}",
        f"t.me/joinchat/{h}",
    ]


def _pick(options: list[str], seed: int, key: str, occ: int) -> str:
    """Детерминированный выбор формы: один вход (seed+адрес+номер) → один выход."""
    digest = hashlib.blake2b(
        f"{seed}|{key}|{occ}".encode("utf-8"), digest_size=8
    ).digest()
    return options[int.from_bytes(digest, "big") % len(options)]


def contains_channel_link(text: str | None) -> bool:
    """Есть ли в тексте ссылка/упоминание Telegram-канала или бота."""
    if not text:
        return False
    # Теги вырезаем, чтобы href внутри <a> не считался «голой» ссылкой дважды.
    bare = _TAG_RE.sub(" ", text)
    return bool(
        _INVITE_RE.search(bare)
        or _PUBLIC_RE.search(bare)
        or _MENTION_RE.search(bare)
    )


def vary_channel_links(text: str | None, seed: int) -> str:
    """Переписать каждую голую Telegram-ссылку в эквивалентную форму.

    Детерминированно по seed (id канала-публикатора): один и тот же пост на один
    и тот же канал даёт один и тот же результат (предпросмотр == отправка), но
    РАЗНЫЕ каналы получают разные формы одной ссылки. Адрес не меняется.
    """
    if not text:
        return text or ""

    counter = {"n": 0}

    def _vary_segment(segment: str) -> str:
        # Инвайты — первыми (защищены от публичного паттерна исключением выше).
        def _sub_invite(m: re.Match) -> str:
            h = m.group(1)
            counter["n"] += 1
            return _pick(_forms_invite(h), seed, f"+{h}", counter["n"])

        def _sub_public(m: re.Match) -> str:
            name = m.group(1)
            counter["n"] += 1
            return _pick(_forms_public(name), seed, name.lower(), counter["n"])

        def _sub_mention(m: re.Match) -> str:
            name = m.group(1)
            counter["n"] += 1
            return _pick(_forms_public(name), seed, name.lower(), counter["n"])

        segment = _INVITE_RE.sub(_sub_invite, segment)
        segment = _PUBLIC_RE.sub(_sub_public, segment)
        segment = _MENTION_RE.sub(_sub_mention, segment)
        return segment

    # Идём по тексту, пропуская HTML-теги нетронутыми: варьируем только то, что
    # пользователь видит как текст.
    out: list[str] = []
    pos = 0
    for tag in _TAG_RE.finditer(text):
        out.append(_vary_segment(text[pos:tag.start()]))
        out.append(tag.group(0))  # сам тег — как есть
        pos = tag.end()
    out.append(_vary_segment(text[pos:]))
    return "".join(out)
