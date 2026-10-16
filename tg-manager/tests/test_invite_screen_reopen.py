"""Экран инвайтинга открывается второй раз без ошибки.

Что было. `openMassInvite` начинался со строки

    document.getElementById('massInviteAccsLoad').textContent = 'Загрузка аккаунтов…';

а ниже по той же функции делал `wrap.innerHTML = …`, где `wrap` —
`massInviteAccsWrap`, РОДИТЕЛЬ этого элемента. Первая отрисовка затирала
заглушку насовсем, и при втором открытии экрана getElementById возвращал null:

    Cannot set properties of null (setting 'textContent')

Падало это ДО первого `await`, то есть со второго открытия функция обрывалась
в самом начале — аккаунты-инвайтеры не загружались и история не рисовалась
вообще. Экран выглядел живым, а запускать инвайт было нечем.

Проверка намеренно ограничена ОДНОЙ функцией: внутри её тела область видимости
переменных разрешима однозначно. Попытка искать этот класс по всему файлу
регулярками даёт ложные срабатывания — одноимённые переменные в разных функциях
указывают на разные элементы.
"""
from __future__ import annotations

import pathlib
import re

# Экран инвайтинга вынесен в mini_app/screens/invite.js. Тест разбирает ТЕЛО
# функции, поэтому берём именно тот файл, где она объявлена, а не склейку:
# в склейке границы файлов условны. Разметка при этом осталась в index.html.
from tests.miniapp_source import miniapp_html, source_of

HTML = source_of("openMassInvite")
MARKUP = miniapp_html()


def _function_body(name: str) -> str:
    """Тело функции — от её объявления до строки, где закрывается верхний уровень."""
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\([^)]*\)\s*\{",
                  HTML, re.M)
    assert m, f"функция {name} не найдена"
    depth, i = 0, m.end() - 1
    while i < len(HTML):
        if HTML[i] == "{":
            depth += 1
        elif HTML[i] == "}":
            depth -= 1
            if depth == 0:
                return HTML[m.end():i]
        i += 1
    raise AssertionError(f"не нашёл конец функции {name}")


def _children_of(container_id: str) -> set[str]:
    """id, объявленные внутри элемента с данным id (по реальному дереву)."""
    from html.parser import HTMLParser

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    class T(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack: list[str | None] = []
            self.inside: set[str] = set()

        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            eid = d.get("id")
            if eid and container_id in [a for a in self.stack if a]:
                self.inside.add(eid)
            if tag not in VOID:
                self.stack.append(eid)

        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()

    t = T()
    t.feed(MARKUP)
    return t.inside


def test_loader_is_recreated_not_dereferenced():
    """Заглушку «Загрузка аккаунтов…» надо создавать заново, а не искать:
    её родителя эта же функция перерисовывает."""
    body = _function_body("openMassInvite")
    assert "getElementById('massInviteAccsLoad').textContent" not in body, (
        "элемент разыменовывается напрямую — при втором открытии экрана он уже "
        "затёрт перерисовкой massInviteAccsWrap, и функция упадёт на первой строке"
    )
    assert "massInviteAccsLoad" in body, "состояние загрузки исчезло вовсе"


def test_function_never_dereferences_what_it_wipes():
    """Общее правило для этой функции: нельзя разыменовывать без защиты элемент,
    лежащий внутри контейнера, который она сама перерисовывает."""
    body = _function_body("openMassInvite")

    wiped: set[str] = set(re.findall(
        r"getElementById\(\s*['\"]([A-Za-z0-9_\-]+)['\"]\s*\)\.innerHTML\s*=", body))
    # контейнер, положенный в переменную ВНУТРИ этой же функции
    for var, eid in re.findall(
            r"(?:const|let|var)\s+(\w+)\s*=\s*document\.getElementById\("
            r"\s*['\"]([A-Za-z0-9_\-]+)['\"]\s*\)", body):
        if re.search(r"\b" + re.escape(var) + r"\.innerHTML\s*=", body):
            wiped.add(eid)

    deref = set(re.findall(
        r"document\.getElementById\(\s*['\"]([A-Za-z0-9_\-]+)['\"]\s*\)\s*\.(?!\s)", body))

    doomed = set()
    for container in wiped:
        doomed |= (_children_of(container) & deref)

    assert not doomed, (
        "эти элементы исчезнут после первой же перерисовки родителя, а функция "
        f"обращается к ним без защиты: {sorted(doomed)}"
    )


def test_loader_reset_happens_before_the_await():
    """Сброс состояния должен идти до загрузки данных — иначе на экране
    остаются цифры прошлого открытия, пока идёт запрос."""
    body = _function_body("openMassInvite")
    assert "massInviteAccsLoad" in body
    assert body.index("massInviteAccsLoad") < body.index("await Promise.allSettled")
