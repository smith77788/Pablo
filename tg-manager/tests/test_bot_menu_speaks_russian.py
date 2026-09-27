# -*- coding: utf-8 -*-
"""Бот говорит с владельцем по-русски и теми же словами, что мини-апп.

Две болезни, которые здесь закрыты.

1. Английские подписи кнопок. Владелец английский не понимает (CLAUDE.md,
   первые строки), а в боте оставались «Bot Factory», «Dry Run», «Global
   Presence», «Recovery Engine», «Broadcast», «workspace», «Free Mode» и ещё
   полторы сотни. Кнопка — самый заметный текст продукта: её читают до того,
   как решат нажать.

2. Один модуль под двумя именами. Бот звал раздел «Content Mesh», мини-апп —
   «Сеть контента». Человек, перешедший из бота в мини-апп, искал раздел,
   которого там «нет». Поэтому второй тест требует буквального совпадения
   названий с плитками каталога мини-аппа.

Допускаются только технические обозначения, у которых нет русского имени:
имена сторонних продуктов и библиотек (Telegram, BotFather, Telethon,
Railway), форматы (JSON, CSV, ZIP), поля Telegram (username, bio) и
устоявшиеся сокращения (API, SEO, 2FA). Список ниже — закрытый: новое
английское слово в подписи валит тест, и это намеренно.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot"
MINIAPP = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

# Технические обозначения без русского эквивалента. Пополнять осознанно:
# «нет русского слова», а не «лень переводить».
KEEP = {
    # продукты, сервисы, библиотеки
    "infragram", "telegram", "botfather", "telethon", "pyrogram", "railway",
    "claude", "gpt", "gmail", "outlook", "oauth", "cf", "tdata", "spintax",
    "excel", "usdt", "ton", "wallet",
    # форматы и протоколы
    "json", "csv", "zip", "url", "ip", "html", "md", "txt", "pdf", "session",
    "string", "qr",
    # поля и сущности Telegram
    "username", "bio", "premium", "stars", "story", "bm",
    # сокращения
    "api", "seo", "smm", "ai", "ui", "tg", "id", "crm", "dm", "ctr", "fa",
    "sms", "email",
    "trc", "ua", "ru", "en", "os",
    # названия тарифов (совпадают с ключами в БД)
    "enterprise", "starter", "pro", "free",
    # разметка и команды, которые пользователь вводит как есть
    "b", "i", "https", "http", "t", "me", "start", "help", "menu", "code",
    # служебное
    "user", "autoreg", "batch", "owner", "count", "country",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z\-']+")
# kb.button(text="…") и text=f"…" — подпись кнопки, самый видимый текст бота.
_BTN = re.compile(r'text=f?(["\'])(.*?)\1', re.S)


def _labels():
    """(файл, строка, подпись) для каждой подписи кнопки в боте."""
    out = []
    for path in sorted(BOT.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for m in _BTN.finditer(src):
            line = src[: m.start()].count("\n") + 1
            out.append((path.relative_to(ROOT), line, m.group(2)))
    return out


def _english(label: str) -> list[str]:
    """Английские слова в подписи. Подстановки f-строк вырезаны: там значения
    из БД, а не текст, который писал разработчик."""
    clean = re.sub(r"\{[^{}]*\}", " ", label)
    # Хвостовой дефис — часть русского слова («SEO-аудит»), а не английского.
    return [w for w in _WORD.findall(clean) if w.lower().strip("-'") not in KEEP]


def test_detector_actually_sees_labels():
    """Анти-пустота: если бы регулярка перестала находить подписи, оба теста
    ниже проходили бы на пустом множестве и ничего не стерегли."""
    labels = _labels()
    assert len(labels) > 1500, f"подписей найдено всего {len(labels)} — сломан разбор"
    files = {str(f) for f, _, _ in labels}
    assert len(files) > 50, f"подписи найдены лишь в {len(files)} файлах — сломан обход"
    # и детектор английского действительно срабатывает на английском
    assert _english("🔄 Recovery Engine") == ["Recovery", "Engine"]
    assert _english("🔄 Восстановление") == []
    assert _english("📊 Отчёт {name}") == [], "подстановки не должны попадать в находки"
    assert _english("📈 SEO-аудит") == [], "хвостовой дефис не делает слово английским"


def test_bot_buttons_are_russian():
    bad = [
        f"    {f}:{line}: «{lbl}» → {en}"
        for f, line, lbl in _labels()
        if (en := _english(lbl))
    ]
    assert not bad, (
        "английские подписи кнопок бота (владелец английский не понимает):\n"
        + "\n".join(bad)
    )


# Модуль → как его зовёт плитка каталога мини-аппа. Бот обязан звать так же.
SAME_NAME = [
    "Сеть контента", "Авто-воронки", "Нарратив", "Клон и адаптация", "Персоны",
    "ИИ Память", "Призрак", "Физика", "Агент роста", "Разведка рекламы",
    "ДНК аудитории", "Звёзды", "Ноды", "Рой", "Фабрика ботов",
    "Фабрика каналов", "Массовая публикация", "Массовые жалобы", "Топология",
]


def test_bot_and_miniapp_call_modules_the_same():
    """Имя модуля в боте и в мини-аппе — одно и то же слово.

    Проверяется в обе стороны: имя есть в каталоге мини-аппа И встречается в
    текстах бота. Разъедутся — человек, перешедший из бота в мини-апп, не
    найдёт раздел.
    """
    bot_text = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(BOT.rglob("*.py"))
    )
    missing_miniapp = [n for n in SAME_NAME if f">{n}</div>" not in MINIAPP]
    assert not missing_miniapp, (
        "нет такой плитки в каталоге мини-аппа (переименовали?): "
        + ", ".join(missing_miniapp)
    )
    missing_bot = [n for n in SAME_NAME if n not in bot_text]
    assert not missing_bot, (
        "бот зовёт модуль иначе, чем мини-апп: " + ", ".join(missing_bot)
    )


# Английские названия модулей: бот писал их прямо в тексте сообщений
# («⚡ <b>Auto-Funnel</b>», «👻 <b>Ghost Engine</b>», «🗺️ <b>Topology Map</b>»),
# даже там, где кнопка рядом уже была русской. Заголовок экрана владелец читает
# первым, поэтому он важнее кнопки.
ENGLISH_MODULE_NAMES = [
    "Bot Factory", "Channel Factory", "Ecosystem Factory", "Global Presence",
    "Content Mesh", "Ghost Engine", "Auto-Funnel", "Topology Map",
    "Recovery Engine", "Health Center", "Intelligence Report", "Keyword Gap",
    "Mass Publish", "Free Mode", "Strike Module", "Copilot", "Failover",
    "Workspace", "Swarm", "Broadcast", "Dry Run",
]
_CYR = re.compile(r"[А-Яа-яЁё]")


def _human_strings():
    """Строковые литералы с кириллицей — то есть тексты для человека.

    Докстринги исключены: их читает разработчик. Строки без кириллицы тоже
    (callback_data, ключи словарей, SQL) — но с оговоркой: кусок f-строки может
    быть без кириллицы, поэтому куски одной f-строки склеиваются обратно.
    """
    import ast as _ast

    out = []
    for path in sorted(BOT.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = _ast.parse(src)
        docs = set()
        for node in _ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef,
                                 _ast.AsyncFunctionDef)) and body:
                first = body[0]
                if isinstance(first, _ast.Expr) and isinstance(first.value, _ast.Constant) \
                        and isinstance(first.value.value, str):
                    docs.add((first.value.lineno, first.value.col_offset))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.JoinedStr):
                text = "".join(
                    v.value for v in node.values
                    if isinstance(v, _ast.Constant) and isinstance(v.value, str)
                )
            elif isinstance(node, _ast.Constant) and isinstance(node.value, str):
                if (node.lineno, node.col_offset) in docs:
                    continue
                text = node.value
            else:
                continue
            if _CYR.search(text):
                out.append((path.relative_to(ROOT), node.lineno, text))
    return out


def test_message_texts_call_modules_in_russian():
    """В тексте сообщения модуль зовётся так же, как на кнопке и в мини-аппе."""
    strings = _human_strings()
    assert len(strings) > 2000, f"строк-сообщений найдено {len(strings)} — сломан разбор"
    bad = []
    for f, line, text in strings:
        hit = [n for n in ENGLISH_MODULE_NAMES if n in text]
        if hit:
            bad.append(f"    {f}:{line}: {text[:70]!r} → {hit}")
    assert not bad, (
        "английские названия модулей в текстах бота:\n" + "\n".join(sorted(set(bad)))
    )
