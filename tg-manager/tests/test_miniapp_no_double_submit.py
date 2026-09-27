"""Регресс: двойной тап по кнопке не отправляет два одинаковых запроса.

Из 218 функций, висящих на кнопках мини-аппа и делающих POST, 81 не гасила свою
кнопку на время запроса. Двойной тап по «Создать» заводил вторую экосистему,
вторую связку, второй импорт прокси. Гасить это по кнопкам — 81 правка, и каждое
новое место снова забыли бы, поэтому защита стоит в одном месте: в `api()`.

Правило: пока ИЗМЕНЯЮЩИЙ запрос в полёте, второй точно такой же не уходит —
он получает результат первого. Ключ — метод, путь и тело целиком, поэтому два
разных действия друг другу не мешают, а окно совпадает со временем ответа
сервера: осознанный повтор через секунду проходит как обычно.

Проверено в браузере (Chromium, ответ сервера задержан на 400 мс):
  * два одинаковых POST подряд → один запрос, оба вызова получают один ответ;
  * разные тела → уходят оба;
  * повтор после ответа → уходит снова;
  * два GET → уходят оба;
  * после ошибки карта пустеет, следующий запрос уходит.

Здесь закреплены условия, без которых любое из этих свойств отваливается молча.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html


def _body(name: str) -> str:
    """Тело функции по балансу скобок.

    Начало тела ищем ПОСЛЕ закрывающей скобки сигнатуры, а не первым `{` подряд:
    у `api(path, opts={})` первый `{` — это значение по умолчанию, и наивный
    поиск возвращал пустое `{}`. Проверка при этом оставалась зелёной на всём,
    что искала через `not in`."""
    src = miniapp_html()
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"функция {name} не найдена"
    k, par = m.end() - 1, 0
    while True:
        if src[k] == "(":
            par += 1
        elif src[k] == ")":
            par -= 1
            if par == 0:
                break
        k += 1
    i = src.index("{", k)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"не закрылось тело {name}")


def test_api_dedups_in_flight_mutations():
    body = _body("api")
    assert "_API_INFLIGHT" in body, "в api() нет карты запросов в полёте"
    assert re.search(r"_API_INFLIGHT\.get\(", body), "api() не смотрит, летит ли такой же запрос"
    assert re.search(r"_API_INFLIGHT\.set\(", body), "api() не запоминает запрос в полёте"


def test_key_includes_method_path_and_body():
    """Ключ без тела склеил бы два РАЗНЫХ действия по одному адресу."""
    body = _body("api")
    m = re.search(r"const\s+_key\s*=\s*([^;]+);", body)
    assert m, "ключ дедупа не найден"
    key = m.group(1)
    assert "_m" in key, "в ключе нет метода"
    assert "path" in key, "в ключе нет пути"
    assert "opts.body" in key, "в ключе нет тела — разные действия склеятся в одно"


def test_reads_and_uploads_are_not_deduped():
    body = _body("api")
    assert "_m === 'GET'" in body, "дедуп не пропускает чтения"
    assert "FormData" in body and "_isFormBody" in body, (
        "загрузка файла обязана идти мимо дедупа: тело FormData нечем сравнить"
    )


def test_entry_is_removed_when_request_finishes():
    """Без очистки первая же ошибка залипла бы навсегда: кнопка умерла бы молча."""
    body = _body("api")
    assert re.search(r"\.finally\(\s*\(\)\s*=>\s*\{\s*_API_INFLIGHT\.delete\(", body), (
        "запись не удаляется по завершении запроса — после ошибки действие "
        "перестанет работать до перезапуска приложения"
    )


def test_real_request_still_happens_through_apiRaw():
    """Дедуп — обёртка. Сама отправка, ретрай по 401 и разбор ошибок — ниже."""
    raw = _body("_apiRaw")
    assert "fetchT(" in raw, "_apiRaw больше не шлёт запрос"
    assert "r.status === 401" in raw, "потерян повтор после протухшего токена"
    assert "showPaywall(" in raw, "потерян экран подписки на 403"
