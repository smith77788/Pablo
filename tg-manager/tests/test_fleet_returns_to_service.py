"""Дорога аккаунта ОБРАТНО в строй не должна ломаться молча.

Аварию, ради которой это написано, владелец видел так: «флот не работает».
Внутри было два звена, каждое из которых умеет отказать беззвучно:
  • колонка session_conflict_at появляется миграцией, а лаг миграции в этом
    репозитории уже случался (файл пропустили из-за совпавшего basename);
  • пассивное самолечение кулдаунов ловило свою ошибку в log.debug, то есть на
    проде её не было видно вовсе.

Обе проверки статические и Postgres не требуют — специально: за DSN-гейтом они
молча не запускались бы там, где живой базы нет.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_conflict_column_is_in_the_defensive_ddl():
    """session_conflict_at обязана появляться даже при лаге миграции.

    Колонку читают ОБА монитора здоровья и пассивное самолечение кулдаунов. Если
    schema_v185 не применилась (в этом репозитории уже был случай, когда файл
    миграции пропустили из-за совпавшего basename — см. комментарий про
    cf_relay_url), падает ровно то, что возвращает флот в строй.
    """
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "ADD COLUMN IF NOT EXISTS session_conflict_at" in src, (
        "колонки нет в защитном до-миграционном списке main.py — при лаге "
        "миграции флот останется запаркованным")


def test_self_heal_failure_is_not_swallowed_into_debug():
    """Сбой самолечения обязан быть виден на обычном уровне логов.

    Это единственный путь из 'cooldown' обратно в 'active'. Молчаливое падение
    выглядит снаружи как «аккаунты не работают без причины» — ровно тот случай,
    который и разбирался.
    """
    path = os.path.join(ROOT, "services", "account_monitor.py")
    src = open(path, encoding="utf-8").read()
    lines = src.split("\n")
    body = ""
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.name == "_heal_expired_cooldowns":
            body = "\n".join(lines[n.lineno - 1:n.end_lineno])
    assert body, "_heal_expired_cooldowns не найдена"
    assert "log.debug(" not in body, (
        "сбой самолечения уходит в debug — на проде его не видно")
    assert "log.warning(" in body or "log.error(" in body