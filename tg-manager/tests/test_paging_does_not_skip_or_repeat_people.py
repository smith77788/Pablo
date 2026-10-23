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

import functools
import os
import pathlib
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
# Только то, что уникально САМО, без оглядки на запрос. `user_id`, `msg_id` и
# `channel_id` отсюда убраны: они уникальны лишь там, где запрос закрепляет
# остаток ключа (`bot_id=$1`, `chat_id=$2`), и этот зачёт теперь выдаётся
# механикой — по схеме и по тексту запроса, а не по имени столбца.
#
# Снятие `msg_id` сразу нашло настоящую ошибку, которую имя и прикрывало:
# поиск по архиву (`vault_service.py`, WHERE owner_id=$1 БЕЗ chat_id) листался
# по `msg_date, msg_id`, а msg_id у одного владельца повторяется в каждом
# чате. Оправдание из комментария относилось к ДРУГОМУ запросу того же файла,
# где `chat_id=$2` стоит безусловно. Список имён потому и плох: он переносит
# довод с места, где тот верен, на место, где не верен.
_UNIQUE = ("id", "uc.id", "unified_contacts.id", "r.id", "q.id", "a.id",
           "b.id", "oq.id", "p.id", "s.id", "ba.id", "mb.bot_id")

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


def order_is_total(order: str, lit: str = "") -> bool:
    """Последний ключ сортировки обязан быть уникальным.

    Уникальный ключ ПОСЛЕДНИМ, а не где-нибудь: порядок задаётся слева
    направо, и `ORDER BY id, title` уникален, а `ORDER BY title, name` — нет.

    Сортировка, собранная переменной (`ORDER BY {order}`), статически не
    судится — но `ORDER BY {order_by}, id` судится: добивка здесь литерал, и
    она последняя, что бы ни подставилось раньше.
    Один ключ — не единственная форма уникальности. Составной ключ
    (`ORDER BY bu.user_id, bu.bot_id` при `PRIMARY KEY (bot_id, user_id)`)
    задаёт порядок не хуже, а по последнему ключу выглядит нарушением: сам
    `bot_id` в выдаче повторяется сколько угодно. Такие ключи не перечисляются
    руками, а читаются из СХЕМЫ: добавят таблицу — мерка узнает сама. Зачёт
    даётся только если запрос эту таблицу и называет, иначе совпадение имён
    столбцов из чужой таблицы прикрыло бы настоящую ошибку.
    """
    if _last_key(order) in _UNIQUE:
        return True
    return _covers_a_composite_key(order, lit)


@functools.lru_cache(maxsize=1)
def _schema_unique_sets() -> dict[str, set[frozenset[str]]]:
    """Таблица -> её составные уникальные ключи, прямо из файлов схемы."""
    tbl = re.compile(r"CREATE TABLE[^(]*?(\w+)\s*\((.*?)\n\);", re.S | re.I)
    key = re.compile(r"(?:PRIMARY KEY|UNIQUE)\s*\(([^)]*)\)", re.I)
    col = re.compile(r"(\w+)\s+[\w()]+(?:\s+\w+)*?\s+(?:PRIMARY KEY|UNIQUE)\b", re.I)
    out: dict[str, set[frozenset[str]]] = {}
    for f in sorted(pathlib.Path(__file__).resolve().parents[1].glob("schema*.sql")):
        text = f.read_text(encoding="utf-8", errors="replace")
        for name, body in tbl.findall(text):
            for cols in key.findall(body):
                cs = frozenset(c.strip().strip('"').lower()
                               for c in cols.split(",") if c.strip())
                if cs:
                    out.setdefault(name.lower(), set()).add(cs)
        # Однословный `id BIGSERIAL PRIMARY KEY` объявлен в строке столбца, а
        # не отдельным PRIMARY KEY (...), поэтому ловится отдельно.
        for name, body in tbl.findall(text):
            for line in body.splitlines():
                m = col.match(line.strip())
                if m:
                    out.setdefault(name.lower(), set()).add(frozenset({m.group(1).lower()}))
    return out


def _bare_keys(order: str) -> set[str]:
    """Столбцы сортировки без псевдонима таблицы и без направления."""
    out = set()
    for k in order.split(","):
        k = re.sub(r"\s+(asc|desc)$", "", k.strip().lower())
        k = re.sub(r"\s+nulls\s+(first|last)$", "", k).strip()
        if k:
            out.add(k.split(".")[-1])
    return out


# Запросы, чья сортировка полна по причине, которую статически не доказать:
# выдача сгруппирована, или остаток ключа закреплён условием. Таких мало, и
# довод для каждого прочитан глазами.
#
# Довод привязан к ОБРЫВКУ САМОГО ЗАПРОСА, а не к имени столбца, и это главное
# в этой конструкции. Прежняя форма — имя `msg_id` в списке уникальных —
# прикрыла настоящую ошибку: довод «msg_id уникален при фиксированном chat_id»
# был верен для одного запроса файла и молча распространился на другой, где
# chat_id не фиксирован вовсе. Довод, привязанный к тексту, так переехать не
# может: изменится запрос — обрывок перестанет находиться, и проверка ниже
# потребует перечитать запрос заново, вместо того чтобы и дальше его прощать.
#
# Пробовалось и отвергнуто: выводить закрепление из текста запроса регекспом.
# `owner_id=$1` в дизъюнкции не закрепляет ничего (у списка каналов условие
# видимости тянет и чужие строки), а `mc.owner_id=$1` внутри CTE закрепляет —
# и отличить одно от другого без разбора SQL не выходит. Два подхода подряд
# дали то ложную находку, то ложный зачёт. Лучше короткий список прочитанного,
# чем мерка, которая делает вид, что понимает SQL.
_JUSTIFIED = (
    ("FROM bot_users WHERE bot_id=$1",
     "подписчики одного бота: пара (bot_id, user_id) — ключ, bot_id закреплён"),
    ("GROUP BY mc.channel_id",
     "строка выдачи одна на канал владельца: группировка по channel_id "
     "при mc.owner_id=$1"),
)


def _justification(lit: str) -> str:
    flat = " ".join(_normalise(lit).split())
    for fragment, reason in _JUSTIFIED:
        if " ".join(fragment.split()) in flat:
            return reason
    return ""


@functools.lru_cache(maxsize=1)
def _sql_constants() -> dict[str, str]:
    """Имя -> текст у строковых констант уровня модуля, по всему коду.

    Запрос часто собран из куска, который лежит отдельной константой:
    `WHERE ({_CHANNELS_VISIBLE_SQL})`, `WITH visible AS
    ({_CHANNELS_ONE_ROW_SQL})`. В исходнике f-строки на их месте стоит ИМЯ, и
    мерка, читающая только литерал, не видит ни обещания `DISTINCT ON`, ни
    дизъюнкции в условии видимости. Именно на этом она дважды и ошиблась:
    сперва не заметила, что канал в выдаче не один, потом не заметила, что
    починено.

    Константы собираются со всех файлов разом: кусок запроса живёт в одном
    модуле, а подставляется в другом.
    """
    import ast

    root = pathlib.Path(__file__).resolve().parents[1]
    out: dict[str, str] = {}
    for path in (sorted((root / "services").rglob("*.py"))
                 + sorted((root / "bot").rglob("*.py"))):
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            t = node.targets[0]
            if not isinstance(t, ast.Name):
                continue
            v = node.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.setdefault(t.id, v.value)
            elif isinstance(v, ast.JoinedStr):
                out.setdefault(t.id, ast.get_source_segment(src, v) or "")
    return out


def _expanded(lit: str) -> str:
    """Литерал с раскрытыми подстановками констант — ТОЛЬКО как контекст.

    Сортировка по-прежнему берётся из СЫРОГО литерала: у раскрытого куска
    бывает своя `ORDER BY` (у `_CHANNELS_ONE_ROW_SQL` она есть), и первой в
    тексте оказалась бы она, а судить надо внешнюю. Раскрытие нужно лишь
    затем, чтобы увидеть обещания и условия, которые до него не видны.
    """
    out = lit
    for _ in range(3):          # вложенность бывает: константа в константе
        before = out
        for name, text in _sql_constants().items():
            token = "{" + name + "}"
            if token in out:
                out = out.replace(token, text)
        if out == before:
            break
    return out


def _covers_a_composite_key(order: str, lit: str) -> bool:
    """Уникальный ключ таблицы накрыт сортировкой плюс закреплёнными столбцами.

    Позиция ключей тут не важна, в отличие от правила по последнему ключу:
    если ключ накрыт, строка им определена однозначно, и что стоит в
    сортировке дальше, уже ни на что не влияет. Правило по последнему ключу
    оставлено строгим нарочно — оно судит сортировки, про которые схема ничего
    не знает, и там лучше перебдеть.

    Зачёт даётся только таблице, которую запрос НАЗЫВАЕТ: иначе совпадение
    имён столбцов с чужой таблицей прикрыло бы настоящую ошибку.
    """
    if not lit:
        return False
    have = _bare_keys(order)
    low = _expanded(lit).lower()
    # `SELECT DISTINCT ON (channel_id)` — обещание самого запроса: строка на
    # каждое значение ровно одна. Если сортировка этот столбец несёт, порядок
    # полон, и доказывать это схемой не нужно. В отличие от простого
    # `SELECT DISTINCT`, который различает строки по ВСЕМУ списку столбцов и
    # потому ничего об одном из них не обещает.
    for m in re.finditer(r"distinct\s+on\s*\(([^)]*)\)", low):
        cols = {c.strip().split(".")[-1] for c in m.group(1).split(",") if c.strip()}
        if cols and cols <= have:
            return True
    for table, ksets in _schema_unique_sets().items():
        if table not in low:
            continue
        if any(ks <= have for ks in ksets):
            return True
    return False


def order_is_dynamic(order: str) -> bool:
    """Последний ключ подставляется из переменной — читать надо глазами."""
    return "{" in _last_key(order)


@functools.lru_cache(maxsize=1)
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
    # `user_id` сам по себе добивкой НЕ считается: у одного пользователя
    # строка в каждом боте. Мерка этого и не прощает — прощает отдельный
    # прочитанный довод, привязанный к тексту запроса.
    assert not order_is_total("last_seen DESC NULLS LAST, user_id")
    assert _justification(
        "SELECT user_id FROM bot_users WHERE bot_id=$1 "
        "ORDER BY last_seen DESC NULLS LAST, user_id")
    # `DISTINCT ON` — обещание запроса, его мерка читает сама.
    assert order_is_total(
        "member_count DESC NULLS LAST, channel_id",
        "SELECT DISTINCT ON (channel_id) channel_id FROM managed_channels "
        "ORDER BY member_count DESC NULLS LAST, channel_id")
    assert not order_is_total(
        "member_count DESC NULLS LAST, channel_id",
        "SELECT DISTINCT channel_id, title FROM managed_channels "
        "ORDER BY member_count DESC NULLS LAST, channel_id")


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
    for where, line, order, lit in _paging_queries():
        if not order:
            bad.append(f"{where}:{line}: сдвиг без сортировки вообще")
        elif not order_is_total(order, lit) and not _justification(lit):
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

OUTSIDE_CEILING = 0

# Сортировка, собранная переменной: статически не судится, проверена чтением.
#   * contacts_hub/repository.py — order собирается из карты сортировок, и к
#     любой из них дописано `, id` (см. get_contacts);
#   * vault_service.py — order это `msg_date …, msg_id …`, а msg_id уникален
#     в пределах чата, которым запрос скоупится всегда (`chat_id=$2` стоит в
#     условии безусловно, не по фильтру).
DYNAMIC_CEILING = 2


@functools.lru_cache(maxsize=1)
def _all_paging_queries() -> list[tuple[str, int, str, str]]:
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
                        " ".join(m.group(1).split()) if m else "", lit))
    return out


def test_the_rest_of_the_code_only_gets_better():
    """Храповик: запросов без добивки вне слоя мини-аппа только меньше."""
    rows = _all_paging_queries()
    assert len(rows) >= 20, f"запросов со сдвигом вне слоя мало: {len(rows)}"
    bad = [f"{w}:{ln}  ORDER BY {o}" for w, ln, o, lit in rows
           if not order_is_total(o, lit) and not order_is_dynamic(o)
           and not _justification(lit)]
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
    dyn = [f"{w}:{ln}" for w, ln, o, _l in rows if order_is_dynamic(o)]
    assert len(dyn) <= DYNAMIC_CEILING, (
        f"запросов с сортировкой из переменной стало {len(dyn)} вместо "
        f"{DYNAMIC_CEILING}: мерка их не судит, значит каждый надо прочитать "
        f"глазами и записать причину здесь же.\n  " + "\n  ".join(dyn))
    assert dyn, "ни одного — мерка перестала их различать"


# ── Листание, разложенное по двум местам ─────────────────────────────────────
#
# Мерки выше требуют, чтобы сдвиг и сортировка стояли в ОДНОМ литерале. Это
# и было их слепым пятном, причём ровно наоборот к ожиданиям: аккуратно
# написанное листание так НЕ пишут. В services/op_worker.py оно разложено —
# общий читатель дописывает ` LIMIT {n} OFFSET {k}` к переданному запросу, а
# SELECT с сортировкой стоит на месте вызова. Ни один из двух литералов мерки
# выше не задевает: в первом нет SELECT, во втором нет OFFSET. То есть мерка
# была строже всего к самому неряшливому коду и слепа к самому аккуратному, а
# общий читатель — это ПРАВИЛЬНАЯ форма, которая переживёт остальные.
#
# Нашлось это не глазами: я собирался сказать соседней сессии, что их слой уже
# под охраной, и перед тем как сказать — проверил. Охвата не было.


def _pagers(tree, src) -> set[str]:
    """Функции, которые сами дописывают сдвиг к ЧУЖОМУ запросу.

    Ищется свойство, а не имя: в теле есть литерал со сдвигом и БЕЗ SELECT,
    то есть сдвиг приклеивается к запросу, пришедшему снаружи. Список
    читателей руками не держится, поэтому следующий такой же найдётся сам.
    """
    import ast

    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sn in _string_nodes(node):
            lit = (sn.value if isinstance(sn, ast.Constant)
                   else ast.get_source_segment(src, sn) or "")
            if (isinstance(lit, str) and _OFFSET.search(lit)
                    and "SELECT" not in lit.upper()):
                out.add(node.name)
                break
    return out


def _split_paged_in(src: str) -> list[tuple[int, str]]:
    """Строка и сортировка каждого запроса, чей сдвиг дописывает читатель."""
    import ast

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    names = _pagers(tree, src)
    if not names:
        return []
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        f = node.func
        nm = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        if nm not in names:
            continue
        arg = node.args[0]
        if not isinstance(arg, (ast.Constant, ast.JoinedStr)):
            continue
        lit = (arg.value if isinstance(arg, ast.Constant)
               else ast.get_source_segment(src, arg) or "")
        if not isinstance(lit, str) or "SELECT" not in lit.upper():
            continue
        m = _ORDER.search(_normalise(lit))
        order = (" ".join(m.group(1).split()).strip().strip('"\'') if m else "")
        out.append((arg.lineno, order, lit))
    return [(ln, o) for ln, o, _l in out]


@functools.lru_cache(maxsize=1)
def _split_paging_queries() -> list[tuple[str, int, str, str]]:
    """По всему коду: файл, строка, сортировка, литерал."""
    import ast

    root = pathlib.Path(__file__).resolve().parents[1]
    out = []
    for path in (sorted((root / "services").rglob("*.py"))
                 + sorted((root / "bot").rglob("*.py"))):
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        names = _pagers(tree, src)
        if not names:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            f = node.func
            nm = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if nm not in names:
                continue
            arg = node.args[0]
            if not isinstance(arg, (ast.Constant, ast.JoinedStr)):
                continue
            lit = (arg.value if isinstance(arg, ast.Constant)
                   else ast.get_source_segment(src, arg) or "")
            if not isinstance(lit, str) or "SELECT" not in lit.upper():
                continue
            m = _ORDER.search(_normalise(lit))
            order = (" ".join(m.group(1).split()).strip().strip("\"'")
                     if m else "")
            out.append((str(path.relative_to(root)), arg.lineno, order, lit))
    return tuple(out)


_PAGER_SRC = (
    "async def _page_source(sql, *args):\n"
    "    offset = 0\n"
    "    while True:\n"
    "        rows = await fetch(pool, sql + f' LIMIT {_PAGE} OFFSET {offset}', *args)\n"
    "        if not rows:\n"
    "            return\n"
    "        offset += len(rows)\n")


def test_the_detector_finds_a_shared_pager_by_what_it_does():
    """Читатель узнаётся по делу, а не по имени: иначе мерка слепа к новому."""
    import ast

    src = _PAGER_SRC
    assert _pagers(ast.parse(src), src), (
        "функция, приклеивающая сдвиг к чужому запросу, не распознана")
    # А обычный запрос со сдвигом В СЕБЕ читателем не считается: иначе
    # читателями оказались бы все, и судились бы их случайные аргументы.
    plain = ("async def h(pool):\n"
             "    return await pool.fetch('SELECT 1 FROM t ORDER BY id "
             "LIMIT $1 OFFSET $2')\n")
    assert not _pagers(ast.parse(plain), plain), (
        "запрос со сдвигом внутри себя принят за общего читателя")


def test_the_detector_judges_the_query_at_the_call_site():
    """Проба на больном и на здоровом: правило ниже не может быть любым."""
    bad = _PAGER_SRC + (
        "async def go():\n"
        "    await _page_source('SELECT a FROM parsed_audiences "
        "WHERE owner_id=$1 ORDER BY parsed_at DESC', 1)\n")
    rows = [o for _ln, o in _split_paged_in(bad)]
    assert rows and not order_is_total(rows[0]), (
        f"сортировка с места вызова не разобрана или сочтена полной: {rows}")

    good = bad.replace("ORDER BY parsed_at DESC", "ORDER BY parsed_at DESC, id")
    rows = [o for _ln, o in _split_paged_in(good)]
    assert rows and order_is_total(rows[0]), (
        f"добитая сортировка с места вызова сочтена неполной: {rows}")


def test_the_composite_key_counts_only_for_its_own_table():
    """Составной ключ из схемы — зачёт, но только своей таблице."""
    lit = ("SELECT DISTINCT bu.user_id, bu.bot_id FROM bot_users bu "
           "ORDER BY bu.user_id, bu.bot_id")
    assert order_is_total("bu.user_id, bu.bot_id", lit), (
        "PRIMARY KEY (bot_id, user_id) у bot_users не зачтён: по последнему "
        "ключу такая сортировка выглядит нарушением, хотя порядок задаёт")
    # Без литерала зачёта нет: таблицу назвать нечем.
    assert not order_is_total("bu.user_id, bu.bot_id")
    # И чужой таблице — тоже нет, даже при тех же именах столбцов.
    assert not order_is_total(
        "user_id, bot_id", "SELECT x FROM some_log ORDER BY user_id, bot_id")
    # Пара, не накрывающая объявленный ключ, зачёта не получает: у bot_users
    # ключ (bot_id, user_id), а здесь bot_id ни в сортировке, ни закреплён.
    assert not order_is_total(
        "created_at, user_id",
        "SELECT x FROM bot_users ORDER BY created_at, user_id")


def test_the_split_sweep_sees_the_real_code():
    """Пустая выдача сделала бы правило ниже зелёным ни на чём."""
    rows = _split_paging_queries()
    assert len(rows) >= 3, f"запросов через общего читателя найдено мало: {len(rows)}"
    assert all(o for _w, _ln, o, _l in rows), (
        "запрос через читателя вообще без сортировки: "
        + str([(r[0], r[1]) for r in rows if not r[2]]))


def test_a_shared_pager_does_not_excuse_a_partial_order():
    """Листание через общего читателя стоит на том же правиле."""
    bad = [f"{w}:{ln}  ORDER BY {o}" for w, ln, o, lit in _split_paging_queries()
           if not order_is_total(o, lit) and not order_is_dynamic(o)
           and not _justification(lit)]
    assert not bad, (
        "сдвиг дописывает общий читатель, а сортировка на месте вызова не "
        "полна — страницы будут повторять и терять записи:\n  "
        + "\n  ".join(bad))


def test_every_justification_still_matches_a_real_query():
    """Довод, переживший свой запрос, прощает неизвестно что.

    Это и есть смысл привязки к тексту: обрывок запроса перестаёт находиться,
    как только запрос переписали, и проверка требует перечитать его заново.
    Имя столбца так не умеет — оно продолжает прощать молча, что и случилось с
    `msg_id`.
    """
    lits = ([lit for _w, _ln, _o, lit in _paging_queries()]
            + [lit for _w, _ln, _o, lit in _all_paging_queries()]
            + [lit for _w, _ln, _o, lit in _split_paging_queries()])
    stale = []
    for fragment, reason in _JUSTIFIED:
        flat = " ".join(fragment.split())
        if not any(flat in " ".join(_normalise(x).split()) for x in lits):
            stale.append(f"{fragment!r} ({reason})")
    assert not stale, (
        "довод больше не относится ни к одному запросу со сдвигом — запрос "
        "переписали, и довод надо перечитать, а не оставлять:\n  "
        + "\n  ".join(stale))
    assert len(_JUSTIFIED) <= 4, (
        f"доводов стало {len(_JUSTIFIED)}: столько прочитанного глазами уже "
        "не читают, и список превращается в список исключений")


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
