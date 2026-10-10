"""Цели касания в списках: палец, а не курсор.

Мини-апп открывают с телефона — другого способа его открыть нет. В строках
списков действия были набраны голыми span'ами вида

    <span style="font-size:18px;cursor:pointer;color:var(--red);padding:4px"
          onclick="deleteProxy(...)">🗑</span>

Замерено в Chromium на 360×780: такая цель — 26×33, соседние 🔄 и 🗑 стоят в
6 пикселях друг от друга, расстояние между их центрами 34 пикселя. Рекомендации
Apple и Google называют 44 и 48; 34 — это промах через раз, причём крайняя
справа кнопка удаляет. Подписи для озвучки у этих span'ов не было вовсе.

Плюс сама строка: `.li-sub` несла тип, состояние, задержку и число аккаунтов
одной строкой с многоточием. На 360px под неё остаётся ~114 пикселей при нужных
220 — многоточие съедало ровно то, ради чего на строку смотрят: работает прокси
или нет.

ЗАМЕРЕНО ПОСЛЕ ПРАВКИ: кнопки 36×36, расстояние между центрами ровно 44,
строка состояния видна целиком, горизонтальной прокрутки нет.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html

#: минимум по стороне для иконки в строке и минимальный зазор между ними;
#: 36 + 8 даёт 44 между центрами — величину, которой и определяется промах
MIN_ICON = 36
MIN_GAP = 8


def _css_value(html: str, selector: str, prop: str) -> int:
    m = re.search(re.escape(selector) + r"\{([^}]*)\}", html)
    assert m, f"нет правила {selector}"
    v = re.search(prop + r":\s*(\d+)px", m.group(1))
    assert v, f"в {selector} нет {prop}"
    return int(v.group(1))


def test_row_icons_are_big_enough_for_a_finger():
    html = miniapp_html()
    for sel in (".icon-btn", ".icon-btn.sm"):
        for prop in ("width", "height"):
            got = _css_value(html, sel, prop)
            assert got >= MIN_ICON, (
                f"{sel} {prop}={got}px — меньше {MIN_ICON}px, на телефоне "
                "это промах по соседней кнопке")


def test_neighbouring_row_actions_are_44px_apart():
    gap = _css_value(miniapp_html(), ".li-right", "gap")
    assert gap >= MIN_GAP, (
        f"зазор между действиями строки {gap}px: с иконкой {MIN_ICON}px это "
        f"{MIN_ICON + gap}px между центрами, а промах начинается раньше 44")


def test_no_hand_styled_delete_icons_left():
    """Голый span с cursor:pointer и padding:4px — это цель 26×33 без подписи."""
    html = miniapp_html()
    bad = re.findall(r'<span style="[^"]*cursor:pointer[^"]*padding:4px[^"]*"[^>]*onclick', html)
    assert not bad, (
        f"осталось {len(bad)} действий, набранных вручную вместо .icon-btn — "
        "они снова выйдут мелкими и без подписи для озвучки")


def test_destructive_row_actions_say_what_they_do():
    html = miniapp_html()
    for fn, label in (("deleteProxy", "Удалить прокси"),
                      ("deleteAr", "Удалить авто-ответ"),
                      ("deleteTpl", "Удалить шаблон"),
                      ("deleteKw", "Удалить ключевое слово")):
        m = re.search(r'<span class="icon-btn sm danger"[^>]*onclick="' + fn + r'\(', html)
        assert m, f"{fn}: действие не переведено на .icon-btn sm danger"
        tag = html[m.start():m.start() + 260]
        assert f'aria-label="{label}"' in tag, (
            f"{fn}: у кнопки нет подписи «{label}» — озвучка прочитает один эмодзи")


def test_the_proxy_row_shows_its_state_instead_of_an_ellipsis():
    html = miniapp_html()
    assert ".li-sub.wrap{" in html, "нет варианта строки, которая переносится"
    # обе строки прокси (вкладка и пул) переносятся, а не обрезаются
    subs = re.findall(r'<div class="li-sub[^"]*"[^>]*>\$\{type\} · \$\{alive\}', html)
    assert len(subs) == 2, f"ожидались две строки прокси, найдено {len(subs)}"
    for s in subs:
        assert "wrap" in s, "строка прокси всё ещё прячет состояние за многоточием"
