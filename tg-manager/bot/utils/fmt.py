"""Числа для текстов бота по-русски.

`f"{n:,}"` даёт «12,345»: запятая — английский разделитель разрядов, а по-русски
запятая отделяет дробную часть, то есть «12,345 подписчиков» читается как
двенадцать с хвостиком. Разряды по-русски разделяются пробелом: «12 345».
"""
from __future__ import annotations


def num_ru(value) -> str:
    """12345 → «12 345», 1234.5 → «1 234.5»; не число — как есть."""
    try:
        return f"{value:,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def plural(n, one: str, few: str, many: str) -> str:
    """Слово по числу: 1 бот, 2 бота, 5 ботов, 11 ботов, 21 бот.

    Вместо «{n} бот(ов)»: скобка с окончанием — это форма из бланка, а не
    фраза, и владелец её читает как недоделку.
    """
    try:
        k = abs(int(n))
    except (TypeError, ValueError):
        return many
    if 11 <= k % 100 <= 14:
        return many
    if k % 10 == 1:
        return one
    if 2 <= k % 10 <= 4:
        return few
    return many
