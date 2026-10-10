"""Варьирование формы Telegram-ссылок: разный вид, тот же адрес.

Главный инвариант: варьирование меняет ТОЛЬКО форму ссылки (https://t.me/x ↔
t.me/x ↔ @x ↔ telegram.me/x), но адрес назначения (имя канала / хэш инвайта)
остаётся прежним — иначе ссылка повела бы не туда. Второй инвариант: разные
каналы-публикаторы (seed) получают разные формы — ради этого всё и затевалось.
"""
from __future__ import annotations

import re

from services import link_vary


def _targets(text: str) -> set[str]:
    """Канонические цели всех ссылок в тексте — для сверки «адрес не изменился»."""
    t = set()
    for m in re.finditer(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
                         r"(?:joinchat/|\+)([A-Za-z0-9_-]{12,})", text, re.I):
        t.add("invite:" + m.group(1))
    for m in re.finditer(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
                         r"(?!joinchat/|\+)([A-Za-z][A-Za-z0-9_]{3,31})", text, re.I):
        t.add("public:" + m.group(1).lower())
    for m in re.finditer(r"(?<![\w@./])@([A-Za-z][A-Za-z0-9_]{3,31})\b", text):
        t.add("public:" + m.group(1).lower())
    return t


def test_detects_channel_links():
    assert link_vary.contains_channel_link("Подпишись https://t.me/mychannel")
    assert link_vary.contains_channel_link("вот @mychannel заходи")
    assert link_vary.contains_channel_link("t.me/+AbCdEf1234567890 приват")
    assert not link_vary.contains_channel_link("просто текст без ссылок")
    assert not link_vary.contains_channel_link("")
    assert not link_vary.contains_channel_link(None)


def test_vary_preserves_the_destination():
    text = "Лучший канал: https://t.me/best_news_channel — заходи!"
    before = _targets(text)
    for seed in range(50):
        out = link_vary.vary_channel_links(text, seed)
        assert _targets(out) == before, (
            f"seed={seed}: адрес ссылки изменился — ссылка поведёт не туда:\n{out}"
        )


def test_invite_link_destination_preserved():
    text = "Приват: https://t.me/+AbCdEf1234567890xyz"
    before = _targets(text)
    for seed in range(30):
        out = link_vary.vary_channel_links(text, seed)
        assert _targets(out) == before, f"seed={seed}: хэш инвайта изменился:\n{out}"


def test_different_seeds_produce_variety():
    """Ради этого всё и делается: разные каналы — разные формы одной ссылки."""
    text = "Канал https://t.me/best_news_channel"
    seen = {link_vary.vary_channel_links(text, s) for s in range(40)}
    assert len(seen) >= 3, (
        f"варьирование почти не даёт разнообразия ({len(seen)} форм) — "
        f"сигнатура «один и тот же URL» остаётся"
    )


def test_deterministic_same_seed():
    text = "Канал t.me/best_news_channel"
    a = link_vary.vary_channel_links(text, 777)
    b = link_vary.vary_channel_links(text, 777)
    assert a == b, "один seed даёт разные результаты — предпросмотр разойдётся с отправкой"


def test_noop_without_links():
    text = "Обычный пост про погоду без единой ссылки."
    assert link_vary.vary_channel_links(text, 123) == text


def test_html_anchor_href_not_broken():
    """href внутри <a> не трогаем — иначе ломается parse_mode=html."""
    text = '<a href="https://t.me/best_news_channel">наш канал</a>'
    for seed in range(20):
        out = link_vary.vary_channel_links(text, seed)
        assert out == text, f"seed={seed}: сломали HTML-ссылку:\n{out}"


def test_bare_link_outside_tags_still_varies():
    text = '<b>Жир</b> и ссылка https://t.me/best_news_channel текст'
    outs = {link_vary.vary_channel_links(text, s) for s in range(30)}
    # тег <b> остаётся, а ссылка варьируется
    assert all("<b>Жир</b>" in o for o in outs)
    assert len(outs) >= 2, "голая ссылка рядом с тегом не варьируется"


def test_does_not_touch_plain_words():
    """Короткие @ и слова с точками не должны считаться ссылками на каналы."""
    text = "почта user@mail.ru и версия v1.2 — не ссылки"
    assert link_vary.vary_channel_links(text, 5) == text
