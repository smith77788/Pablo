"""Соединение пула без лишнего похода в базу на каждом возврате.

ЗАЧЕМ. asyncpg на КАЖДОМ возврате соединения в пул выполняет сброс сессии:

    SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;

и ждёт ответа, прежде чем отдать результат вызывающему. То есть любой
`pool.fetchval(...)` — это ДВА похода в базу: сам запрос и сброс. Замер
(`deploy/scripts/bench_db_latency.py`, база за 20 мс туда-обратно):
`pool.fetchval("select 1")` — 43 мс со сбросом и 21 мс без него. На /start
(11 обращений к базе) и на главной мини-аппа (десятки) это прямая половина
времени ответа, а соединение всё это время занято и недоступно другим —
пул из 20 соединений делят бот, мини-апп и ~70 фоновых циклов.

Сброс нужен только соединению, которое оставило за собой состояние сессии:
`SET` без `LOCAL`, сессионную advisory-блокировку, `LISTEN`, курсор
`DECLARE ... WITH HOLD`, `set_config(...)`. Таких мест в коде единицы
(lock_timeout миграций, восстановление из бэкапа), а обычные запросы
состояния не оставляют. Поэтому соединение помечается «грязным», когда через
него прошёл такой текст, и только тогда при возврате получает полный сброс,
как раньше. Чистое соединение возвращается без похода в базу.

Открытая транзакция на возврате по-прежнему откатывается (asyncpg добавляет
ROLLBACK сам, независимо от текста сброса), подписчики `add_listener` тоже
ведут к полному сбросу.

Ошибиться в «чистую» сторону дорого, в «грязную» — дёшево (один лишний
сброс). Поэтому распознавание намеренно грубое: любое из ключевых слов в
начале любого оператора, а функции блокировок и set_config — где угодно в
тексте.
"""
from __future__ import annotations

import re

import asyncpg

# Оператор начинается с начала текста или после «;», перед ним могут стоять
# комментарии. `SET LOCAL` живёт до конца транзакции — сброс ему не нужен.
_STMT_START = r"(?:\A|;)\s*(?:(?:--[^\n]*(?:\n|\Z)|/\*.*?\*/)\s*)*"
_SESSION_STATE = re.compile(
    _STMT_START
    + r"(?:SET\s+(?!LOCAL\b)|RESET\b|LISTEN\b|DECLARE\b|PREPARE\b|LOAD\b)"
    r"|\bpg_(?:try_)?advisory_lock(?:_shared)?\s*\("
    r"|\bset_config\s*\(",
    re.IGNORECASE | re.DOTALL,
)

# Решение по тексту запроса кэшируется: запросы в коде — константы, и
# регулярка на каждый вызов не нужна. Потолок — чтобы динамический SQL не
# раздул словарь.
_verdicts: dict[str, bool] = {}
_VERDICTS_MAX = 20_000


def leaves_session_state(query) -> bool:
    """Может ли этот текст оставить состояние сессии после себя."""
    if not isinstance(query, str):
        return True
    hit = _verdicts.get(query)
    if hit is None:
        hit = _SESSION_STATE.search(query) is not None
        if len(_verdicts) >= _VERDICTS_MAX:
            _verdicts.clear()
        _verdicts[query] = hit
    return hit


class LeanResetConnection(asyncpg.Connection):
    """asyncpg.Connection, сбрасывающее сессию только когда есть что сбрасывать."""

    __slots__ = ("_session_dirty",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session_dirty = False

    def _mark(self, query) -> None:
        if not self._session_dirty and leaves_session_state(query):
            self._session_dirty = True

    # Простой запрос без аргументов идёт мимо _get_statement — ловим отдельно.
    async def execute(self, query: str, *args, timeout: float = None) -> str:
        self._mark(query)
        return await super().execute(query, *args, timeout=timeout)

    # Через _get_statement идут fetch/fetchrow/fetchval/executemany/prepare/cursor.
    async def _get_statement(self, query, timeout, **kwargs):
        self._mark(query)
        return await super()._get_statement(query, timeout, **kwargs)

    async def add_listener(self, channel, callback):
        self._session_dirty = True
        return await super().add_listener(channel, callback)

    def _get_reset_query(self):
        if self._session_dirty:
            return super()._get_reset_query()
        return ""

    async def reset(self, *, timeout=None):
        await super().reset(timeout=timeout)
        # Сброс прошёл (или его не требовалось) — сессия снова чистая.
        self._session_dirty = False
