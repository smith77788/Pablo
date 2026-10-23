"""В каждый экран мини-аппа можно попасть, и ни одна функция не висит мёртвой.

НАХОДКА, РАДИ КОТОРОЙ ЭТО НАПИСАНО (16.10.2026). У семи экранов была полная
разметка, рабочий загрузчик и живые ручки на сервере — и ни одной кнопки
входа: ни плитки в каталоге «Все функции», ни вызова откуда-либо ещё. То есть
семь готовых разделов продукта владелец не мог открыть никак.

Две из них были не просто экранами, а единственной дверью к операции:

* «Импорт групп с аккаунтов» — кнопка жила только на экране-дубле фабрики
  групп. Ручка /api/miniapp/groups/import_all на сервере есть, а нажать её
  было негде: групп с аккаунтов нельзя было завести ни одним способом.
* «Рассылка по сети ботов» — модалку открывала только функция, на которую
  никто не ссылался. Ручка /api/miniapp/network_broadcast тоже на месте.

ПОЧЕМУ ЭТО НЕ ЛОВИЛОСЬ. Каталог «Все функции» строится из разметки, а боковое
меню — из каталога (buildDrawer читает плитки экрана s-more). Значит экран без
плитки не появляется НИГДЕ, и проверка «есть ли такая функция» этого не видит:
функция есть, экран есть, разметка есть. Нет только ссылки.

ЧТО СТЕРЕЖЁТ ЭТОТ ФАЙЛ. Во-первых, у каждого экрана есть функция, которая его
открывает, и на эту функцию кто-то ссылается. Во-вторых, в мини-аппе нет
функций, на которые не ссылается никто. Вторая половина — про мёртвый код: в
тот же заход нашлись и удалены десять функций без единой ссылки, включая
экран-дубль фабрики групп (единая фабрика умеет и каналы, и группы по галочке)
и «openKeywords», оставленный «на всякий случай» рядом с openKeywordsScreen.

ВАЖНО ПРО ИЗМЕРИТЕЛЬ. Имя функции фронта может приходить С СЕРВЕРА: быстрые
действия по итогу операции присылаются как {"fn": "openChainStep"} и
вызываются по имени. Поэтому ссылки ищутся не только во фронте, но и в
services/ и bot/ — иначе проверка потребовала бы удалить работающую проводку.
Без этой половины измеритель давал 15 находок вместо 13.
"""
from __future__ import annotations

import collections
import functools
import re

from tests.miniapp_source import (
    ROOT,
    js_function,
    js_functions,
    miniapp_html,
    miniapp_source,
)

# Экраны-вкладки открывает goTab/navGo по имени вкладки, а не push('s-…'):
# у них нет и не должно быть функции-открывашки.
TAB_SCREENS = {"s-accounts", "s-bots", "s-broadcasts", "s-more"}

_FN = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)
# Идентификатор JS целиком: `$` — его часть, поэтому `foo` в `foo$bar`
# ссылкой на `foo` не считается.
_WORD = re.compile(r"[A-Za-z_$][\w$]*")


@functools.lru_cache(maxsize=1)
def _code() -> str:
    """Фронт без строк-комментариев: ссылка из комментария — не ссылка."""
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in miniapp_source().splitlines())


@functools.lru_cache(maxsize=1)
def _server() -> str:
    """Сервер и бот: оттуда приходят имена функций фронта для быстрых действий."""
    return "\n".join(p.read_text(encoding="utf-8")
                     for d in ("services", "bot")
                     for p in (ROOT / d).rglob("*.py"))


@functools.lru_cache(maxsize=1)
def _names() -> tuple[str, ...]:
    return tuple(sorted({m.group(1) for m in _FN.finditer(_code())}))


def _screens() -> list[str]:
    return sorted(set(re.findall(r'<div class="screen" id="(s-[\w-]+)"', miniapp_html())))


@functools.lru_cache(maxsize=1)
def _referenced() -> frozenset[str]:
    """Имена функций, на которые ссылается хоть кто-то, кроме них самих.

    Считать `\bимя\b` по всему фронту для каждой из 1432 функций — минута на
    проход (40 мс на имя), и столько же на серверном слое. Один проход
    токенизатором даёт те же числа за десятые доли секунды: достаточно знать,
    сколько раз имя встречается всего и сколько — внутри собственного тела.
    """
    bodies = js_functions()
    outside = collections.Counter(_WORD.findall(_code()))
    on_server = set(_WORD.findall(_server()))
    live = set()
    for name in _names():
        body = bodies.get(name) or js_function(name)
        inside = collections.Counter(_WORD.findall(body))[name]
        if outside[name] - inside > 0 or name in on_server:
            live.add(name)
    return frozenset(live)


def test_the_detector_sees_the_app_at_all():
    """Самопроверка: пустой список экранов или функций сделал бы файл зелёным зря."""
    assert len(_screens()) > 150, f"экранов найдено {len(_screens())} — измеритель сломан"
    assert len(_names()) > 1000, f"функций фронта найдено {len(_names())}"
    assert len(_server()) > 500_000, "серверный слой не читается"
    assert len(_referenced()) > 1000, "ссылки почти не находятся — измеритель слеп"


def test_every_screen_can_be_opened():
    bodies, live = js_functions(), _referenced()
    pushers: dict[str, list[str]] = {}
    for name in _names():
        for sid in re.findall(r"push\('([^']+)'\)", bodies.get(name) or js_function(name)):
            pushers.setdefault(sid, []).append(name)

    orphans, unreachable = [], []
    for sid in _screens():
        if sid in TAB_SCREENS:
            continue
        opens = pushers.get(sid, [])
        if not opens:
            orphans.append(sid)
        elif not any(n in live for n in opens):
            unreachable.append((sid, opens))

    assert not orphans, (
        "экран есть в разметке, но ни одна функция его не открывает: "
        f"{orphans}. Либо завести вход, либо удалить разметку")
    assert not unreachable, (
        "в экран нельзя попасть: открывашка есть, но на неё никто не "
        f"ссылается — ни плитка каталога, ни другой экран, ни сервер: "
        f"{unreachable}. Каталог «Все функции» — это и есть боковое меню, "
        "так что экран без плитки не появляется нигде")


def test_no_function_is_left_dead():
    live = _referenced()
    dead = [n for n in _names() if n not in live]
    assert not dead, (
        f"на эти функции не ссылается никто — ни фронт, ни сервер: {dead}. "
        "Либо подключить, либо удалить: мёртвая функция в монолите читается "
        "как рабочая и переживает правки, которые её сломали")


def test_the_two_operations_found_this_way_are_reachable():
    """Импорт групп и рассылка по сети — вход к ним, а не только ручка."""
    html = miniapp_html()
    assert "importAllResources('groups')" in html, (
        "импорт групп с аккаунтов снова некуда нажать, хотя ручка "
        "/api/miniapp/groups/import_all на сервере есть")
    assert 'onclick="openNetBcModal()"' in html, (
        "рассылку по сети ботов снова нельзя открыть")
    # Массовая операция, которую не отменить одной кнопкой, обязана спрашивать.
    assert "askConfirm(" in js_function("sendNetBc"), (
        "рассылка подписчикам всех ботов уходит без подтверждения")
