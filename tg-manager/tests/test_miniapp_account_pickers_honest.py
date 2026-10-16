"""Регресс: выбор аккаунта не обрывается молча на первой сотне.

Что было. Все пикеры аккаунтов (Ghost, прогрев, фабрика персон, массовые
операции, фабрика групп, сеть контента) звали `/api/miniapp/accounts` без
единого параметра. Эндпоинт по умолчанию отдаёт `limit=100` и максимум 200 за
запрос, и в ответе честно пишет `page.has_more` и `page.filtered_total` — фронт
не читал ни то, ни другое.

Чем это плохо именно здесь. Продукт — про флот в сотни аккаунтов. При 340
аккаунтах пользователь видел 100 и не имел ни одного признака, что есть
остальные: в выпадашке нужного аккаунта просто не существовало, а отметка
«выбрать все» в чекбоксах накрывала треть парка — и операция уходила по трети,
без единого слова об этом. Поиска в пикерах не было, то есть добраться до
остальных было нечем в принципе.

Плюс два клиентских фильтра врали. Прогрев брал `acc_status==='active'`: у
здорового аккаунта это поле бывает NULL (эндпоинт отдаёт его как `'ok'`) — такой
аккаунт из прогрева выпадал; а выключенный со старым `acc_status='active'` в
список попадал. Массовые операции брали `a.is_active` — у забаненного и
спамблокнутого аккаунта этот флаг остаётся true, и пользователь отмечал
аккаунты, обречённые упасть в воркере. Оба среза теперь считает сервер
(`?filter=active`).
"""
from __future__ import annotations

import re

import pytest

from tests.miniapp_source import miniapp_source, source_of

# Пикеры: имя загрузчика → (якорь в разметке, нужен ли серверный срез «активные»)
PICKERS = {
    "_ghostLoadAccs": ("gc-account", False),
    "_warmupLoadAccs": ("warmupAccSel", True),
    "_gpLoadAccs": ("gpAccountsList", False),
    "_massOpsLoadAccs": ("mjAccounts", True),
    "_gfLoadAccounts": ("gfAccount", False),
    "_meshLoadAccs": ("mc-account", False),
    "_invLoadAccs": ("massInviteAccsWrap", True),
}


def _body(fn: str) -> str:
    """Тело функции целиком — по балансу фигурных скобок.

    Окном фиксированной длины пользоваться нельзя: сдвинулся код — проверка
    молча перестаёт смотреть туда, куда собиралась (`test_no_silently_disabled_guards`).
    """
    src = source_of(fn)
    m = re.search(r"^(?:async )?function " + re.escape(fn) + r"\s*\(", src, re.M)
    assert m, f"функция {fn} не найдена"
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"не закрылось тело {fn}")


def test_no_picker_calls_accounts_without_limit():
    """Голый вызов без limit — это и есть «молча первая сотня»."""
    src = miniapp_source()
    bare = re.findall(r"""api\(\s*['"]/api/miniapp/accounts['"]\s*\)""", src)
    assert not bare, (
        f"{len(bare)} вызовов /api/miniapp/accounts без параметров: сервер отдаст "
        "первые 100 и промолчит. Берите список через accPickerLoad()."
    )


@pytest.mark.parametrize("fn", sorted(PICKERS))
def test_picker_loads_through_helper(fn):
    assert "accPickerLoad(" in _body(fn), f"{fn} берёт аккаунты мимо accPickerLoad()"


@pytest.mark.parametrize("fn,anchor", sorted((f, a) for f, (a, _) in PICKERS.items()))
def test_picker_shows_honest_note(fn, anchor):
    """Приписка «показаны N из M» обязана появляться у своего же списка."""
    body = _body(fn)
    assert f"accPickerSetNote('{anchor}'" in body, (
        f"{fn} не подписывает список {anchor}: пользователь не узнает, что показано не всё"
    )
    assert "accPickerNote(" in body, f"{fn} не считает приписку"


@pytest.mark.parametrize("fn", sorted(f for f, (_, act) in PICKERS.items() if act))
def test_active_filter_is_server_side(fn):
    """«Активные» считает сервер: клиентский фильтр врал в обе стороны."""
    body = _body(fn)
    assert "accPickerLoad('active'" in body, f"{fn} не просит серверный срез активных"
    assert "a.acc_status==='active'" not in body, f"{fn} всё ещё фильтрует клиентом"
    assert ".filter(a=>a.is_active)" not in body, f"{fn} всё ещё фильтрует клиентом"


@pytest.mark.parametrize("anchor", sorted(a for a, _ in PICKERS.values()))
def test_every_picker_has_search(anchor):
    """Без поиска аккаунт за пределами показанных недостижим ничем."""
    src = miniapp_source()
    assert f"accPickerSearchBox('{anchor}'" in src, f"у пикера {anchor} нет поиска"


def test_helper_asks_for_the_server_maximum_and_reads_the_page():
    body = _body("accPickerLoad")
    assert "limit=200" in body, "помощник должен просить серверный максимум"
    assert "q=" in body and "encodeURIComponent(q)" in body, (
        "поиск обязан уходить на сервер: искать внутри загруженной страницы — "
        "значит искать ровно в той сотне, из-за которой всё и затевалось"
    )
    assert "has_more" in body and "filtered_total" in body, (
        "помощник не читает page.has_more/filtered_total — приписке неоткуда взяться"
    )


def test_note_is_silent_when_everything_is_shown():
    """Строка под каждым пикером там, где аккаунтов десяток, только мешает."""
    body = _body("accPickerNote")
    assert "if (!r) return '';" in body
    assert "r.more" in body, "приписка обязана зависеть от того, показано ли всё"
    assert body.rstrip().endswith("return '';\n}") or "return '';" in body


def test_search_box_is_inserted_once():
    """Пикеры открываются много раз — вставка на каждом открытии даст дубли id."""
    body = _body("accPickerSearchBox")
    assert "if (document.getElementById(inpId)) return;" in body, (
        "нет защиты от повторной вставки поля поиска"
    )


def test_note_element_is_reused_not_duplicated():
    body = _body("accPickerSetNote")
    assert "getElementById(noteId)" in body and "if (!note)" in body, (
        "приписка должна создаваться один раз и дальше переписываться"
    )
