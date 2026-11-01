"""Выгрузка операций обязана идти из БД, а не из разметки экрана.

ЧТО ЛОМАЛОСЬ. На экране «Операции» есть кнопка 📥 «Экспорт CSV». Она собирала
файл из того, что дорисовано в DOM:

    document.querySelectorAll('#opsList .li').forEach(el => {
      rows.push(['', name, badge, '', sub, '']);   // ID, Создана, Прогресс — пусто
    });

Три беды, и ни одна не выглядит как ошибка:
1. В файл попадала одна отрисованная страница списка, а не история операций.
2. Три колонки из шести писались пустыми ВСЕГДА, а в «Канал» уезжал
   подзаголовок строки.
3. Ни всего/сделано, ни числа ошибок, ни времени завершения в разметке нет
   вовсе — хотя бэкенд их отдаёт.

Владелец получал файл с правильной шапкой и почти пустым телом. Это хуже
отказа: по такому отчёту принимают решения, и он не выглядит сломанным.

ЧТО ТЕПЕРЬ. Кнопка зовёт `/api/miniapp/operations/export` — ручка была
написана, скоупнута по владельцу и отдавала русскую шапку, но её не звал ни
один экран (её нашёл потолок `test_routes_the_owner_can_reach`).
"""
from __future__ import annotations

import re
import sys

sys.path.insert(0, "tests")
from miniapp_source import (  # noqa: E402
    api_function, api_routes, api_source, miniapp_source,
)

UI = miniapp_source()


def _fn(name: str) -> str:
    """Тело JS-функции по балансу скобок — срез фиксированной длины промахнётся."""
    i = UI.index(f"function {name}(")
    depth, j, started = 0, i, False
    while j < len(UI):
        if UI[j] == "{":
            depth += 1
            started = True
        elif UI[j] == "}":
            depth -= 1
            if started and depth == 0:
                return UI[i:j + 1]
        j += 1
    raise AssertionError(f"не нашёл тело {name}")


def _code_only(body: str) -> str:
    """Без строчных комментариев: в них объяснено, КАК было и ЧТО зовём, и
    обе проверки цеплялись за собственное объяснение. Проверено обратным
    контролем: с комментариями положительная проверка прошла на заведомо
    сломанной кнопке, потому что путь ручки упомянут в комментарии.
    """
    return "\n".join(l for l in body.split("\n")
                     if not l.lstrip().startswith("//"))


def test_the_button_calls_the_endpoint():
    body = _code_only(_fn("exportOpsCSV"))
    assert "/api/miniapp/operations/export" in body, (
        "кнопка выгрузки операций снова собирает файл сама — в нём будет одна "
        "страница списка и пустые колонки")


def test_the_button_does_not_scrape_the_screen():
    body = _code_only(_fn("exportOpsCSV"))
    for bad in ("querySelectorAll", "#opsList", "new Blob", "URL.createObjectURL"):
        assert bad not in body, (
            f"выгрузка операций снова читает разметку ({bad}): столбцы, которых "
            f"на экране нет, в файл не попадут")


def test_the_endpoint_the_button_calls_exists_on_get():
    assert "/api/miniapp/operations/export" in api_routes(), (
        "маршрута выгрузки операций нет — кнопка получит 404")
    # Выгрузка открывается ссылкой во внешнем браузере, то есть GET: на другом
    # методе кнопка получила бы 405 при живом с виду маршруте.
    assert re.search(
        r'add_get\(\s*"/api/miniapp/operations/export"', api_source()), (
        "маршрут выгрузки операций больше не на GET — ссылка в браузере "
        "ответит 405")


def test_the_endpoint_gives_what_the_screen_cannot():
    """Смысл перехода: поля, которых в разметке нет вовсе."""
    src = api_function("operation_export")
    for col in ("total_items", "done_items", "retry_count", "finished_at"):
        assert col in src, f"выгрузка перестала отдавать {col}"
    assert "owner_id=$1" in src, "выгрузка операций больше не скоупнута по владельцу"
    assert "Ошибок" in src and "Завершено" in src, (
        "шапка CSV перестала быть русской — владелец английского не читает")


def test_the_shared_download_helper_carries_a_ticket():
    """Ссылку открывает внешний браузер: заголовка Authorization там нет, и без
    пропуска в адресе придёт 401. Пропуск, а не токен сессии — тот оседал бы в
    истории браузера (tests/test_session_token_never_in_url.py)."""
    body = _fn("exportCSV")
    assert "openTicketLink(" in body, (
        "общий помощник выгрузки перестал подставлять пропуск — ручка ответит 401")
    assert "token=" not in body


def test_the_probe_reads_the_real_ui():
    """Пустой исходник мини-аппа сделал бы отрицательные проверки зелёными."""
    assert len(UI) > 500_000, "исходник мини-аппа подозрительно мал"
    assert re.search(r"onclick=\"exportOpsCSV\(\)\"", UI), (
        "кнопки выгрузки операций больше нет на экране — проверять нечего")
