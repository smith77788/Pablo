"""Поиск по операциям: у владельца их сотни, а искались они перелистыванием.

«Диспетчер задач» умел фильтровать по статусу и по типу — а типов у продукта
больше тридцати, и «тот самый инвайт во вторник» приходилось листать. Поиска не
было ни на экране, ни в эндпоинте.

Два условия, без которых поиск был бы обманом:

  * ищет СЕРВЕР. Фильтровать загруженную страницу бессмысленно: на экране
    тридцать операций из двух сотен, и «ничего не найдено» означало бы всего
    лишь «нет среди этих тридцати»;
  * счётчики считаются под ТЕМ ЖЕ срезом, что и список. Иначе плитки
    «Всего / Готово / Ошибки» отвечают на другой вопрос, чем список под ними:
    ищешь «инвайт», видишь три строки и «Всего 214». Этот же разрыв уже нашёлся
    в счётчике каналов — см. test_miniapp_channels_count_matches_list.

Проверено в браузере (фикстура на 60 операций): без поиска 30 строк и «показано
30 из 60»; «Публикация» — 20 строк и кнопка догрузки пропадает; поиск по типу
операции работает; несовпадение даёт «Ничего не найдено» с названной причиной и
кнопкой сброса; сброс возвращает полный список и очищает поле.
"""
from __future__ import annotations

import ast
import pathlib
import re

from tests.miniapp_source import miniapp_html

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")


def _handler(name: str) -> str:
    for node in ast.walk(ast.parse(API)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"обработчик {name} не найден")


def test_endpoint_accepts_a_search_term():
    src = _handler("operations")
    assert "request.query.get('q'" in src or 'request.query.get("q"' in src, (
        "эндпоинт операций не принимает поиск"
    )
    assert "label ILIKE" in src, "поиск не смотрит в подпись операции"
    assert "op_type ILIKE" in src, "поиск не смотрит в тип операции"


def test_search_term_is_bounded():
    """Строка из запроса уходит в SQL как параметр и не бесконечна."""
    src = _handler("operations")
    m = re.search(r"_q = .*?\[:(\d+)\]", src)
    assert m, "длина поискового запроса не ограничена"
    assert int(m.group(1)) <= 128
    assert "f'%{_q}%'" in src or 'f"%{_q}%"' in src, (
        "шаблон поиска должен уходить параметром, а не склейкой в текст запроса"
    )


def test_counts_use_the_same_slice_as_the_list():
    """Плитки и список обязаны отвечать на один вопрос.

    Утверждение нарочно узкое: раньше здесь стояло «ILIKE в счётчиках ИЛИ
    _cwhere дополняется где-то в обработчике», и оно проходило на одной только
    ветке фильтра по типу — то есть на удалённом поиске тоже было зелёным."""
    src = _handler("operations")
    # всё, что собирает условие счётчиков, — от его объявления до запроса
    start = src.index("_cwhere = ")
    end = src.index("GROUP BY status")
    assert start < end, "условие счётчиков объявлено после самого запроса"
    counts = src[start:end]
    assert "label ILIKE" in counts and "op_type ILIKE" in counts, (
        "поиск не попал в счётчики: плитки покажут число не из этого списка"
    )
    assert "_cargs.append(f'%{_q}%')" in counts or '_cargs.append(f"%{_q}%")' in counts, (
        "счётчики не получают поисковое слово параметром"
    )


def test_status_stays_the_second_parameter():
    """Гейт фильтра по статусу смотрит на `oq.status=$2` в последнем запросе."""
    src = _handler("operations")
    assert "oq.status=${len(_wargs)}" in src or "oq.status=$" in src
    i = src.index("_where = 'oq.owner_id=$1'") if "_where = 'oq.owner_id=$1'" in src \
        else src.index('_where = "oq.owner_id=$1"')
    tail = src[i:]
    assert tail.index("status_filter") < tail.index("op_type_filter"), (
        "статус обязан добавляться в условие ПЕРВЫМ, иначе он перестанет быть $2"
    )
    assert tail.index("op_type_filter") < tail.index("_q"), (
        "поиск добавляется последним, чтобы не сдвинуть номера параметров"
    )


def test_screen_has_a_search_box_wired_to_the_server():
    ui = miniapp_html()
    assert 'id="opsSearch"' in ui, "на экране операций нет поля поиска"
    assert 'oninput="onOpsSearch(' in ui, "поле поиска ни к чему не подключено"
    assert "p.set('q', OPS_SEARCH)" in ui, (
        "запрос уходит без поискового слова — поиск искал бы в загруженной странице"
    )


def test_new_search_starts_a_new_page():
    """Иначе «Загрузить ещё» от прошлого запроса домешивал бы чужие строки."""
    ui = miniapp_html()
    m = re.search(r"function onOpsSearch\(v\)\s*\{(.*?)\n\}", ui, re.DOTALL)
    assert m, "onOpsSearch не найден"
    body = m.group(1)
    for reset in ("OPS_ROWS = []", "OPS_OFFSET = 0", "OPS_TOTAL = 0"):
        assert reset in body, f"поиск не сбрасывает страницу ({reset})"


def test_empty_result_names_the_reason_and_offers_a_way_out():
    """«Нет операций» при активном поиске читается как «у вас их нет»."""
    ui = miniapp_html()
    assert "Ничего не найдено" in ui
    assert "по запросу «" in ui, "пустой результат не называет запрос"
    assert "resetOpsFilters()" in ui, "из пустого результата нет выхода одним тапом"
    m = re.search(r"function resetOpsFilters\(\)\s*\{(.*?)\n\}", ui, re.DOTALL)
    assert m, "resetOpsFilters не найден"
    assert "OPS_SEARCH = ''" in m.group(1) and "OPS_FILTER = null" in m.group(1), (
        "сброс обязан снимать и поиск, и фильтр — иначе кнопка врёт"
    )
