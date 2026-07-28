"""Чекбоксы внутри `.field` были невидимы — выбрать аккаунты для инвайта нельзя.

ЖАЛОБА ПОЛЬЗОВАТЕЛЯ: «не выбираются аккаунты для инвайтинга», «кривые окна в
разделах».

ЧТО БЫЛО СЛОМАНО. Правило `.field textarea,.field input,.field select` писалось
под ввод ТЕКСТА: `width:100%`, `padding:10px 12px`, `appearance:none`. Селектор
`input` без уточнения типа накрывал и чекбоксы: `appearance:none` отключает
нативную отрисовку (галочки нет, своей замены не написано), а `width:100%`
растягивает контрол на всю строку. Замер в браузере: 7 контролов рендерились
**0×0 пикселей** — их нельзя было ни увидеть, ни нажать.

Задеты: список аккаунтов-инвайтеров (`#massInviteAccsWrap`) и «Что копировать» в
клон-адаптации (`clFname`…`clFcmds`). Пользователь тыкал в аккаунты, ничего не
происходило, и инвайт уходил «на все активные» — то есть выбор не работал молча.

Почему это дожило до жалобы: часть чекбоксов по приложению несёт инлайновый
`width:18px;height:18px` — симптом лечили поштучно там, где замечали, не находя
общей причины. Ниже причина снята один раз и закреплена гейтом.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return "\n".join(m.group(1) for m in re.finditer(r"<style>(.*?)</style>", HTML, re.DOTALL))


def test_checkbox_override_exists():
    css = _css()
    m = re.search(r"\.field input\[type=checkbox\][^{]*\{([^}]*)\}", css)
    assert m, (
        "нет правила, возвращающего чекбоксам нативный вид: правило для текстовых "
        "полей делает их невидимыми"
    )
    body = m.group(1)
    assert "appearance:auto" in body, (
        "appearance:none оставляет пустой прямоугольник — галочки не будет никогда"
    )
    assert "width:auto" in body, "width:100% растягивает чекбокс на всю строку"


def test_override_covers_radio_too():
    css = _css()
    m = re.search(r"\.field input\[type=checkbox\][^{]*\{", css)
    assert m and "radio" in css[m.start():m.start() + 120], (
        "радио-кнопки ломаются тем же правилом — исключать надо оба типа"
    )


def test_override_comes_after_the_text_rule():
    """Каскад: правило-исключение обязано идти ПОСЛЕ общего, иначе не победит."""
    css = _css()
    general = css.find(".field textarea,.field input,.field select{")
    override = css.find(".field input[type=checkbox]")
    assert general != -1 and override != -1
    assert override > general, "исключение выше общего правила не сработает"


def test_invite_account_checkboxes_are_in_a_field():
    """Проверка предпосылки: если разметку переделают и чекбоксы уедут из `.field`,
    правило-исключение станет неактуальным — тест должен это заметить."""
    m = re.search(r'<div id="massInviteAccsWrap"', HTML)
    assert m, "список аккаунтов-инвайтеров не найден"
    head = HTML[max(0, m.start() - 400):m.start()]
    assert 'class="field"' in head, (
        "список выехал из .field — перепроверить, каким правилом теперь стилизуются "
        "чекбоксы"
    )


def test_no_bare_appearance_none_on_checkboxes_anywhere():
    """Тот же дефект не должен приезжать через инлайновые стили."""
    bad = re.findall(r'<input[^>]*type=["\']?checkbox[^>]*appearance:\s*none', HTML)
    assert not bad, f"чекбокс с appearance:none — снова невидим: {bad[:3]}"
