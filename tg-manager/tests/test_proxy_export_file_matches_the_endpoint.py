"""Файл прокси, который скачивает владелец, — это то, что отдаёт ручка.

Кнопка «Экспорт» на экране прокси делает так: зовёт
`/api/miniapp/proxy/export?format=json`, а CSV собирает САМА — свой заголовок
из десяти колонок и своё отображение полей. Та же ручка умеет отдавать готовый
CSV (`format=csv`) с таким же заголовком. То есть заголовок и набор полей
описаны в проекте ДВАЖДЫ, в разметке и в ручке.

Сегодня они совпадают, и поэтому это не поломка, а мина. Добавят колонку в
ручку — скачанный файл её не получит; переименуют поле в JSON — колонка в
файле станет пустой, причём файл всё равно скачается, с правильным
заголовком и нужным числом строк. Ровно так вела себя выгрузка операций: три
колонки из шести были пустыми ВСЕГДА, и заметить это можно было только
сверив файл с базой руками.

Поэтому набор сверяет три вещи: заголовки совпадают по составу и порядку;
каждое поле, которое читает разметка, ручка в JSON действительно отдаёт; и
число колонок в обоих местах одно.

Почему не переписать кнопку на общий `exportCSV` (как сделано для операций):
там выгрузка была сломана, и любой путь через ручку был лучше. Здесь ничего
не сломано, а `exportCSV` открывает ссылку через `tg.openLink` вместо
скачивания Blob'ом внутри приложения — менять работающий путь на другой
механизм без возможности проверить его в клиенте Telegram дороже, чем
закрыть расхождение храповиком.
"""
from __future__ import annotations

import ast
import re

from tests.miniapp_source import api_function, miniapp_html

HTML = miniapp_html()


def _js_fn(name: str) -> str:
    """Тело JS-функции по балансу фигурных скобок."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена в разметке"
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


def _ui_header() -> list[str]:
    body = _js_fn("exportProxies")
    m = re.search(r"const header = \[(.*?)\];", body, re.S)
    assert m, "в exportProxies больше нет своего заголовка — сверять нечего"
    return re.findall(r"'([^']*)'", m.group(1))


def _api_header() -> list[str]:
    src = api_function("proxy_export")
    m = re.search(r"header = \[(.*?)\]", src, re.S)
    assert m, "в proxy_export больше нет заголовка CSV"
    return re.findall(r'"([^"]*)"', m.group(1))


def _api_item_keys() -> set[str]:
    """Ключи словаря, который ручка кладёт в items (ast, не регулярка)."""
    src = api_function("proxy_export")
    keys: set[str] = set()
    for node in ast.walk(ast.parse(src.lstrip())):
        if isinstance(node, ast.Dict):
            got = {k.value for k in node.keys
                   if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if {"id", "label", "url"} <= got:
                keys |= got
    assert keys, "не нашёл словарь элемента в proxy_export"
    return keys


def _ui_fields() -> set[str]:
    """Поля, которые разметка читает у элемента: p.<поле>."""
    body = _js_fn("exportProxies")
    m = re.search(r"list\.map\(p\s*=>\s*\[(.*?)\]\s*\)", body, re.S)
    assert m, "в exportProxies больше нет отображения полей элемента"
    return set(re.findall(r"\bp\.(\w+)", m.group(1)))


def test_the_probe_reads_both_sides_at_all():
    """Детектор, который ничего не нашёл, зелёный всегда."""
    ui, api = _ui_header(), _api_header()
    assert len(ui) >= 8, f"заголовок разметки прочитан не весь: {ui}"
    assert len(api) >= 8, f"заголовок ручки прочитан не весь: {api}"
    assert len(_api_item_keys()) >= 8
    assert len(_ui_fields()) >= 8


def test_headers_match_exactly():
    ui, api = _ui_header(), _api_header()
    assert ui == api, (
        "заголовок скачиваемого файла разошёлся с заголовком ручки — "
        "владелец получит не тот файл, который отдаёт сервер:\n"
        f"  разметка: {ui}\n  ручка:    {api}")


def test_every_field_the_screen_reads_is_actually_returned():
    missing = sorted(_ui_fields() - _api_item_keys())
    assert not missing, (
        "разметка читает у элемента поля, которых ручка не отдаёт — эти "
        f"колонки в файле будут ПУСТЫМИ всегда: {missing}")


def test_column_count_is_the_same_on_both_sides():
    assert len(_ui_header()) == len(_ui_fields()), (
        "колонок в заголовке разметки и полей в строке разное число — "
        "колонки съедут относительно заголовка")
