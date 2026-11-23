"""Экран длиннее 4096 знаков обрезается, а не пропадает.

Telegram не принимает сообщение длиннее 4096 знаков ЦЕЛИКОМ («message is too
long»). Экраны бота собираются из данных — списка аккаунтов, каналов, целей,
журнала, — и на большом флоте такой список однажды перерастает лимит. Тогда
нажатие ничего не показывает, а глобальный обработчик ошибок отвечал «Сообщение
слишком длинное. Сократите текст» — владельцу, который ничего не писал.

Здесь промежуточный слой сессии бота (aiogram request middleware): перед
отправкой `sendMessage` / `editMessageText` он меряет ВИДИМУЮ длину (разметка
HTML в лимит не входит) и, если текст не влезает, режет его по строкам,
закрывает оставшиеся открытыми теги и дописывает, что дальше не поместилось.
Резать по строкам, а не по знаку, нужно ради разметки: середина `<a href=…>` или
`&amp;` превращает ответ в «can't parse entities», то есть в ту же тишину.
"""
from __future__ import annotations

import html
import logging
import re

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import EditMessageText, SendMessage

log = logging.getLogger(__name__)

TG_LIMIT = 4096
# Запас под приписку и под расхождение нашей оценки с подсчётом Telegram
# (он считает в UTF-16, эмодзи бывают по два знака).
_BUDGET = 3800
TAIL = "\n\n… дальше не поместилось в одно сообщение."

_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)[^>]*>")
_VOID = {"br"}


def visible_len(text: str, *, as_html: bool) -> int:
    """Длина, которую увидит Telegram, в единицах UTF-16."""
    if as_html:
        text = html.unescape(re.sub(r"<[^>]*>", "", text))
    return len(text.encode("utf-16-le")) // 2


def _open_tags(fragment: str) -> list[str]:
    stack: list[str] = []
    for m in _TAG.finditer(fragment):
        closing, name = m.group(1), m.group(2).lower()
        if name in _VOID:
            continue
        if closing:
            if name in stack:
                while stack and stack.pop() != name:
                    pass
        else:
            stack.append(name)
    return stack


def fit(text: str, *, as_html: bool) -> str:
    """Текст, который Telegram примет: исходный, если влезает, иначе обрезанный."""
    if visible_len(text, as_html=as_html) <= TG_LIMIT:
        return text
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        cost = visible_len(line, as_html=as_html) + 1
        if used + cost > _BUDGET:
            break
        kept.append(line)
        used += cost
    if not kept:
        # Одна огромная строка: режем по знаку, но только как обычный текст.
        plain = html.unescape(re.sub(r"<[^>]*>", "", text)) if as_html else text
        cut = plain[:_BUDGET]
        return (html.escape(cut, quote=False) if as_html else cut) + TAIL
    body = "\n".join(kept)
    if as_html:
        body += "".join(f"</{name}>" for name in reversed(_open_tags(body)))
    return body + TAIL


def _is_html(method) -> bool:
    mode = getattr(method, "parse_mode", None)
    if mode is None:
        return False
    if isinstance(mode, str):
        return mode.upper() == "HTML"
    # Значение по умолчанию (Default) — у этого бота это HTML.
    return True


class LongTextGuard(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        if isinstance(method, (SendMessage, EditMessageText)):
            text = getattr(method, "text", None)
            if isinstance(text, str) and len(text) > _BUDGET:
                mode = getattr(method, "parse_mode", None)
                if not (isinstance(mode, str) and mode.upper().startswith("MARKDOWN")):
                    fitted = fit(text, as_html=_is_html(method))
                    if fitted is not text:
                        log.warning("экран не влез в 4096 знаков (%d) — обрезан", len(text))
                        method = method.model_copy(update={"text": fitted})
        return await make_request(bot, method)


def install(bot) -> None:
    """Идемпотентно вешает защиту на сессию бота."""
    mws = bot.session.middleware
    if not any(isinstance(m, LongTextGuard) for m in mws):
        mws(LongTextGuard())
