"""Регрессия: нормализация ссылки/username источника парсинга.

Пользователи вставляют что угодно (`@name`, `https://t.me/name`,
`t.me/name?after=123`). Раньше на входе парсера был только `.lstrip("@")` — ссылка
`https://t.me/name` уходила как есть и источник не резолвился. normalize_source_ref
приводит всё к «голому» username, сохраняя приватные инвайты (+HASH / joinchat/…).

Функция достроена из незавершённого черновика параллельного агента (worktree) —
остальные его хелперы (CSV-экспорт, фильтры, clamp limit, валидация типа) уже были
реализованы на общей ветке инлайн, поэтому не дублируются; недостающей была только
надёжная нормализация ссылок.
"""
from __future__ import annotations

import pytest

from services.parser import normalize_source_ref


@pytest.mark.parametrize("raw,expected", [
    ("@durov", "durov"),
    ("durov", "durov"),
    ("  @spaced  ", "spaced"),
    ("https://t.me/durov", "durov"),
    ("http://t.me/durov", "durov"),
    ("t.me/name?after=123", "name"),          # query-хвост убран
    ("https://t.me/name/456", "name"),        # путь после username убран
    ("telegram.me/foo", "foo"),
    ("telegram.dog/bar", "bar"),
    ("", ""),
    ("   ", ""),
    ("@channel?x=1", "channel"),
])
def test_normalize_public(raw, expected):
    assert normalize_source_ref(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("https://t.me/+AbCdEf123", "+AbCdEf123"),   # приватный инвайт сохраняем
    ("t.me/+HASH?x=1", "+HASH"),                  # но query-хвост убираем
    ("t.me/joinchat/XYZ", "joinchat/XYZ"),
    ("t.me/joinchat/XYZ?y=2", "joinchat/XYZ"),
])
def test_normalize_private_invites(raw, expected):
    assert normalize_source_ref(raw) == expected


def test_wired_into_miniapp_entry():
    """Входная точка парсинга в mini_app_api должна звать normalize_source_ref,
    а не сырой .lstrip('@') (иначе вставленная ссылка не резолвится)."""
    import inspect
    from services import mini_app_api
    src = inspect.getsource(mini_app_api)
    assert "normalize_source_ref(str(body.get(\"source_ref\"" in src, (
        "parse-эндпойнт должен нормализовать source_ref общим хелпером"
    )
