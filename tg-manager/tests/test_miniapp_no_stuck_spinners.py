"""Ратчет: экран не остаётся в вечной загрузке, и кнопки не зовут пустоту.

Экран мини-аппа ставит спиннер в контейнер, идёт в API и заменяет спиннер
результатом. Если запрос падает, а блок `catch` пишет НЕ во все контейнеры, в
которые ставил спиннер, часть экрана крутится вечно. Кнопок там нет — выйти
можно только через /menu, ровно как в `test_no_dead_end_screens`.

Так было на шести экранах. Отдельно на аналитике кнопка «↺ Повторить» звала
`loadPromoData()` — функции с таким именем в приложении нет, то есть
единственный выход с экрана ошибки бросал ReferenceError.

Оба теста — сплошные по файлам мини-аппа: такие вещи находятся только сплошной
проверкой, потому что каждая по отдельности выглядит мелочью.
"""
from __future__ import annotations

import re
from pathlib import Path

MINI = Path(__file__).resolve().parents[1] / "mini_app"

# Контейнеры, которые снимает не `catch`, а другой механизм.
SPINNER_EXCEPTIONS = {
    # invite.js: ошибки приходят через Promise.allSettled (opsD.reason), и
    # контейнер заполняется в обычном потоке, а не в catch.
    "massInviteHistory": "ошибку разбирает allSettled, а не catch",
}

# Имена, которые выглядят вызовом функции, но ею не являются.
NOT_FUNCTIONS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "new",
    "alert", "confirm", "prompt", "close", "open", "print", "setTimeout",
    "clearTimeout", "setInterval", "parseInt", "parseFloat", "Number",
    "String", "Boolean", "Array", "Object", "JSON", "Math", "Date",
    "encodeURIComponent", "decodeURIComponent", "require",
}


def _sources() -> dict[str, str]:
    out = {"mini_app/index.html": (MINI / "index.html").read_text("utf-8")}
    for js in sorted((MINI / "screens").glob("*.js")):
        out[f"mini_app/screens/{js.name}"] = js.read_text("utf-8")
    return out


def _all_src() -> str:
    return "\n".join(_sources().values())


def _spinner_containers(src: str) -> set[str]:
    return set(re.findall(
        r"""txt\(\s*['"]([\w-]+)['"]\s*,\s*['"`]<div class="spin-wrap\"""", src))


def _catch_bodies(src: str) -> str:
    """Тела всех блоков catch одной строкой — со сбалансированными скобками."""
    out: list[str] = []
    i = 0
    while True:
        i = src.find("catch", i)
        if i < 0:
            break
        brace = src.find("{", i)
        if brace < 0:
            break
        depth, j = 1, brace + 1
        while j < len(src) and depth:
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
            j += 1
        out.append(src[brace:j])
        i = brace + 1
    return "\n".join(out)


def test_spinner_detector_sees_the_screens():
    """Страховка измерителя: пустой список превратил бы ратчет в заглушку."""
    assert len(_spinner_containers(_all_src())) > 100


def test_every_spinner_is_cleared_when_the_request_fails():
    src = _all_src()
    catches = _catch_bodies(src)
    stuck = sorted(
        cid for cid in _spinner_containers(src)
        if cid not in SPINNER_EXCEPTIONS
        and not re.search(r"""['"]""" + re.escape(cid) + r"""['"]""", catches))
    assert not stuck, (
        "при ошибке эти контейнеры остаются в вечной загрузке — экран без "
        "единой кнопки:\n  " + "\n  ".join(stuck))


def test_spinner_exceptions_still_exist():
    """Список исключений должен описывать настоящее, а не прошлое."""
    live = _spinner_containers(_all_src())
    stale = [k for k in SPINNER_EXCEPTIONS if k not in live]
    assert not stale, f"в списке исключений остались несуществующие: {stale}"


# ── Кнопка обязана звать существующую функцию ────────────────────────────────

def _defined_names(src: str) -> set[str]:
    names = set(re.findall(r"(?:async\s+)?function\s+(\w+)\s*\(", src))
    names |= set(re.findall(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", src))
    names |= set(re.findall(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?function", src))
    names |= set(re.findall(r"window\.(\w+)\s*=", src))
    return names


def test_handler_detector_resolves_known_names():
    """Страховка измерителя на заведомо здоровом примере."""
    defined = _defined_names(_all_src())
    for known in ("openCloud", "cloudUpload", "loadAnalytics", "errHtml", "toast"):
        assert known in defined, f"измеритель не видит {known}()"


def test_every_button_calls_a_function_that_exists():
    src = _all_src()
    defined = _defined_names(src)
    missing: dict[str, int] = {}

    def _note(name: str) -> None:
        if name not in defined and name not in NOT_FUNCTIONS:
            missing[name] = missing.get(name, 0) + 1

    for m in re.finditer(
            r"""on(?:click|change|input|submit)\s*=\s*["'`]([^"'`]+)["'`]""", src):
        for fn in re.findall(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(", m.group(1)):
            _note(fn)
    # Имя функции, переданное строкой: кнопка «Повторить» в errHtml и действие
    # в пустом состоянии empty(..., {label, fn}).
    for m in re.finditer(r"""errHtml\([^,]+,\s*['"](\w+)\(""", src):
        _note(m.group(1))
    for m in re.finditer(r"""fn\s*:\s*['"](\w+)\(?""", src):
        _note(m.group(1))

    assert not missing, (
        "кнопка зовёт несуществующую функцию — нажатие бросает ReferenceError:\n  "
        + "\n  ".join(f"{k} — {v} упоминаний" for k, v in sorted(missing.items())))
