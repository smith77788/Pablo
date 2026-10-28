# -*- coding: utf-8 -*-
"""Необратимое действие спрашивает и называет, скольких коснётся.

Два места запускали необратимое БЕЗ единого вопроса:

* массовая правка профиля (`submitAccProfile` в режиме AP_MASS) — среди операций
  «2FA пароль» и «Закрыть сторонние сессии». Пароль 2FA не восстановить, закрытые
  сессии не вернуть, а человек в массовом режиме не видит списка аккаунтов и не
  знает, сколько их выбрано;
* отправка сообщения выбранным контактам (`submitAccContMsg`) — сообщение уходит
  живым людям и отзыву не подлежит; при этом выбранные без @username отсеивались
  молча, так что число получателей не совпадало с числом галочек.

Проверяется структура, а не формулировка: перед запросом к серверу стоит
`await askConfirm(...)`, и в его текст попадает число целей.

22.10.2026 сюда добавлен откат версии контакта (`rollbackVersion`), и сам
гейт на нём подтянули дважды — обе дыры нашлись обратным контролем, а не
чтением:

* позиция запроса бралась по ПЕРВОМУ вхождению пути, а функция имеет право
  сходить на тот же путь до вопроса, чтобы узнать, о чём спрашивать (откат
  сначала запрашивает предпросмотр). Теперь ищется изменяющий вызов —
  `_mutating`;
* `plural(ch.length, …)` возвращает СЛОВО, а не число, и вопрос «изменит
  поля:» проезжал проверку «названо ли число». Аргументы plural( … ) из
  текста теперь вычёркиваются до поиска.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from miniapp_source import miniapp_source  # noqa: E402

SRC = miniapp_source()

# функция → (эндпойнт, по чему считается масштаб)
GUARDED = {
    "submitAccProfile": ("/api/miniapp/accounts/mass", "_massSelCount()"),
    "submitAccContMsg": ("/api/miniapp/dm/adhoc_send", "usernames.length"),
    # Откат версии контакта перезаписывает карточку целиком, а спрашивал
    # «Откатить к версии 3?» — число версии вместо числа того, что исчезнет.
    "rollbackVersion": ("/api/miniapp/uch/contacts/", "ch.length"),
}


def _mutating(body: str, endpoint: str) -> int:
    """Позиция ИЗМЕНЯЮЩЕГО запроса к эндпойнту, а не первого упоминания пути.

    Функция имеет право сходить на тот же путь ДО вопроса — чтобы узнать, о
    чём спрашивать: `rollbackVersion` сначала запрашивает предпросмотр
    (GET), потом спрашивает, потом перезаписывает (POST). Сравнение с первым
    вхождением пути объявляло бы такую функцию нарушителем, а значит толкало
    бы к обратному — спрашивать вслепую.
    """
    for m in re.finditer(re.escape(endpoint), body):
        if "method:'POST'" in body[m.end(): m.end() + 200] or \
           "method: 'POST'" in body[m.end(): m.end() + 200]:
            return m.start()
    return body.find(endpoint)


def _body(name: str) -> str:
    m = re.search(r"async function " + name + r"\s*\(", SRC)
    assert m, f"{name}() не найдена"
    i = SRC.index("{", m.end() - 1)
    depth, j = 0, i
    while j < len(SRC):
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i : j + 1]
        j += 1
    raise AssertionError(f"не выделить тело {name}()")


def _confirm_arg(body: str) -> str:
    """Аргумент askConfirm( … ) целиком, по балансу скобок."""
    k = body.find("askConfirm(")
    assert k >= 0, "askConfirm( не найден"
    i = body.index("(", k)
    depth, j = 0, i
    while j < len(body):
        if body[j] == "(":
            depth += 1
        elif body[j] == ")":
            depth -= 1
            if depth == 0:
                return body[i : j + 1]
        j += 1
    raise AssertionError("не выделить аргумент askConfirm")


def test_detector_sees_the_functions():
    """Анти-пустота: без этого тест ниже прошёл бы на пустых телах."""
    for name, (endpoint, _) in GUARDED.items():
        b = _body(name)
        assert len(b) > 300, f"{name}(): тело подозрительно короткое ({len(b)})"
        assert endpoint in b, f"{name}(): запроса к {endpoint} больше нет — переписали?"
        assert "askConfirm(" in b, f"{name}(): askConfirm пропал"


def test_confirm_precedes_the_request():
    """Вопрос стоит ДО запроса, иначе он уже ничего не предотвращает."""
    bad = []
    for name, (endpoint, _) in GUARDED.items():
        b = _body(name)
        ci, ei = b.find("askConfirm("), _mutating(b, endpoint)
        if not re.search(r"if\s*\(\s*!\s*await\s+askConfirm\(", b):
            bad.append(f"{name}(): результат askConfirm не проверяется — отказ не остановит запуск")
        elif ci > ei:
            bad.append(f"{name}(): askConfirm стоит ПОСЛЕ запроса к {endpoint}")
    assert not bad, "\n    ".join([""] + bad)


def test_confirm_names_the_scale():
    """В вопросе стоит число целей, а не абстрактное «продолжить?»."""
    bad = []
    for name, (_, counter) in GUARDED.items():
        b = _body(name)
        arg = _confirm_arg(b)
        # Склонение не заменяет число: `plural(ch.length, …)` возвращает СЛОВО
        # («поля»), и вопрос «изменит поля:» проезжал проверку, не называя,
        # сколько их. Поэтому аргументы plural( … ) из текста вычёркиваем и
        # ищем число в том, что останется.
        arg_wo_plural = re.sub(r"plural\([^)]*\)", "", arg)
        # число попадает в текст либо прямо, либо через переменную, посчитанную выше
        direct = counter in arg_wo_plural
        via_var = False
        for v in set(re.findall(r"\+\s*([A-Za-z_]\w*)\s*\+", arg_wo_plural)) | set(
            re.findall(r"\$\{\s*([A-Za-z_]\w*)", arg_wo_plural)
        ):
            dm = re.search(r"\b(?:const|let|var)\s+" + v + r"\s*=([^;]{0,200});", b[: b.find("askConfirm(")])
            if dm and re.escape(counter).replace(r"\(", "(").replace(r"\)", ")") in dm.group(1):
                via_var = True
                break
        if not (direct or via_var):
            bad.append(f"{name}(): в вопросе нет числа целей ({counter})")
        if "plural(" not in arg:
            bad.append(f"{name}(): число без склонения — «1 аккаунтах» читается как сбой")
    assert not bad, "\n    ".join([""] + bad)


def test_unrecoverable_options_carry_a_warning():
    """2FA и закрытие сессий отдельно предупреждают: их не откатить."""
    b = _body("submitAccProfile")
    arg_src = b[: b.find(_confirm_arg(b))] + _confirm_arg(b)
    for op, word in (("'2fa'", "восстановить нельзя"), ("'close_sessions'", "без возврата")):
        assert op in arg_src and word in arg_src, (
            f"для {op} пропало предупреждение о необратимости («{word}»)"
        )
