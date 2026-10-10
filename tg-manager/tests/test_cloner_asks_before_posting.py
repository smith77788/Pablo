"""Клонер контента: цели выбираются, и публикация подтверждается.

Экран «📋 Клонер контента» принимал один источник и по кнопке «Клонировать»
пересылал последние 10 постов ВО ВСЕ управляемые каналы владельца. Выбора
каналов на экране не было, числа каналов — тоже, подтверждения — никакого.
Публикация в канал необратима, так что один случайный тап рассылал чужие
посты по всему хозяйству.

Тест держит три вещи: цели выбираются и проверяются на принадлежность
владельцу, количество постов ограничено, а запуск спрашивает подтверждение,
называя точные числа.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def _handler(name: str) -> str:
    i = API.index(f"async def {name}(request")
    j = API.index("\n    async def ", i + 10)
    return API[i:j]


def test_submit_accepts_explicit_targets():
    h = _handler("content_cloner_submit")
    assert 'body.get("channel_ids")' in h, "цели нельзя назвать явно — уйдёт во все"
    assert "channel_id = ANY($2::bigint[])" in h


def test_chosen_targets_must_belong_to_the_owner():
    h = _handler("content_cloner_submit")
    assert "owner_id=$1 AND channel_id = ANY" in h, "выбранные каналы не скоуплены"
    assert "есть чужой или удалённый" in h, \
        "чужой канал в списке молча отбрасывается вместо отказа"


def test_post_count_is_bounded():
    h = _handler("content_cloner_submit")
    assert re.search(r'validate_integer\(body\.get\("msg_count".*?max_val=100', h), \
        "количество постов без верхней границы"
    assert '"msg_count": msg_count' in h, "в операцию уходит жёстко зашитое число"


def test_screen_lets_you_pick_the_channels():
    assert 'id="clonerTargets"' in HTML, "на экране нет выбора каналов"
    assert "function loadClonerTargets(" in HTML
    body = _fn("loadClonerTargets")
    assert "cloner-ch" in body and "checkbox" in body
    assert "goTab('channels')" in body, "без своих каналов экран не говорит, что делать"


def test_launch_confirms_with_real_numbers():
    body = _fn("submitCloner")
    assert "askConfirm(" in body, "рассылка по каналам уходит без подтверждения"
    assert "Отменить публикацию в канале будет нельзя" in body
    assert "picked.length" in body and "boxes.length" in body, \
        "подтверждение не считает, во сколько каналов уйдёт"
    assert "channel_ids" in body and "msg_count" in body


def test_sending_to_everything_is_an_explicit_choice():
    body = _fn("submitCloner")
    assert "toAll" in body
    assert "уйдёт во все" in body, \
        "при пустом выборе владелец не предупреждён, что уйдёт во все каналы"


def test_history_opens_the_operation_report():
    body = _fn("loadClonerHistory")
    assert "openOpDetail(" in body, "из истории нельзя посмотреть отчёт операции"
    assert "target_refs" in body, "не видно, во сколько каналов шло клонирование"
    assert re.search(r"errHtml\(errRu\(e\)\s*,", body), "ошибка истории без «Повторить»"


def test_screen_warns_that_publishing_is_irreversible():
    assert "Публикация необратима" in HTML
