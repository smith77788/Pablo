# -*- coding: utf-8 -*-
"""Пустой экран под срезом не должен выдавать себя за «здесь ничего нет».

Человек выбирает этап «Выиграно» в сделках или «только избранное» в хранилище,
список оказывается пустым — и экран пишет «Нет сделок · создайте первую» или
«Нет сохранённых сообщений». Это неправда: данные есть, просто не под этим
срезом. Хуже того, предлагаемое действие уводит создавать дубль того, что уже
существует.

Приём был отработан на рассылках: `renderBcasts` под активным `BCAST_FILTER`
рисует «По этому срезу пусто» с кнопкой «Показать все». Этот тест требует того
же от остальных экранов, где срез реально уходит в запрос.

Проверяется структура, а не формулировка: в ветке «список пуст» решение должно
ЗАВИСЕТЬ от переменной среза, и ветка «срез активен» обязана предлагать
действие, которое срез снимает, — иначе человек упрётся в тупик.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from miniapp_source import miniapp_source  # noqa: E402

SRC = miniapp_source()

# функция → (переменная среза, функция сброса среза)
GUARDED = {
    "loadDeals": ("DEALS_FILTER", "filterDeals"),
    "reloadVaultChat": ("VAULT_FILTER", "setVaultFilter"),
    "renderBcasts": ("BCAST_FILTER", "filterBcasts"),
}


def _body(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + name + r"\s*\(", SRC)
    assert m, f"функция {name}() не найдена — экран переименовали?"
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
    raise AssertionError(f"не удалось выделить тело {name}()")


def _empty_branch(body: str, name: str) -> str:
    """Ветка «список пуст» целиком: от проверки .length до конца блока."""
    m = re.search(r"if\s*\(\s*!\s*[\w.\[\]()]+\.length\s*\)\s*\{", body)
    if m:
        i = m.end() - 1
        depth, j = 0, i
        while j < len(body):
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                depth -= 1
                if depth == 0:
                    return body[i : j + 1]
            j += 1
    # однострочная форма без фигурных скобок
    m = re.search(r"if\s*\(\s*!\s*[\w.\[\]()]+\.length\s*\)([^\n]+)", body)
    assert m, f"в {name}() нет ветки «список пуст»"
    return m.group(1)


def test_detector_sees_the_branches():
    """Анти-пустота: без этого тест ниже прошёл бы на пустых строках."""
    for name in GUARDED:
        branch = _empty_branch(_body(name), name)
        assert "empty(" in branch, f"{name}(): ветка «пусто» ничего не рисует — сломан разбор"
        assert len(branch) > 80, f"{name}(): ветка подозрительно короткая ({len(branch)})"


def test_empty_under_filter_says_so_and_offers_reset():
    bad = []
    for name, (var, reset) in GUARDED.items():
        branch = _empty_branch(_body(name), name)
        if var not in branch:
            bad.append(f"{name}(): ветка «пусто» не смотрит на {var} — под срезом соврёт")
            continue
        # ветка под срезом должна вести к снятию среза
        if reset + "(" not in branch:
            bad.append(
                f"{name}(): под срезом не предлагает {reset}() — человеку нечем вернуться"
            )
        # и не должна звать создавать новое, пока срез активен
        seg = branch[branch.index(var):]
        head = seg[: seg.find("empty(", seg.find("empty(") + 1)] if seg.count("empty(") > 1 else seg
        if re.search(r"(Создайте|Нажмите \+|➕ Создать)", head):
            bad.append(
                f"{name}(): под активным срезом зовёт создавать новое — уведёт плодить дубль"
            )
    assert not bad, "срез скрывает данные, а экран молчит:\n    " + "\n    ".join(bad)
