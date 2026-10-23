"""Экран «Что умеет Infragram»: описание проекта в самом продукте.

ЗАЧЕМ ЭТО ЕСТЬ. Владельца постоянно спрашивают, что это за проект, и он
пересказывает вручную. Кнопка отвечает за него и отдаёт готовое сообщение для
пересылки — со ссылкой, по которой человек откроет описание сам.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, и почему именно это:
  • ЛИМИТЫ. Telegram отклоняет сообщение длиннее 4096 символов и
    callback_data длиннее 64 байт. Текст растёт вместе с продуктом, и однажды
    он перешагнёт лимит — тогда кнопка перестанет открываться. Пусть об этом
    скажет тест, а не пользователь.
  • РАЗМЕТКА. Незакрытый тег в HTML — и Telegram отказывает во всём сообщении.
  • ЧЕСТНОСТЬ. Описание обещает возможности; каждая опорная должна иметь
    подтверждение в коде. Обещать несуществующее хуже, чем промолчать.
  • НЕ УСТАРЕЕТ. Каталог функций живёт в мини-аппе и меняется. Тест сверяет с
    ним опорные разделы: продукт вырос — описание обязано догнать.
"""
from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TG_TEXT_LIMIT = 4096
_TG_CALLBACK_LIMIT = 64        # байт, не символов


@pytest.fixture(scope="module")
def catalog():
    from services import product_catalog
    return product_catalog


# ── лимиты Telegram ──────────────────────────────────────────────────────────

def test_intro_fits_telegram_limit(catalog):
    text = catalog.intro_text()
    assert len(text) <= _TG_TEXT_LIMIT, (
        f"вступление {len(text)} символов при лимите {_TG_TEXT_LIMIT} — "
        "Telegram откажет в отправке, и кнопка перестанет открываться")


def test_every_section_fits_telegram_limit(catalog):
    too_long = [(s.title, len(catalog.section_text(s))) for s in catalog.SECTIONS
                if len(catalog.section_text(s)) > _TG_TEXT_LIMIT]
    assert not too_long, f"разделы длиннее лимита: {too_long}"


def test_share_message_fits_telegram_limit(catalog):
    text = catalog.share_text("SomeLongBotUsername")
    assert len(text) <= _TG_TEXT_LIMIT, (
        f"сообщение для пересылки {len(text)} символов — не отправится")


def test_callback_data_fits_telegram_limit(catalog):
    from bot.callbacks import AboutCb

    for s in catalog.SECTIONS:
        packed = AboutCb(action="sec", key=s.key).pack()
        assert len(packed.encode("utf-8")) <= _TG_CALLBACK_LIMIT, (
            f"callback_data раздела «{s.title}» длиннее {_TG_CALLBACK_LIMIT} байт: "
            f"{packed!r} — Telegram не примет такую кнопку")


# ── разметка ─────────────────────────────────────────────────────────────────

def _tags_balanced(html: str) -> bool:
    stack = []
    for m in re.finditer(r"</?([a-z]+)>", html):
        tag = m.group(1)
        if m.group(0).startswith("</"):
            if not stack or stack.pop() != tag:
                return False
        else:
            stack.append(tag)
    return not stack


def test_html_markup_is_valid_everywhere(catalog):
    """Незакрытый тег — и Telegram отклоняет ВСЁ сообщение."""
    texts = {"вступление": catalog.intro_text(),
             "пересылка": catalog.share_text("bot")}
    texts.update({s.title: catalog.section_text(s) for s in catalog.SECTIONS})
    broken = [name for name, t in texts.items() if not _tags_balanced(t)]
    assert not broken, f"несбалансированная HTML-разметка: {broken}"


def test_only_tags_telegram_understands(catalog):
    """Telegram понимает узкий набор тегов; остальные ломают сообщение."""
    allowed = {"b", "i", "u", "s", "a", "code", "pre", "em", "strong"}
    texts = [catalog.intro_text(), catalog.share_text("bot")] + [
        catalog.section_text(s) for s in catalog.SECTIONS]
    bad = {t for text in texts for t in re.findall(r"</?([a-z]+)[ >]", text)
           if t not in allowed}
    assert not bad, f"теги, которых Telegram не понимает: {sorted(bad)}"


# ── структура ────────────────────────────────────────────────────────────────

def test_sections_are_usable(catalog):
    assert len(catalog.SECTIONS) >= 5, "разделов слишком мало для «полного описания»"
    keys = [s.key for s in catalog.SECTIONS]
    assert len(keys) == len(set(keys)), f"ключи разделов повторяются: {keys}"
    for s in catalog.SECTIONS:
        assert re.fullmatch(r"[a-z_]+", s.key), (
            f"ключ «{s.key}» не латиницей — попадёт в callback_data и сломает кнопку")
        assert s.items, f"раздел «{s.title}» пуст — открывать его незачем"
        assert s.summary and not s.summary.endswith(".."), s.title


def test_share_message_carries_a_link_back(catalog):
    """Смысл пересылки — чтобы человек открыл описание сам и больше не спрашивал."""
    text = catalog.share_text("MyBot")
    assert "t.me/MyBot?start=about" in text, (
        "в пересылаемом сообщении нет ссылки на бота — получателю некуда идти")
    # без username ссылку не построить, но текст всё равно обязан быть полезным
    assert "t.me/" not in catalog.share_text(""), "битая ссылка без username"
    assert len(catalog.share_text("")) > 500


def test_share_message_has_no_navigation_leftovers(catalog):
    """Пересылают одно сообщение: подсказки про кнопки в нём бессмысленны."""
    text = catalog.share_text("MyBot")
    for phrase in ("Нажмите раздел", "кнопку ниже", "выберите раздел"):
        assert phrase.lower() not in text.lower(), (
            f"в пересылаемом тексте осталась подсказка «{phrase}» — "
            "у получателя кнопок нет")


# ── честность обещаний ───────────────────────────────────────────────────────

# Возможность из описания → что доказывает её существование в коде.
_CLAIMS = {
    "Прогрев": "services/account_warmer.py",
    "Парсер": "services/parser.py",
    "Массовый инвайт": "services/mass_inviter_engine.py",
    "Фабрика ботов": "bot/handlers/bot_factory.py",
    "Фабрика каналов": "bot/handlers/channel_factory.py",
    "Фабрика групп": "bot/handlers/group_factory.py",
    "Глобальный поиск": "services/global_search_engine.py",
    "Клонер контента": "services/content_cloner_engine.py",
    "Авто-воронки": "services/auto_funnel.py",
    "Spintax": "services/spintax_service.py",
    "Комплаенс": "services/compliance_engine.py",
    "Экосистемы": "services/ecosystem_brain.py",
}


def test_every_claim_has_something_behind_it(catalog):
    """Описание не должно обещать того, чего в продукте нет."""
    all_text = " ".join(
        [catalog.intro_text(), catalog.share_text("b")]
        + [catalog.section_text(s) for s in catalog.SECTIONS])
    missing = []
    for claim, path in _CLAIMS.items():
        if claim.lower() in all_text.lower() and not os.path.exists(
                os.path.join(ROOT, path)):
            missing.append(f"{claim} (нет {path})")
    assert not missing, (
        "описание обещает возможности, которых в коде нет: " + ", ".join(missing))


def test_headline_capabilities_are_mentioned(catalog):
    """Обратная сторона: главные вещи продукта не должны потеряться в описании."""
    all_text = " ".join(
        [catalog.intro_text()] + [catalog.section_text(s) for s in catalog.SECTIONS])
    for must in ("Рассылк", "Аккаунт", "Канал", "Прокси", "Прогрев",
                 "Парсер", "CRM", "Фабрика"):
        assert must.lower() in all_text.lower(), (
            f"в описании не упомянуто «{must}» — человек не поймёт, что это умеют")


def test_description_keeps_up_with_the_product(catalog):
    """Каталог функций мини-аппа — живой; описание обязано его догонять.

    Сверяем не пункт-в-пункт (формулировки тут человеческие, а не имена
    экранов), а масштаб: если модулей стало заметно больше, чем описание
    покрывает разделами, значит оно устарело и его пора дополнить.
    """
    index = os.path.join(ROOT, "mini_app", "index.html")
    html = open(index, encoding="utf-8").read()
    i = html.find('<div class="screen" id="s-more">')
    assert i != -1, "экран «Все функции» не найден — сверять не с чем"
    j = html.find('<div class="screen" id="s-', i + 10)
    labels = set(re.findall(r'<div class="mgmt-tile-lbl">([^<]+)</div>', html[i:j]))
    assert len(labels) > 30, f"каталог мини-аппа разобрался плохо: {len(labels)}"

    described = sum(len(s.items) for s in catalog.SECTIONS)
    assert described >= len(labels) * 0.6, (
        f"в мини-аппе {len(labels)} функций, а в описании перечислено {described}. "
        "Продукт вырос — дополните services/product_catalog.py")


# ── проводка в бот ───────────────────────────────────────────────────────────

def test_button_is_in_the_main_menu():
    src = open(os.path.join(ROOT, "bot", "keyboards.py"), encoding="utf-8").read()
    assert "AboutCb(action=\"menu\")" in src, (
        "кнопки нет в главном меню — до описания невозможно добраться")


def test_router_is_registered_and_command_exists():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "about_handler.router" in src, "роутер экрана не подключён"
    assert 'command="about"' in src, "команда /about не зарегистрирована в меню Telegram"


def test_deep_link_opens_the_description():
    """Ссылка из пересылаемого сообщения обязана срабатывать."""
    src = open(os.path.join(ROOT, "bot", "handlers", "start.py"),
               encoding="utf-8").read()
    assert 'start_param == "about"' in src, (
        "?start=about не обрабатывается — ссылка в пересылке ведёт в пустоту")


def test_no_screen_without_a_way_out():
    """После любого обработчика у человека остаются кнопки.

    Проверяем не каждую отправку по отдельности, а ИТОГ обработчика: сообщение
    для пересылки намеренно идёт БЕЗ кнопок (у получателя они не работают), но
    сразу за ним отправляется второе — с навигацией. Тупик — это когда кнопок
    не осталось совсем.
    """
    import ast

    src = open(os.path.join(ROOT, "bot", "handlers", "about.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.split("\n")

    handlers = [n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (n.name.startswith("cb_") or n.name.startswith("cmd_"))]
    assert handlers, "обработчиков не найдено"

    # Тела всех функций модуля — обработчик может отдавать экран через хелпер
    # (_send_intro), и это нормально: важен итог, а не место вызова.
    bodies = {n.name: "\n".join(lines[n.lineno - 1:n.end_lineno])
              for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _effective(fn) -> str:
        """Тело обработчика плюс тела ХЕЛПЕРОВ, которые он зовёт.

        Именно хелперов (имя с подчёркивания), а не соседних обработчиков.
        Обработчик зовёт соседа только в запасной ветке («раздел не найден» →
        меню), и если засчитывать его кнопки, то пропадёт ровно та поломка,
        ради которой этот тест есть: основная ветка осталась без клавиатуры,
        а тест зелёный, потому что кнопки нашлись у соседа.
        """
        body = bodies[fn.name]
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else (
                    f.attr if isinstance(f, ast.Attribute) else None)
                if (name and name.startswith("_") and name in bodies
                        and name != fn.name):
                    body += "\n" + bodies[name]
        return body

    dead_ends = [fn.name for fn in handlers if "reply_markup" not in _effective(fn)]
    assert not dead_ends, (
        "обработчик оставляет экран без единой кнопки — из него не выйти: "
        f"{dead_ends}")


def test_forwardable_message_is_sent_without_buttons():
    """Сообщение для пересылки обязано быть чистым.

    Кнопки под пересланным сообщением у получателя не работают — он нажмёт и
    ничего не произойдёт. Поэтому текст отдаётся отдельным сообщением без
    разметки, а навигация идёт следующим.
    """
    import ast

    src = open(os.path.join(ROOT, "bot", "handlers", "about.py"),
               encoding="utf-8").read()
    share = None
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.name == "cb_about_share":
            share = n
    assert share is not None, "обработчик пересылки не найден"

    def _mentions_share_text(node) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                name = f.id if isinstance(f, ast.Name) else (
                    f.attr if isinstance(f, ast.Attribute) else None)
                if name == "share_text":
                    return True
        return False

    # Берём именно ту отправку, в аргументах которой собирается текст для
    # пересылки, — без окон по символам: сдвинется код, проверка не промахнётся.
    sends = [c for c in ast.walk(share)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr in ("answer", "reply", "send_message")]
    forwardable = [c for c in sends if any(_mentions_share_text(a)
                                           for a in list(c.args) + c.keywords)]
    assert forwardable, "текст для пересылки нигде не отправляется"
    for call in forwardable:
        assert not any(k.arg == "reply_markup" for k in call.keywords), (
            "под пересылаемым сообщением есть кнопки — у получателя они мертвы")

    with_markup = [c for c in sends
                   if any(k.arg == "reply_markup" for k in c.keywords)]
    assert with_markup, "после пересылки не осталось навигации — человек застрянет"
