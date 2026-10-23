"""Листание по OFFSET поверх неуникальной сортировки теряет и повторяет людей.

НАХОДКА (23.10.2026). Список контактов — главный экран хаба — листается
кнопкой «Показать ещё» через `offset`, а сортируется по НЕУНИКАЛЬНОМУ
столбцу: `first_name`, `username`, `discovered_at DESC`, `last_synced_at DESC`,
`last_changed_at DESC`. Postgres не обещает порядок внутри равных значений, и
внутри одного ORDER BY у него нет причин его держать: достаточно, чтобы строка
изменилась между запросами страниц, и она переезжает.

А между страницами список и меняется: синхронизация пишет `last_synced_at`,
владелец ставит метку, движок доверия пересчитывает поле. У сортировок
`synced`/`changed` строки сортируются ровно по тому полю, которое пишет фоновая
синхронизация.

Что это значит для владельца: он листает «все контакты» до конца, и часть людей
не видит НИ РАЗУ, а часть видит дважды. Молча. На том же срезе стоят массовые
действия и выгрузка.

Доказано живьём (ниже, test_the_contact_list_walk_shows_everyone_once): десять
контактов с одинаковым именем, страницы по четыре, между первой и второй
страницей — обычное действие владельца над одной строкой. Итог без добивочного
ключа: выдано 10, уникальных 9, один человек показан дважды, другой — ни разу.
С `, id` — ровно десять разных.

То же у `parsed_audience` (аудитория парсера листается тем же способом поверх
`parsed_at DESC`, а парсер пишет всю пачку одним временем, так что ties там не
исключение, а норма) и у `mp_catalog` (`subs DESC NULLS LAST`).

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. убрать добивочный `, id` из сортировки списка контактов -> красный
     test_the_contact_list_walk_shows_everyone_once (выдано 10, уникальных 9);
  2. убрать `, id DESC` из parsed_audience -> красный
     test_the_parsed_audience_walk_shows_everyone_once;
  3. вернуть мерке поиск слова OFFSET по всему файлу вместо разбора литералов
     -> красный test_the_detector_sees_the_paging_queries: в выдаче появляются
     докстринг про `OFFSET -5` и чужие сортировки, то есть сторож начинает
     ругаться не на то.

МЕРКУ ПРИШЛОСЬ ЧИНИТЬ ДВАЖДЫ, и второй раз показательнее первого:
`ast.walk` выдаёт и саму f-строку, и её внутренние Constant-куски. Хвост вида
«ORDER BY … LIMIT $2 OFFSET $3» — такой кусок: в нём есть и сдвиг, и
сортировка, а SELECT остался в начале f-строки. Из-за этого один запрос
считался дважды, и один из двух «запросов» был обрывком без SELECT. Поймано
не глазами, а собственной самопроверкой: `test_the_detector_sees_the_paging_queries`
требует SELECT в том же литерале. Поэтому обход теперь не заходит внутрь
f-строк.

Самопроверка требует СВОЙСТВО (есть SELECT), а не имя файла. Сначала она была
написана как «в выдаче нет такого-то имени файла» — и это поймал
`test_tests_do_not_pin_the_monolith`: такая проверка ослепла бы при первом же
выносе ручки в соседний модуль. Имя монолита не упоминается здесь даже в
кавычках в прозе: храповик считает такое упоминание привязкой, и правильно —
кавычки в докстринге от кавычек в коде он отличить не может, а отличать
пришлось бы ему, а не автору.
"""
from __future__ import annotations

import os
import re

import pytest

from tests.miniapp_source import api_files, api_source

DSN = os.environ.get("INFRAGRAM_TEST_DSN")
live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 992001

# Ключи, которые сами задают порядок: первичный ключ строки — и, отдельным
# списком, ключ САМОЙ ВЫДАЧИ там, где он уникален по построению запроса:
#
#   * `channel_id` — запрос списка каналов идёт через `SELECT DISTINCT
#     channel_id`, и ORDER BY в DISTINCT-запросе вообще может ссылаться только
#     на выбранные столбцы; после DISTINCT канал в выдаче ровно один раз;
#   * `mb.bot_id` — список ботов сгруппирован `GROUP BY mb.bot_id`, то есть
#     строка выдачи одна на бота;
#   * `user_id` — подписчики читаются при ФИКСИРОВАННОМ `bot_id=$1`, а в
#     bot_users пара «бот+человек» уникальна;
#   * `msg_id` — архив переписки читается при ФИКСИРОВАННОМ `chat_id=$2`
#     (условие стоит в запросе всегда, не по фильтру), а msg_id — это id
#     сообщения в этом чате.
#
# Список именованный, а не «любой столбец с id в названии»: `account_id` или
# `funnel_id` в выдаче по многу строк, и добивкой они не были бы.
#   * `oa.id` — тот же `id`, но у таблицы под алиасом в join'е;
#   * `entity_id` — ключ reg_check_cache это ПАРА (entity_id, entity_type), и
#     сортировка там несёт оба ключа: `… , entity_type, entity_id DESC`. Сам
#     по себе entity_id в этой таблице не уникален, уникальна пара, которую
#     сортировка и выписывает целиком.
_UNIQUE = ("id", "uc.id", "unified_contacts.id", "r.id", "q.id", "a.id",
           "b.id", "oq.id", "p.id", "s.id", "ba.id", "mb.bot_id", "oa.id",
           "channel_id", "user_id", "msg_id", "entity_id")

# Запросом со сдвигом считается ТОЛЬКО строковый литерал, в котором есть и
# подстановка OFFSET, и свой ORDER BY. Первая мерка искала слово OFFSET где
# угодно в файле — включая комментарий «`OFFSET -5` Postgres не принимает» — и
# брала ближайший ORDER BY СВЕРХУ, то есть сортировку ЧУЖОГО запроса. Она дала
# четырнадцать «находок», из которых настоящими не были даже те, что совпали.
# Детектор, дающий десятки находок в зрелом коде, почти всегда сломан.
_OFFSET = re.compile(r"\bOFFSET\s*(?:\$\d+|\$\{|\{)")
_ORDER = re.compile(r"\bORDER BY\s+(.*?)(?=\s+LIMIT\b|\s+OFFSET\b|$)",
                    re.S | re.I)

# Склейка f-строк в ИСХОДНИКЕ: запрос собран как
# `f"... ORDER BY created_at DESC " f"LIMIT ${n} OFFSET ${m}"`, и в тексте
# между сортировкой и LIMIT стоит `" f"`. Без этой нормализации регексп не
# видит границу и забирает в последний ключ сортировки кусок питоновского
# синтаксиса — `created_at DESC " f"LIMIT $3` вместо `created_at DESC`.
# Третья по счёту правка этой мерки, и каждая находилась её же самопроверкой.
_GLUE = re.compile(r"""['"]\s*f?\s*['"]|\\n""")


def _normalise(lit: str) -> str:
    """Исходник f-строки — в текст запроса: склейку считаем пробелом."""
    return _GLUE.sub(" ", lit)


def _string_nodes(node):
    """Строковые литералы, НЕ заходя внутрь f-строк.

    `ast.walk` выдаёт и саму f-строку, и её внутренние `Constant`-куски. Хвост
    вида «ORDER BY … LIMIT $2 OFFSET $3» — такой кусок: в нём есть и сдвиг, и
    сортировка, а SELECT остался в начале f-строки. Из-за этого первая версия
    считала один запрос дважды, и один из двух «запросов» был обрывком без
    SELECT. Обход обязан останавливаться там, где узел имеет смысл только
    целиком.
    """
    import ast

    if isinstance(node, ast.JoinedStr):
        yield node
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
        return
    for child in ast.iter_child_nodes(node):
        yield from _string_nodes(child)


def _last_key(order: str) -> str:
    keys = [k.strip() for k in order.split(",") if k.strip()]
    if not keys:
        return ""
    last = keys[-1].lower()
    last = re.sub(r"\s+(asc|desc)$", "", last)
    last = re.sub(r"\s+nulls\s+(first|last)$", "", last).strip()
    return last


def order_is_total(order: str) -> bool:
    """Последний ключ сортировки обязан быть уникальным.

    Уникальный ключ ПОСЛЕДНИМ, а не где-нибудь: порядок задаётся слева
    направо, и `ORDER BY id, title` уникален, а `ORDER BY title, name` — нет.

    Сортировка, собранная переменной (`ORDER BY {order}`), статически не
    судится — но `ORDER BY {order_by}, id` судится: добивка здесь литерал, и
    она последняя, что бы ни подставилось раньше.
    """
    return _last_key(order) in _UNIQUE


def order_is_dynamic(order: str) -> bool:
    """Последний ключ подставляется из переменной — читать надо глазами."""
    return "{" in _last_key(order)


def _paging_queries() -> list[tuple[str, int, str | None, str]]:
    """Запросы со сдвигом: файл, строка, их СОБСТВЕННАЯ сортировка, литерал."""
    import ast

    out = []
    for p in api_files():
        src = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in _string_nodes(tree):
            lit = (node.value if isinstance(node, ast.Constant)
                   else ast.get_source_segment(src, node) or "")
            if not _OFFSET.search(lit):
                continue
            m = _ORDER.search(_normalise(lit))
            out.append((p.name, node.lineno,
                        " ".join(m.group(1).split()) if m else None, lit))
    return out


def test_the_detector_knows_a_total_order_from_a_partial_one():
    """Проба: без неё правило ниже могло бы быть каким угодно."""
    assert order_is_total("parsed_at DESC, id DESC")
    assert order_is_total("first_name, last_name, id")
    assert order_is_total("id")
    assert not order_is_total("parsed_at DESC")
    assert not order_is_total("first_name, last_name")
    assert not order_is_total("subs DESC NULLS LAST")
    # Уникальный ключ не последним порядок не задаёт.
    assert not order_is_total("id, title")
    assert order_is_total("a.id")
    # Столбец с «id» в названии добивкой не становится: таких строк много.
    assert not order_is_total("created_at DESC, account_id")
    assert not order_is_total("funnel_id")
    assert not order_is_total("bot_id")
    assert order_is_total("subscriber_count DESC, mb.bot_id")
    assert order_is_total("last_seen DESC NULLS LAST, user_id")


def test_the_detector_sees_the_paging_queries():
    """Пустая выдача сделала бы правило ниже зелёным ни на чём."""
    qs = _paging_queries()
    assert len(qs) >= 10, f"запросов со сдвигом найдено мало: {len(qs)}"
    assert all(o for _, _, o, _l in qs), (
        "запрос со сдвигом вообще без сортировки: "
        + str([(q[0], q[1]) for q in qs if not q[2]]))
    assert "_list_offset(" in api_source(), "общей двери для сдвига больше нет"
    # Мерка обязана отличать ЗАПРОС от прозы о запросе: в слое есть докстринг
    # «`OFFSET -5` Postgres не принимает», и первая версия считала его
    # находкой. Признак запроса — SELECT в том же литерале. Проверяем свойство,
    # а не имя файла: привязка к имени ослепла бы при первом выносе ручки.
    not_queries = [(w, ln) for w, ln, _o, lit in qs if "SELECT" not in lit.upper()]
    assert not not_queries, (
        f"мерка считает запросом текст без SELECT: {not_queries}")


def test_every_paged_query_has_a_tiebreaker():
    """Листание по сдвигу обязано стоять на полном порядке."""
    bad = []
    for where, line, order, _lit in _paging_queries():
        if not order:
            bad.append(f"{where}:{line}: сдвиг без сортировки вообще")
        elif not order_is_total(order):
            bad.append(f"{where}:{line}: ORDER BY {order[:80]}")
    assert not bad, (
        "Страницы могут повторять и терять записи: порядок внутри равных "
        "значений Postgres не обещает:\n  " + "\n  ".join(bad))


def test_the_contact_list_sort_carries_the_tiebreaker():
    """Сортировки списка контактов задаются картой — добивка нужна у всех."""
    from tests.miniapp_source import py_function
    src = py_function("services/contacts_hub/repository.py", "get_contacts")
    m = re.search(r"order\s*=\s*(.+)", src)
    assert m, "сортировка списка контактов больше не собирается"
    assert "id" in m.group(1), (
        "ни одна сортировка списка контактов не добивается уникальным ключом — "
        "«Показать ещё» снова будет повторять и терять людей")


# ── Тот же класс вне слоя мини-аппа ─────────────────────────────────────────
#
# Класс не про мини-апп, а про листание: параллельная сессия нашла его у себя
# («Аудиторию инвайта читали страницами и теряли людей», op_worker.py) ровно
# после того, как он был описан здесь. Поэтому мерка смотрит на ВЕСЬ код.
#
# Внутри слоя мини-аппа правило жёсткое — ноль. Снаружи — храповик: в
# bot/handlers осталось четырнадцать таких запросов, это списки меню бота,
# другой слой и другая сессия. Число обязано только убывать; сами находки
# переданы владельцу того слоя.

# Долг выплачен целиком: четырнадцать запросов слоя bot/handlers получили
# добивочный ключ (см. коммит про листание в bot/handlers). Ноль значит, что
# правило здесь теперь такое же жёсткое, как в services/.
OUTSIDE_CEILING = 0

# Сортировка, собранная переменной: статически не судится, проверена чтением.
#   * contacts_hub/repository.py — order собирается из карты сортировок, и к
#     любой из них дописано `, id` (см. get_contacts);
#   * vault_service.py — order это `msg_date …, msg_id …`, а msg_id уникален
#     в пределах чата, которым запрос скоупится всегда (`chat_id=$2` стоит в
#     условии безусловно, не по фильтру).
DYNAMIC_CEILING = 2


def _all_paging_queries() -> list[tuple[str, int, str]]:
    """Запросы со сдвигом по всему коду, кроме слоя мини-аппа."""
    import ast
    import pathlib as _pl

    root = _pl.Path(__file__).resolve().parents[1]
    out = []
    for path in (sorted((root / "services").rglob("*.py"))
                 + sorted((root / "bot").rglob("*.py"))):
        if path.name.startswith("mini_app_"):
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in _string_nodes(tree):
            lit = (node.value if isinstance(node, ast.Constant)
                   else ast.get_source_segment(src, node) or "")
            if not _OFFSET.search(lit) or "SELECT" not in lit.upper():
                continue
            m = _ORDER.search(_normalise(lit))
            out.append((str(path.relative_to(root)), node.lineno,
                        " ".join(m.group(1).split()) if m else ""))
    return out


def test_the_rest_of_the_code_only_gets_better():
    """Храповик: запросов без добивки вне слоя мини-аппа только меньше."""
    rows = _all_paging_queries()
    assert len(rows) >= 20, f"запросов со сдвигом вне слоя мало: {len(rows)}"
    bad = [f"{w}:{ln}  ORDER BY {o}" for w, ln, o in rows
           if not order_is_total(o) and not order_is_dynamic(o)]
    assert len(bad) <= OUTSIDE_CEILING, (
        f"запросов без добивки стало {len(bad)} вместо {OUTSIDE_CEILING}:"
        + "\n  " + "\n  ".join(bad))
    if len(bad) < OUTSIDE_CEILING:
        raise AssertionError(
            f"долг уменьшился до {len(bad)} — опустите OUTSIDE_CEILING тем же "
            "коммитом, иначе храповик перестанет сторожить рост")
    in_services = [b for b in bad if b.startswith("services/")]
    assert not in_services, (
        "в services/ снова появился запрос без добивки — этот слой уже чист, "
        "и долг здесь не копится:\n  " + "\n  ".join(in_services))


def test_dynamic_orders_are_known_and_checked_by_hand():
    """Сортировка из переменной — та, что проверена чтением, и только она."""
    rows = _all_paging_queries()
    dyn = [f"{w}:{ln}" for w, ln, o in rows if order_is_dynamic(o)]
    assert len(dyn) <= DYNAMIC_CEILING, (
        f"запросов с сортировкой из переменной стало {len(dyn)} вместо "
        f"{DYNAMIC_CEILING}: мерка их не судит, значит каждый надо прочитать "
        f"глазами и записать причину здесь же.\n  " + "\n  ".join(dyn))
    assert dyn, "ни одного — мерка перестала их различать"


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


async def _walk(pool, fetch_page, touch):
    """Листать так, как листает владелец: страница, действие, следующая."""
    seen = []
    for n, off in enumerate((0, 4, 8)):
        seen += await fetch_page(off)
        if n == 0:
            await touch()
    return seen


@live
def test_the_contact_list_walk_shows_everyone_once():
    """Проход по всему списку обязан показать каждого ровно один раз."""
    import asyncpg

    from services.contacts_hub.repository import get_contacts

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute(
                "DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
            ids = []
            for i in range(10):
                # Одинаковое имя — обычное дело: тёзки, и ещё больше пустых
                # имён, которые тоже равны между собой.
                ids.append(await pool.fetchval(
                    "INSERT INTO unified_contacts(owner_id, first_name, username)"
                    " VALUES($1,'Иван',$2) RETURNING id", OWNER, f"u{i}"))

            async def page(off):
                r = await get_contacts(pool, OWNER, limit=4, offset=off)
                return [str(c["id"]) for c in r["contacts"]]

            async def touch():
                # Владелец пометил одного избранным — строка переписана.
                await pool.execute(
                    "UPDATE unified_contacts SET is_favorite = NOT "
                    "COALESCE(is_favorite,false) WHERE id=$1", ids[0])

            seen = await _walk(pool, page, touch)
            await pool.execute(
                "DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
            return seen, [str(x) for x in ids]
        finally:
            await pool.close()

    seen, ids = _run(go())
    dupes = sorted(x for x in set(seen) if seen.count(x) > 1)
    missed = sorted(set(ids) - set(seen))
    assert not dupes, f"один и тот же контакт показан дважды: {dupes}"
    assert not missed, (
        f"контакты не показаны ни на одной странице: {missed} — владелец "
        "пролистал весь список и этих людей не увидел")
    assert len(seen) == 10, f"страницы отдали {len(seen)} строк вместо 10"


@live
def test_the_parsed_audience_walk_shows_everyone_once():
    """Та же проверка для аудитории парсера: вся пачка с одним parsed_at."""
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute(
                "DELETE FROM parsed_audiences WHERE owner_id=$1", OWNER)
            for i in range(10):
                await pool.execute(
                    "INSERT INTO parsed_audiences(owner_id, source_type,"
                    " tg_user_id, username, parsed_at)"
                    " VALUES($1,'group',$2,$3,'2026-10-01 10:00:00+00')",
                    OWNER, 520_000 + i, f"p{i}")
            # Тот же запрос, что в ручке: сортировку берём из её исходника,
            # чтобы проверка шла по продукту, а не по своей копии.
            from tests.miniapp_source import api_function
            body = api_function("parsed_audience")
            om = re.search(r"ORDER BY ([^\n]*?) LIMIT", body)
            assert om, "сортировка в parsed_audience больше не читается"
            order = om.group(1).strip()

            async def page(off):
                rows = await pool.fetch(
                    "SELECT tg_user_id FROM parsed_audiences WHERE owner_id=$1 "
                    f"ORDER BY {order} LIMIT 4 OFFSET $2", OWNER, off)
                return [r["tg_user_id"] for r in rows]

            async def touch():
                await pool.execute(
                    "UPDATE parsed_audiences SET is_active = NOT "
                    "COALESCE(is_active,false) WHERE owner_id=$1 AND tg_user_id=$2",
                    OWNER, 520_000)

            seen = await _walk(pool, page, touch)
            await pool.execute(
                "DELETE FROM parsed_audiences WHERE owner_id=$1", OWNER)
            return seen, order
        finally:
            await pool.close()

    seen, order = _run(go())
    dupes = sorted(x for x in set(seen) if seen.count(x) > 1)
    missed = sorted({520_000 + i for i in range(10)} - set(seen))
    assert not dupes and not missed, (
        f"сортировка «{order}» на страницах: повторы {dupes}, потеряны "
        f"{missed} — пачка парсера пишется одним временем, ties здесь норма")
