"""Список диалогов Хранилища одним проходом — те же строки, что тремя.

`_DIALOGS_SQL` читал сообщения владельца трижды (свод, последнее, последнее
живое) и считал непрочитанные коррелированным подзапросом на каждый диалог.
Новый запрос — один проход с array_agg и оконным максимумом исходящих:
30 000 диалогов 192 -> 76 мс в базе, экран 427 -> 362 мс, сводка организма
416 -> 280 мс. Сверяем с прежним запросом на данных со всеми краями: удалённое
последнее, NULL-даты, равные даты, прочитанное, пустой «живой» диалог.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
OWNER = 996011

_OLD_SQL = """
WITH agg AS (
    SELECT chat_id,
           MAX(peer_name)     AS peer_name,
           MAX(peer_username) AS peer_username,
           COUNT(*)           AS total,
           COUNT(*) FILTER (WHERE is_deleted)               AS deleted,
           COUNT(*) FILTER (WHERE media_type IS NOT NULL)   AS media,
           MIN(msg_date)      AS first_date,
           MAX(msg_date)      AS last_date,
           MAX(msg_date) FILTER (WHERE direction='out')     AS last_out,
           MAX(msg_date) FILTER (WHERE direction='in' AND NOT is_deleted) AS last_in
      FROM vault_messages
     WHERE owner_id=$1 AND ($2::bigint IS NULL OR chat_id=$2)
     GROUP BY chat_id
),
last AS (
    SELECT DISTINCT ON (chat_id) chat_id, text_enc, media_type, is_deleted, direction
      FROM vault_messages
     WHERE owner_id=$1 AND ($2::bigint IS NULL OR chat_id=$2)
     ORDER BY chat_id, msg_date DESC NULLS LAST, msg_id DESC
),
live AS (
    SELECT DISTINCT ON (chat_id) chat_id, text_enc AS live_text_enc,
           media_type AS live_media_type, direction AS live_direction
      FROM vault_messages
     WHERE owner_id=$1 AND NOT is_deleted AND ($2::bigint IS NULL OR chat_id=$2)
     ORDER BY chat_id, msg_date DESC NULLS LAST, msg_id DESC
)
SELECT a.*, l.text_enc, l.media_type, l.is_deleted AS last_deleted,
       l.direction AS last_direction,
       v.live_text_enc, v.live_media_type, v.live_direction,
       COALESCE(s.pinned, FALSE)   AS pinned,
       COALESCE(s.archived, FALSE) AS archived,
       COALESCE(s.muted, FALSE)    AS muted,
       s.read_at, s.handled_at, s.purchased_at, s.note,
       (SELECT COUNT(*) FROM vault_messages u
         WHERE u.owner_id=$1 AND u.chat_id=a.chat_id AND u.direction='in'
           AND NOT u.is_deleted
           AND u.msg_date > GREATEST(COALESCE(a.last_out, '-infinity'::timestamptz),
                                     COALESCE(s.read_at, '-infinity'::timestamptz))
       ) AS unread
  FROM agg a
  LEFT JOIN last l USING (chat_id)
  LEFT JOIN live v USING (chat_id)
  LEFT JOIN vault_chat_state s ON s.owner_id=$1 AND s.chat_id=a.chat_id
"""

def test_one_pass_equals_three_passes():
    import asyncpg

    from services import vault_dialogs as vd

    t0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)

    def at(m):
        return t0 + dt.timedelta(minutes=m)

    msgs = [
        # chat 1: последнее входящее, есть исходящее раньше
        (1, 1, "in", at(1), False, None), (1, 2, "out", at(2), False, None),
        (1, 3, "in", at(3), False, None), (1, 4, "in", at(4), False, "photo"),
        # chat 2: последнее удалено — «живое» берётся раньше
        (2, 1, "in", at(1), False, None), (2, 2, "in", at(5), True, None),
        # chat 3: всё удалено
        (3, 1, "in", at(1), True, None),
        # chat 4: NULL-даты и равные даты (решает msg_id)
        (4, 1, "in", None, False, None), (4, 2, "in", at(7), False, None),
        (4, 3, "out", at(7), False, None),
        # chat 5: прочитано после последнего входящего
        (5, 1, "in", at(1), False, None), (5, 2, "in", at(2), False, None),
    ]

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            for t in ("vault_messages", "vault_chat_state"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", OWNER)
            await pool.executemany(
                "INSERT INTO vault_messages(owner_id, chat_id, msg_id, direction, peer_name, "
                "text_enc, msg_date, is_deleted, is_edited, media_type, created_at) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,FALSE,$9,now())",
                [(OWNER, c, m, d, f"П{c}", f"e{c}.{m}", t, dl, mt)
                 for c, m, d, t, dl, mt in msgs])
            await pool.execute(
                "INSERT INTO vault_chat_state(owner_id, chat_id, read_at, pinned) "
                "VALUES($1, 5, $2, TRUE), ($1, 1, $3, FALSE)", OWNER, at(10), at(1))
            out = []
            for chat in (None, 2):
                new = [dict(r) for r in await pool.fetch(vd._DIALOGS_SQL, OWNER, chat)]
                old = [dict(r) for r in await pool.fetch(_OLD_SQL, OWNER, chat)]
                out.append((new, old))
            return out
        finally:
            for t in ("vault_messages", "vault_chat_state"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", OWNER)
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        res = loop.run_until_complete(_go())
    finally:
        loop.close()
    for new, old in res:
        key = lambda r: r["chat_id"]  # noqa: E731
        assert sorted(new, key=key) == sorted(old, key=key)
    assert len(res[0][0]) == 5 and len(res[1][0]) == 1
