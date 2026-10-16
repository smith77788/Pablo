"""Экран без данных перестал быть тупиком.

Девять экранов приходили пустыми и на этом заканчивались: «Данных нет»,
«Нет конфликтов», «Нет ботов», «Нет пересечений», «Доступ к облаку — по
подписке. Обратитесь к владельцу». Ни кнопки, ни объяснения, что вообще сюда
попадает и откуда. На подэкране таббар скрыт, так что выход был только
«назад» — владелец возвращался ровно туда, откуда пришёл, ничего не узнав.

Ошибка была хуже: половина экранов рисовала `empty('⚠️','Ошибка', текст)` —
без кнопки «Повторить», хотя общий `errHtml` её даёт.

Здесь проверяется, что у каждой пустоты есть следующий шаг и объяснение, а у
каждой ошибки — повтор. Плюс два числа, у которых список был под рукой:
строка риска аккаунта открывает сам аккаунт (account_id сервер отдавал с
самого начала), число ботов в сети — список ботов.
"""
from __future__ import annotations

import functools
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")

# экран → функция, которая его рисует
SCREENS = {
    "s-semantic": "openSemanticMemory",
    "s-physics": "openPhysics",
    "s-dna": "openAudienceDna",
    "s-presencemap": "openPresenceMap",
    "s-network": "openNetwork",
    "s-uchconflicts": "openUchConflicts",
    "s-uchreminders": "openUchReminders",
    "s-graph": "openGraph",
}


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(name)


def _code(body: str) -> str:
    """Тело без комментариев: они цитируют старые тексты."""
    return "\n".join(l for l in body.split("\n") if not l.strip().startswith("//"))


def test_the_screens_still_exist():
    """Антивакуумность."""
    h = _html()
    for sid in SCREENS:
        assert f'id="{sid}"' in h, f"экран {sid} пропал"


@pytest.mark.parametrize("fn", sorted(set(SCREENS.values())))
def test_empty_state_offers_a_next_step(fn):
    """`empty(...)` без четвёртого аргумента — экран, из которого нет выхода."""
    body = _code(_js_func(fn))
    empties = re.findall(r"empty\(", body)
    assert empties, f"{fn}: пустого состояния нет — проверка потеряла смысл"
    # хотя бы одна кнопка в пустоте
    assert "{label:" in body, f"{fn}: пустота без кнопки «куда дальше»"


@pytest.mark.parametrize("fn", sorted(set(SCREENS.values())))
def test_error_offers_a_retry(fn):
    body = _code(_js_func(fn))
    assert f"errHtml(" in body, f"{fn}: ошибка без кнопки «Повторить»"
    assert "empty('⚠️','Ошибка'" not in body.replace(" ", ""), \
        f"{fn}: ошибка рисуется пустотой, а не errHtml"


@pytest.mark.parametrize("fn", sorted(set(SCREENS.values())))
def test_retry_names_its_own_screen(fn):
    """errHtml без второго аргумента даёт кнопку, которая ничего не перезагружает."""
    body = _code(_js_func(fn))
    # Первый аргумент — само сообщение, и оно давно не плоская строка: текст
    # ошибки прогоняется через errRu(e), то есть содержит свои скобки. Класс
    # [^)]* на таком вызове не совпадает НИКОГДА — проверка краснела на здоровом
    # коде, а не ловила тупик.
    assert re.search(r"errHtml\(.*?,\s*'" + re.escape(fn) + r"\(\)'\)", body,
                     re.DOTALL), \
        f"{fn}: «Повторить» не знает, что повторять"


def test_risk_row_opens_the_account():
    """account_id сервер отдавал всегда — строка «риск 91%» никуда не вела."""
    body = _js_func("openPhysics")
    assert "openAccount(" in body and "r.account_id" in body


def test_bot_count_opens_the_bots():
    body = _js_func("openNetwork")
    assert "goTab('bots')" in body


def test_paywalls_lead_to_the_subscription():
    """«Обратитесь к владельцу» и «Приобретите через бота» — не действия."""
    h = _html()
    assert "Обратитесь к владельцу для подключения" not in h
    assert "Приобретите лицензию через бота" not in h
    assert h.count('onclick="openBilling()"') >= 2, "у замков нет пути к подписке"


def test_empty_states_say_what_lands_here():
    """«Данных нет» не объясняет ни что это, ни откуда данные берутся."""
    h = _html()
    for phrase in ("Боты ещё не собрали память о пользователях",
                   "Нет данных риска",
                   "Данных ДНК нет",
                   "Нет данных по гео"):
        assert phrase not in h, f"остался невнятный текст: «{phrase}»"
