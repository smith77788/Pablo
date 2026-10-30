#!/usr/bin/env python3.12
"""Список функций мини-аппа, открывающих экран без аргументов — по одной в строку.

Нужен режиму `sweep` у `deploy/scripts/render_miniapp.mjs`: прежде он умел
проверять только пять вкладок, а экранов, которые открываются одним вызовом,
в приложении 114. Список строится из исходника, а не поддерживается руками:
руками он разошёлся бы с кодом ровно так же, как любая перепись по рукописному
списку (о чём в CLAUDE.md есть отдельный разбор).

Открывающей считается функция, в теле которой есть `push('s-…')` — именно так
мини-апп показывает экран. Берутся только функции БЕЗ обязательных аргументов:
остальные требуют id сущности, и позвать их вслепую нечем.

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


def openers() -> list[str]:
    files = [ROOT / "index.html"] + sorted((ROOT / "screens").glob("*.js"))
    out: list[str] = []
    for f in files:
        src = f.read_text(encoding="utf-8")
        for m in _FN.finditer(src):
            if m.group(2).strip():
                continue                      # нужен id сущности — вслепую не позвать
            body = _body(src, m.end() - 1)
            if body and _PUSH.search(body):
                out.append(m.group(1))
    return out


if __name__ == "__main__":
    names = openers()
    if not names:
        print("не найдено ни одной открывающей функции — сломан разбор, "
              "а не приложение", file=sys.stderr)
        raise SystemExit(2)
    print("\n".join(names))
