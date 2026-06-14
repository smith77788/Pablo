"""PostgreSQL-backed FSM storage for aiogram 3.

States and data survive bot restarts.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType

log = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS fsm_state (
    bot_id      BIGINT NOT NULL,
    chat_id     BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    destiny     TEXT NOT NULL DEFAULT 'default',
    state       TEXT,
    data        JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bot_id, chat_id, user_id, destiny)
);
"""


class PostgresFSMStorage(BaseStorage):
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create(cls, pool: asyncpg.Pool) -> "PostgresFSMStorage":
        try:
            await pool.execute(_CREATE_TABLE)
        except Exception as exc:
            log.warning("pg_fsm_storage: could not create fsm_state table: %s", exc)
        return cls(pool)

    def _key(self, key: StorageKey) -> tuple:
        return (key.bot_id, key.chat_id, key.user_id, key.destiny)

    async def set_state(self, key: StorageKey, state: Optional[StateType] = None) -> None:
        state_str = state.state if hasattr(state, "state") else (str(state) if state is not None else None)
        try:
            await self._pool.execute(
                """INSERT INTO fsm_state (bot_id, chat_id, user_id, destiny, state, updated_at)
                   VALUES ($1, $2, $3, $4, $5, now())
                   ON CONFLICT (bot_id, chat_id, user_id, destiny)
                   DO UPDATE SET state=EXCLUDED.state, updated_at=now()""",
                *self._key(key), state_str,
            )
        except Exception as exc:
            log.warning("pg_fsm_storage set_state error: %s", exc)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        try:
            row = await self._pool.fetchrow(
                "SELECT state FROM fsm_state WHERE bot_id=$1 AND chat_id=$2 AND user_id=$3 AND destiny=$4",
                *self._key(key),
            )
            return row["state"] if row else None
        except Exception as exc:
            log.warning("pg_fsm_storage get_state error: %s", exc)
            return None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        try:
            await self._pool.execute(
                """INSERT INTO fsm_state (bot_id, chat_id, user_id, destiny, data, updated_at)
                   VALUES ($1, $2, $3, $4, $5::jsonb, now())
                   ON CONFLICT (bot_id, chat_id, user_id, destiny)
                   DO UPDATE SET data=EXCLUDED.data, updated_at=now()""",
                *self._key(key), json.dumps(data),
            )
        except Exception as exc:
            log.warning("pg_fsm_storage set_data error: %s", exc)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        try:
            row = await self._pool.fetchrow(
                "SELECT data FROM fsm_state WHERE bot_id=$1 AND chat_id=$2 AND user_id=$3 AND destiny=$4",
                *self._key(key),
            )
            if row and row["data"]:
                return dict(row["data"]) if isinstance(row["data"], dict) else json.loads(row["data"])
            return {}
        except Exception as exc:
            log.warning("pg_fsm_storage get_data error: %s", exc)
            return {}

    async def close(self) -> None:
        pass
