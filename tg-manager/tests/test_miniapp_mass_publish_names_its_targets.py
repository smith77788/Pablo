"""Регрессия: массовая публикация не называла число каналов.

Что было сломано.

1. Экран «Массовая публикация» обещал «опубликовать во ВСЕ каналы», кнопка
   говорила «📡 Опубликовать во все каналы», подтверждение спрашивало
   «Опубликовать во ВСЕ ваши каналы?» — и ни одна из трёх надписей ни разу не
   называла число. Владелец подтверждал необратимое действие, не зная, три у
   него канала или триста.

2. Отложенная публикация уходила ВООБЩЕ без подтверждения: ветка
   `else if (!scheduled_for)` пропускала диалог, стоило заполнить дату.

3. «Тест на N каналов» при N ≥ числа каналов был полной публикацией под видом
   канарейки — и тост рапортовал «🐤 Тест-публикация».

4. Канарейка брала адресатов из `/api/miniapp/channels` — списка ШИРЕ того, в
   который уходит публикация: он добавляет каналы экосистем и рабочих
   пространств, а `mass_publish` считает только `managed_channels` по
   `owner_id`. Тест проверял не тот набор.

5. Список операций публикации просил общую страницу очереди (30 строк) и
   отбирал `mass_publish` у себя: после тридцати инвайтов экран писал «Нет
   операций публикации», хотя публикации были. А на ошибке загрузки список
   молча обнулялся — пустая область читается как «публикаций нет».
"""
from __future__ import annotations

import inspect
import re

import pytest

from services import mini_app_api
from tests.miniapp_source import miniapp_html, source_of


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _fn(name: str) -> str:
    """Тело функции мини-аппа по балансу фигурных скобок."""
    src = source_of(name)
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", src, re.M)
    assert m, f"функция {name} не найдена"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    pytest.fail(f"не закрылось тело {name}")


# ── Бэкенд: есть чем назвать адресатов ───────────────────────────────────────

def test_targets_endpoint_exists_and_is_registered():
    src = _api_src()
    assert "async def mass_publish_targets(" in src, (
        "нет эндпойнта, отдающего число каналов-адресатов — экрану нечем "
        "назвать масштаб необратимой публикации")
    assert 'add_get("/api/miniapp/mass_publish/targets"' in src, (
        "эндпойнт не зарегистрирован — фронт получит 404")


def test_targets_counts_the_same_set_mass_publish_publishes_to():
    """Иначе подпись под кнопкой обещает один набор, а пост уходит в другой."""
    src = _api_src()
    counting = "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1"
    targets = src[src.index("async def mass_publish_targets("):]
    targets = targets[:targets.index("async def mass_publish(")]
    assert counting in targets, (
        "счёт адресатов должен идти тем же запросом, что и в mass_publish: "
        "список /channels шире (экосистемы, рабочие пространства)")


def test_operations_can_be_filtered_by_type():
    src = _api_src()
    assert 'op_type_filter' in src and 'request.query.get("op_type")' in src, (
        "очередь операций не умеет фильтровать по типу — экран публикаций "
        "вынужден отбирать их из общей страницы и врать «нет операций»")
    assert "oq.op_type=$" in src, "фильтр по типу не доходит до запроса"


def test_unknown_op_type_is_rejected_not_swallowed():
    """Молча проигнорированный фильтр читается как сломанный фильтр."""
    src = _api_src()
    block = src[src.index('request.query.get("op_type")'):]
    block = block[:block.index("counts: dict")]
    assert "Неизвестный тип операции" in block, (
        "мусорный op_type должен получить честный отказ, а не полный список")


# ── Экран: масштаб назван до нажатия ─────────────────────────────────────────

def test_screen_shows_how_many_channels_will_get_the_post():
    html = miniapp_html()
    assert 'id="mpTargets"' in html, "на экране негде показать число адресатов"
    body = _fn("loadMpTargets")
    assert "mass_publish/targets" in body, "число адресатов берётся не оттуда, куда уйдёт пост"
    assert "MP_TOTAL" in body


def test_button_label_names_the_count():
    body = _fn("renderMpBtn")
    assert "MP_TOTAL" in body and "plural(" in body, (
        "подпись кнопки обязана называть число каналов, а не «во все каналы»")
    assert "MP_TOTAL === 0" in body, (
        "ноль каналов — это знание, а не незнание: кнопка не должна обещать "
        "публикацию, которой не во что уйти")


def _mentions(body: str, expr: str, needle: str, depth: int = 2) -> bool:
    """Есть ли `needle` в выражении — считая значения локальных переменных.

    Текст подтверждения собирается из кусков (`'Опубликовать '+whereTo+'?'`),
    поэтому «искать MP_TOTAL прямо в аргументе askConfirm» — сломанный пробник:
    он не увидит того, что стоит в соседней строке. Разворачиваем локальные
    присваивания на пару уровней вглубь, а не режем фиксированное окно вокруг
    вызова.
    """
    if needle in expr:
        return True
    if depth <= 0:
        return False
    for name in set(re.findall(r"[A-Za-z_$][\w$]*", expr)):
        for m in re.finditer(
                r"(?:const|let|var)\s+" + re.escape(name) + r"\s*=\s*(.*?);",
                body, re.DOTALL):
            if _mentions(body, m.group(1), needle, depth - 1):
                return True
    return False


def test_full_publish_confirmation_names_the_count():
    body = _fn("sendMassPublish")
    confirms = re.findall(r"askConfirm\((.*?)\)\)\)", body, re.DOTALL)
    assert confirms, "подтверждения полной публикации нет вовсе"
    full = [c for c in confirms if "Опубликовать" in c]
    assert full, "нет подтверждения полной публикации"
    assert any(_mentions(body, c, "MP_TOTAL") for c in full), (
        "подтверждение необратимой публикации не называет число каналов")


def test_scheduled_publish_is_also_confirmed():
    body = _fn("sendMassPublish")
    assert "else if (!scheduled_for)" not in body, (
        "отложенная публикация во все каналы уходила без подтверждения: "
        "достаточно было заполнить дату")
    assert "scheduled_for" in body and "Запуск: " in body, (
        "подтверждение отложенной публикации должно называть время запуска")


def test_canary_that_covers_everything_is_refused():
    body = _fn("sendMassPublish")
    assert "canary >= MP_TOTAL" in body, (
        "«тест на N каналов» при N ≥ всего каналов — это полная публикация под "
        "видом канарейки, и тост рапортовал о ней «🐤 Тест-публикация»")


def test_canary_picks_from_the_publish_set_not_from_channels_list():
    body = _fn("sendMassPublish")
    assert "mass_publish/targets" in body, (
        "канарейка обязана брать каналы из того же набора, в который уйдёт "
        "полная публикация")
    assert "'/api/miniapp/channels'" not in body, (
        "/channels шире набора публикации — тест проверял бы не те каналы")


# ── Список операций публикации ───────────────────────────────────────────────

def test_publish_ops_list_asks_the_server_for_its_own_type():
    body = _fn("loadMpOps")
    assert "op_type=mass_publish" in body, (
        "экран отбирал публикации из общей страницы очереди и писал «нет "
        "операций публикации», когда их вытеснили другие операции")


def test_publish_ops_list_does_not_blank_itself_on_error():
    body = _fn("loadMpOps")
    m = re.search(r"catch\s*\([^)]*\)\s*\{(.*)\}\s*$", body, re.DOTALL)
    assert m, "у загрузчика нет catch"
    tail = m.group(1)
    assert "errHtml" in tail, (
        "на ошибке список молча обнулялся — пустая область читается как "
        "«публикаций нет»")
