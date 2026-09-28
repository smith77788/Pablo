"""Прокси выбирается только среди своих.

Прокси в этом продукте — не общий ресурс, а привязка к конкретному аккаунту:
на этом держится анти-детект (один аккаунт — один IP) и на этом же держится
разделение владельцев. Взять чужой прокси значит пустить свою активность через
чужой IP: чужой трафик расходуется, а бан за нашу активность прилетает
владельцу прокси и его аккаунтам.

Так и было в `strike_engine.rotate_proxy_for_strike`: SELECT ... FROM
user_proxies WHERE is_active=TRUE ORDER BY RANDOM() LIMIT 1 — по всей
платформе, с игнорированием переданного account_id. Функцию никто не звал,
поэтому в прод это не уехало; удалена, чтобы не уехало потом.

Тест запрещает выборку прокси без владельца. Исключение одно и названо
явно — плановая уборка, в которой пользователя нет вообще.
"""
from __future__ import annotations

import ast
import glob
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Файл → почему выборка по всей платформе здесь законна.
_РАЗРЕШЕНО = {
    "services/db_maintenance.py": (
        "плановая уборка базы: сверяет память инфраструктуры со всеми живыми "
        "прокси платформы, пользователя в этой операции нет"
    ),
}


def _строки_sql(path: str):
    """Строковые литералы файла (без частей f-строк по отдельности)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    inner = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                inner.add(id(part))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in inner:
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(
                p.value for p in node.values if isinstance(p, ast.Constant)
            )


def _отдаёт_прокси(sql: str) -> bool:
    """Запрос возвращает сам адрес прокси (а не, скажем, флаг)."""
    голова = sql[: sql.upper().find("FROM")]
    return bool(re.search(r"proxy_url|\*", голова, re.I))


def найти_невладельческие_выборки(sql: str) -> bool:
    u = " ".join(sql.split())
    if not re.match(r"\s*(WITH|SELECT)", u, re.I):
        return False
    if not re.search(r"\bFROM\s+user_proxies\b", u, re.I):
        return False
    if not _отдаёт_прокси(u):
        return False
    return not re.search(r"owner_id", u, re.I)


def test_ни_один_запрос_не_берёт_чужой_прокси():
    находки = []
    for каталог in ("services", "bot", "database"):
        for path in sorted(glob.glob(os.path.join(_ROOT, каталог, "**", "*.py"), recursive=True)):
            rel = os.path.relpath(path, _ROOT)
            if rel in _РАЗРЕШЕНО:
                continue
            for lineno, sql in _строки_sql(path):
                if найти_невладельческие_выборки(sql):
                    находки.append(f"{rel}:{lineno}")
    assert not находки, (
        "выборка прокси без владельца: " + ", ".join(находки)
        + ". Свой прокси берётся по owner_id (см. services/proxy_rotation); "
        "чужой IP под нашей активностью переносит бан на чужие аккаунты."
    )


def test_разрешение_дано_реальному_файлу():
    # Исключение, переставшее существовать, — это забытая строка, которая завтра
    # прикроет что-то другое с тем же именем.
    for rel in _РАЗРЕШЕНО:
        assert os.path.exists(os.path.join(_ROOT, rel)), f"нет файла {rel}"


def test_детектор_отличает_свой_запрос_от_чужого():
    assert найти_невладельческие_выборки(
        "SELECT proxy_url FROM user_proxies WHERE is_active=TRUE ORDER BY RANDOM() LIMIT 1"
    )
    assert not найти_невладельческие_выборки(
        "SELECT proxy_url FROM user_proxies WHERE owner_id=$1 AND is_active"
    )
    # Не адрес прокси, а флаг по уже отобранной своей строке — не находка.
    assert not найти_невладельческие_выборки(
        "SELECT NOT is_active FROM user_proxies WHERE id=$1"
    )
    # Не выборка вовсе.
    assert not найти_невладельческие_выборки(
        "UPDATE user_proxies SET is_active=FALSE WHERE id=$1"
    )
