"""Сторож обновлений Telegram: новая версия Bot API доходит до админа в день выхода.

Владелец попросил внедрить HTMLBubbles (интерактивные HTML-виджеты в чатах,
Telegram 13.2). Документация 05.11.2026: приложения их показывают, а API для
отправки нет — журнал Bot API заканчивается версией 10.3, ни метода, ни
типа про виджеты. Сторож следит за официальным журналом и пишет
администраторам, как только выйдет новая версия, и есть ли в ней виджеты.

Держит этот файл:
* разбор журнала (вёрстка core.telegram.org: <h4> с датой, «Bot API X.Y»);
* первый запуск только запоминает версию — без сообщения о давно вышедшей;
* новая версия — сообщение по-русски, с отметкой про HTML-виджеты;
* сообщить некому — отметка НЕ двигается, новость не теряется.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import telegram_updates_watch as w


def _page(*releases: tuple[str, str, list[str]]) -> str:
    parts = ["<html><body><h3>Recent changes</h3>"]
    for date, ver, items in releases:
        slug = date.lower().replace(" ", "-").replace(",", "")
        parts.append(
            f'<h4><a class="anchor" name="{slug}" href="#{slug}"><i class="anchor-icon"></i></a>{date}</h4>'
            f"<p><strong>Bot API {ver}</strong></p><ul>"
            + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
    return "".join(parts) + "</body></html>"


OLD = _page(("August 24, 2026", "10.3", ["Added <em>RichMessageButton</em>.", "Added DisabledButton."]),
            ("July 14, 2026", "10.2", ["Rich messages: markdown or html formatting."]))
NEW = _page(("November 20, 2026", "10.4", ["Added the class <em>HtmlBubble</em> and the method sendHtmlBubble."]),
            ("August 24, 2026", "10.3", ["Added RichMessageButton."]))


def test_the_changelog_is_parsed_newest_first():
    r = w.parse_changelog(OLD)
    assert [(x["version"], x["date"]) for x in r] == [("10.3", "August 24, 2026"),
                                                     ("10.2", "July 14, 2026")]
    assert len(r[0]["items"]) == 2 and "DisabledButton" in r[0]["items"][1]
    assert r[1]["widgets"] is False, "«html formatting» — не виджеты"
    assert w.parse_changelog(NEW)[0]["widgets"] is True


def test_the_alert_is_in_russian_and_says_whether_widgets_arrived():
    text = w.alert_text(w.parse_changelog(NEW)[0])
    assert "Bot API 10.4" in text and "20 ноября 2026" in text
    assert "HTML-виджеты (HTMLBubbles)" in text and "Можно внедрять" in text
    no = w.alert_text(w.parse_changelog(OLD)[0])
    assert "пока нет" in no and "August" not in no


def test_only_newer_versions_are_reported_oldest_first():
    rel = w.parse_changelog(NEW) + w.parse_changelog(OLD)
    assert [x["version"] for x in w.newer_than(rel, "10.2")] == ["10.3", "10.4"]
    assert w.newer_than(rel, "10.4") == []
    assert w.version_key("10.10") > w.version_key("10.9"), "сравнение — по числам, не по строке"


# ── Живая база: отметка в platform_settings ────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


class _Bot:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))


def _run(body):
    async def go():
        import asyncpg
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM platform_settings WHERE key=$1", w.STATE_KEY)
            return await body(pool)
        finally:
            await pool.execute("DELETE FROM platform_settings WHERE key=$1", w.STATE_KEY)
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_first_run_is_a_baseline_then_a_new_version_reaches_the_admin():
    async def body(pool):
        bot = _Bot()

        async def old():
            return OLD

        async def new():
            return NEW

        first = await w.check_once(pool, bot, fetch=old, recipients={111})
        seen1 = await w._seen(pool)
        again = await w.check_once(pool, bot, fetch=old, recipients={111})
        nobody = await w.check_once(pool, bot, fetch=new, recipients=set())
        seen2 = await w._seen(pool)
        failed = await w.check_once(pool, _Bot(fail=True), fetch=new, recipients={111})
        seen3 = await w._seen(pool)
        got = await w.check_once(pool, bot, fetch=new, recipients={111, 222})
        seen4 = await w._seen(pool)
        return first, seen1, again, nobody, seen2, failed, seen3, got, seen4, bot.sent

    first, seen1, again, nobody, seen2, failed, seen3, got, seen4, sent = _run(body)
    assert first == [] and seen1 == "10.3", "первый запуск должен только запомнить версию"
    assert again == [], "о той же версии сообщили второй раз"
    assert nobody == [] and seen2 == "10.3", "сообщить некому, а отметка сдвинулась — новость потеряна"
    assert seen3 == "10.3", "доставка не удалась, а отметка сдвинулась"
    assert [x["version"] for x in got] == ["10.4"] and seen4 == "10.4"
    assert sorted(c for c, _ in sent) == [111, 222]
    assert all("HTMLBubbles" in t for _, t in sent)


def test_it_is_started_with_the_bot():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "main.py"), encoding="utf-8").read()
    assert '_resilient("telegram_updates_watch", _tg_updates.run, pool, bot)' in src
