"""Виртуальный администратор виден в интерфейсе и пишет числа по-русски.

Вход жил только кнопкой внутри «Каналов» — из раздела «Ещё» администратора было
не найти. Числа на его экранах шли через общий num(), который пишет «1.2K», и
склонялись как «3 постов».
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_more_screen_has_admin_tile():
    html = _read("mini_app", "index.html")
    more = html[html.index('<div class="screen" id="s-more">'):]
    more = more[:more.index('<div class="screen"', 10)]
    assert 'onclick="openVaAdmin()"' in more


def test_admin_screens_do_not_use_latin_number_format():
    js = _read("mini_app", "screens", "va_admin.js")
    assert not re.search(r"(?<![\w.])num\(", js), "num() даёт «1.2K» — латиница в интерфейсе"
    assert "' постов</span>" not in js and "пост.)" not in js


def test_ai_banner_says_where_to_enter_key(monkeypatch):
    """Плашка «ИИ не подключён» обязана сказать, ГДЕ ввести ключ: иначе владелец
    видит запрет без выхода (ключ задаётся только в боте, в /admin)."""
    from services import ai_claude, ai_providers
    from services import channel_admin as ca
    monkeypatch.setattr(ai_claude, "enabled", lambda: False)
    monkeypatch.setattr(ai_providers, "configured_providers", list)
    ok, note = ca.ai_ready()
    assert not ok and "/admin" in note and "AI-ключи" in note


def _va():
    return _read("mini_app", "screens", "va_admin.js")


def test_collapsed_settings_still_save_every_field():
    """Свёрнутые группы настроек остаются в разметке — «Сохранить» шлёт всё.

    Экран настроек шёл одной простынёй из двадцати полей: на телефоне до
    кнопки «Сохранить» было четыре пролистывания, и до неё не добирались.
    Поля убрали в две группы `<details>`. Опасность ровно одна: вынести поле
    из разметки и молча потерять его при сохранении — тогда владелец правит
    значение, жмёт «Сохранить», а оно не уходит.
    """
    va = _va()
    # что читается при сохранении
    saved = set(re.findall(r"_vaVal\('(va[A-Za-z]+)'\)", va))
    saved.discard("vaErr")
    # что есть в разметке: часть полей строит _vaArea(id, ...)
    rendered = set(re.findall(r'id="(va[A-Za-z]+)"', va)) | set(
        re.findall(r"_vaArea\('(va[A-Za-z]+)'", va))
    assert saved, "разбор сломался: не нашли ни одного читаемого поля"
    missing = sorted(saved - rendered)
    assert not missing, (
        "поля читаются при сохранении, но их нет в разметке — правка владельца "
        f"пропадёт молча: {missing}")


def test_long_settings_are_grouped_not_one_sheet():
    va = _va()
    assert va.count("<details class=\"acc-actions\"") >= 2, (
        "настройки снова одной простынёй — кнопка «Сохранить» уезжает за "
        "четыре пролистывания")
    # группы сворачиваются, а не прячутся: display:none унёс бы поля из потока
    assert "display:none" not in va.split("<details")[1][:400]


def test_draft_buttons_do_not_share_one_narrow_row():
    """Три кнопки черновика в один ряд при 360px ломали надписи на две строки.

    Главное действие — во всю ширину, второстепенные рядом и с подписями:
    безымянный «✖️» не говорил, что он делает.
    """
    va = _va()
    i = va.index("function _vaDraftsHtml")
    body = va[i:va.index("\nasync function vaDraftAct")]
    assert "'publish')\">✅ Опубликовать</button>" in body.replace("\\'", "'")
    assert 'style="width:100%"' in body, "главное действие не во всю ширину"
    assert "Пропустить</button>" in body, "у кнопки отказа нет подписи"


def test_live_draft_publish_requires_explicit_confirmation():
    va = _va()
    start = va.index("async function vaDraftAct")
    body = va[start:va.index("\nasync function openVaChannel", start)]
    assert "action === 'publish'" in body
    assert "await askConfirm(" in body
    assert body.index("await askConfirm(") < body.index("await api(")


def test_reopening_admin_resets_channel_pagination():
    va = _va()
    start = va.index("async function openVaAdmin")
    body = va[start:va.index("\nasync function _vaLoadList", start)]
    assert "_vaListPage = 0" in body
    assert "_vaChannels = []" in body
    assert body.index("_vaListPage = 0") < body.index("await _vaLoadList()")
