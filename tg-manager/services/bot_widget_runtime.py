"""Виджеты в чатах — сторона бота: учёт отправленных и ответы на касания.

Виджет-голосование (опрос, оценка, «кто идёт») при касании шлёт боту запрос
«vote:N» (обновление widget_query, а из пересланной копии общего виджета —
shared_widget_query). Бот обязан ответить в течение 15 секунд, иначе виджет
скажет «Бот не ответил». Ответ — JSON {my, v, r} (свой голос, счёт, номер
правки) или {err}; заодно бот правит данные виджета, и новый счёт видят все
копии.

Голоса лежат в bot_widget_votes: один голос человека на виджет, повторное
касание того же варианта снимает голос. Строка виджета берётся FOR UPDATE —
два касания разных людей в одну секунду не теряют голос друг друга и не
выдают одинаковый номер правки.
"""
from __future__ import annotations

import json
import logging

from services import bot_widgets as bw

log = logging.getLogger(__name__)

_ERR_GONE = "Этот виджет больше не принимает голоса"
_ERR_FAIL = "Бот не смог принять голос — попробуйте ещё раз"
_ERR_UNKNOWN = "Бот не понял касание"
HOT_AFTER_VOTE_S = 120


async def record_sent(pool, *, owner_id: int, bot_id: int, chat_id: int, message_id: int,
                      widget_id: str | None, template: str, title: str,
                      public_data: str, allow_queries: bool) -> int | None:
    """Запомнить отправленный виджет; повтор той же отправки — та же строка."""
    return await pool.fetchval(
        """INSERT INTO bot_widgets(owner_id, bot_id, chat_id, message_id, widget_id,
                                   template, title, public_data, allow_queries)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
           ON CONFLICT (bot_id, chat_id, message_id) DO UPDATE SET updated_at=NOW()
           RETURNING id""",
        int(owner_id), int(bot_id), int(chat_id), int(message_id), widget_id,
        template, title[:120], public_data or "", bool(allow_queries))


def _who(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()
    return (name or (f"@{user['username']}" if user.get("username") else "Гость"))[:40]


def parse_vote(data: str) -> int | None:
    """«vote:N» → N; что угодно другое — None (данные из чата — не инструкции)."""
    s = str(data or "")
    if not s.startswith("vote:"):
        return None
    tail = s[5:]
    if not tail.isdigit() or len(tail) > 2:
        return None
    return int(tail)


async def _find_widget(conn, bot_id: int, q: dict):
    wid = q.get("widget_id")
    if wid:
        return await conn.fetchrow(
            "SELECT * FROM bot_widgets WHERE bot_id=$1 AND widget_id=$2 "
            "ORDER BY id LIMIT 1 FOR UPDATE", int(bot_id), str(wid))
    chat = (q.get("chat") or {}).get("id")
    mid = q.get("message_id")
    if chat is None or mid is None:
        return None
    return await conn.fetchrow(
        "SELECT * FROM bot_widgets WHERE bot_id=$1 AND chat_id=$2 AND message_id=$3 FOR UPDATE",
        int(bot_id), int(chat), int(mid))


async def apply_vote(pool, bot_id: int, q: dict) -> tuple[dict, dict | None]:
    """Принять голос в базе. (ответ виджету, что править у Telegram | None)."""
    option = parse_vote(q.get("data"))
    user = q.get("from") or {}
    voter = user.get("id")
    if option is None or not voter:
        return {"err": _ERR_UNKNOWN}, None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _find_widget(conn, bot_id, q)
            if not row or not bw.is_vote_template(row["template"]):
                return {"err": _ERR_GONE}, None
            data = bw.load_data(row["public_data"])
            n = bw.vote_options(row["template"], data)
            if not 0 <= option < n:
                return {"err": _ERR_UNKNOWN}, None
            prev = await conn.fetchval(
                "SELECT option FROM bot_widget_votes WHERE widget_row=$1 AND voter_id=$2",
                row["id"], int(voter))
            if prev == option:
                await conn.execute(
                    "DELETE FROM bot_widget_votes WHERE widget_row=$1 AND voter_id=$2",
                    row["id"], int(voter))
                my = -1
            else:
                await conn.execute(
                    """INSERT INTO bot_widget_votes(widget_row, voter_id, voter_name, option)
                       VALUES($1,$2,$3,$4)
                       ON CONFLICT (widget_row, voter_id) DO UPDATE
                       SET option=EXCLUDED.option, voter_name=EXCLUDED.voter_name,
                           voted_at=NOW()""",
                    row["id"], int(voter), _who(user), option)
                my = option
            counts = [0] * n
            for r in await conn.fetch(
                    "SELECT option, COUNT(*) AS c FROM bot_widget_votes "
                    "WHERE widget_row=$1 GROUP BY option", row["id"]):
                if 0 <= r["option"] < n:
                    counts[r["option"]] = int(r["c"])
            names = None
            if row["template"] == "rsvp":
                names = [r["voter_name"] for r in await conn.fetch(
                    "SELECT voter_name FROM bot_widget_votes WHERE widget_row=$1 AND option=0 "
                    "ORDER BY voted_at LIMIT $2", row["id"], bw.RSVP_NAMES_MAX)]
            rev = int(row["revision"] or 0) + 1
            pub = bw.vote_public(data, counts, rev, names)
            text = bw.dump_data(pub)
            await conn.execute(
                "UPDATE bot_widgets SET public_data=$2, revision=$3, updated_at=NOW() "
                "WHERE id=$1", row["id"], text, rev)
    answer = {"my": my, "v": counts, "r": rev}
    if names is not None:
        answer["n"] = pub.get("n", [])
    edit = {"chat_id": row["chat_id"], "message_id": row["message_id"],
            "widget_id": row["widget_id"], "public_data": text}
    return answer, edit


async def on_query(pool, http, token: str, bot_id: int, q: dict) -> dict:
    """Касание в виджете: принять, поправить данные у Telegram, ОТВЕТИТЬ.

    Ответ уходит всегда — и когда правка не удалась, и при сбое базы: без
    ответа виджет 15 секунд крутит ожидание и пишет «Бот не ответил».
    """
    answer: dict
    try:
        answer, edit = await apply_vote(pool, bot_id, q)
        if edit:
            res = await bw.edit_data(http, token, chat_id=edit["chat_id"],
                                     message_id=edit["message_id"],
                                     widget_id=edit["widget_id"],
                                     public_data=edit["public_data"])
            if not res.get("ok"):
                log.info("bot_widgets: bot=%s правка счёта не прошла: %s", bot_id, res.get("error"))
            try:
                from services import workflow_runtime
                workflow_runtime.mark_hot(bot_id, HOT_AFTER_VOTE_S)
            except Exception:
                log.debug("bot_widgets: mark_hot bot=%s", bot_id, exc_info=True)
    except Exception:
        log.exception("bot_widgets: голос не принят bot=%s", bot_id)
        answer = {"err": _ERR_FAIL}
    qid = q.get("id")
    if qid:
        try:
            await bw.answer_query(http, token, qid, json.dumps(answer, ensure_ascii=False,
                                                                 separators=(",", ":")))
        except Exception:
            log.warning("bot_widgets: answerWidgetQuery bot=%s не ушёл", bot_id, exc_info=True)
    return answer
