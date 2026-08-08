"""Регресс: каждый id, к которому обращается `getElementById(...)` в мини-аппе,
обязан где-то в index.html ОПРЕДЕЛЯТЬСЯ (статическим `id="…"` или в шаблонной
строке, создающей элемент в рантайме).

Баг (со скрина пользователя, экран «Хранилище»): коммит-буст заменил в шапке
`<div class="hdr-user" id="vaultCnt">` на колокольчик уведомлений, но
`renderVaultChats` продолжал делать
`document.getElementById('vaultCnt').textContent = …`. Элемента больше не было →
`getElementById` возвращал null → «Cannot set properties of null (setting
'textContent')», и весь экран Хранилища падал в красную плашку с «Повторить».

Тот же класс, что `test_miniapp_safe_helpers_defined.py` (вызвано-но-не-определено),
только для DOM-id. Без фикса тест падает (vaultCnt не определён), с фиксом —
проходит.
"""
from __future__ import annotations

import os
import re

_HTML = os.path.join(os.path.dirname(__file__), "..", "mini_app", "index.html")


def _load() -> str:
    return open(_HTML, encoding="utf-8").read()


def test_every_getElementById_target_is_defined_somewhere():
    src = _load()
    # Определённые id: статические атрибуты И шаблонные строки, создающие DOM.
    defined = set(re.findall(r"""\bid=["']([\w-]+)["']""", src))
    # Обращения только по строковому литералу (динамический id по переменной — не наш кейс).
    refs = set(re.findall(r"""getElementById\(\s*["']([\w-]+)["']\s*\)""", src))
    missing = sorted(refs - defined)
    assert not missing, (
        "getElementById(...) обращается к id, которого нет в index.html "
        "(вернёт null → «Cannot set properties of null» и падение экрана): "
        f"{missing}"
    )


def test_vault_chat_counter_element_exists():
    # Точечный якорь на конкретный баг: счётчик чатов в шапке Хранилища,
    # в который пишет renderVaultChats, должен присутствовать.
    src = _load()
    assert re.search(r"""id=["']vaultCnt["']""", src), (
        "В шапке экрана Хранилища должен быть элемент #vaultCnt — в него пишет "
        "renderVaultChats('… чат.'); без него экран падает."
    )
