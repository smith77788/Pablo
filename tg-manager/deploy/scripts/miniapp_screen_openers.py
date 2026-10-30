#!/usr/bin/env python3.12
"""Список функций мини-аппа, открывающих экран без аргументов — по одной в строку.

Нужен режиму `sweep` у `deploy/scripts/render_miniapp.mjs`: прежде он умел
проверять только пять вкладок, а экранов, которые открываются одним вызовом,
в приложении 114. Список строится из исходника, а не поддерживается руками:
руками он разошёлся бы с кодом ровно так же, как любая перепись по рукописному
списку (о чём в CLAUDE.md есть отдельный разбор).

Открывающей считается функция, в теле которой есть `push('s-…')` — именно так
мини-апп показывает экран.

Функции с ОДНИМ и ДВУМЯ обязательными аргументами тоже идут в список —
строкой вида `имя|1` или `имя|1|1`. Раньше их отбрасывали «нечем позвать вслепую», и обход показывал 111
экранов из 185: треть приложения не мерилась, а отчёт об этом молчал. Звать их
есть чем: заглушка отдаёт одни и те же данные на любой URL, так что экран
детали рисуется при любом id. Обход помечает такой вызов как пробный — если
функция упадёт на подставленном аргументе, это не находка, а просто непокрытый
экран, и он виден в строке охвата.

Использование:
    python3.12 deploy/scripts/miniapp_screen_openers.py > /tmp/openers.txt
    node deploy/scripts/render_miniapp.mjs sweep /tmp/openers.txt
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2] / "mini_app"
_FN = re.compile(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)")
_PUSH = re.compile(r"push\(\s*['\"]s-[\w-]+['\"]")


def _body(src: str, start: int) -> str | None:
    i = src.find("{", start)
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return None


def _params(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def _required(params: list[str]) -> int:
    """Сколько аргументов обязательны: со значением по умолчанию — не в счёт."""
    return sum(1 for p in params if "=" not in p and not p.startswith("..."))


def openers() -> list[str]:
    files = [ROOT / "index.html"] + sorted((ROOT / "screens").glob("*.js"))
    out: list[str] = []
    for f in files:
        src = f.read_text(encoding="utf-8")
        for m in _FN.finditer(src):
            need = _required(_params(m.group(2)))
            if need > 2:
                continue                      # чем заполнить третий — неизвестно
            body = _body(src, m.end() - 1)
            if body and _PUSH.search(body):
                out.append(m.group(1) + "|1" * need)
    return out


if __name__ == "__main__":
    names = openers()
    if not names:
        print("не найдено ни одной открывающей функции — сломан разбор, "
              "а не приложение", file=sys.stderr)
        raise SystemExit(2)
    print("\n".join(names))
