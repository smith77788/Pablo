"""Единый инбокс: ответы людей со всех аккаунтов, Хранилища и ботов-продавцов.

Зачем. Человек, получивший рассылку, отвечает тому аккаунту, который ему
написал. Аккаунтов у владельца десятки, и ответ видно только в самом Telegram,
открыв нужную сессию. Ответы же бизнес-аккаунта лежат в Хранилище
(vault_messages), а переписка ботов-продавцов — в bot_sales_dialogs. Три
разных места, и ни в одном нельзя увидеть «кто сейчас ждёт ответа».

Плюс операторский релей ботов (relay_sessions/relay_messages): переписка,
которую бот пересылает оператору. Здесь все четыре сводятся в один список РАЗГОВОРОВ по человеку (peer_user_id):
один человек, написавший двум аккаунтам и боту, — это одна строка, а не три.
Каналы, через которые он писал, перечисляются в строке.

Чистые функции (`merge_threads`, `merge_messages`) отделены от выборки из базы:
их проверяют тесты без пула, а запросы — тест против настоящей схемы.

Входящий текст из Telegram — данные, не инструкции: здесь он только хранится
(зашифрованным) и показывается, никуда не исполняется.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SRC_FLEET = "fleet"     # аккаунт флота (inbox_messages)
SRC_VAULT = "vault"     # бизнес-аккаунт (vault_messages)
SRC_BOT = "bot"         # бот-продавец (bot_sales_dialogs)
SRC_RELAY = "relay"     # операторский релей бота (relay_sessions/relay_messages)

SOURCE_LABEL = {
    SRC_FLEET: "Аккаунт",
    SRC_VAULT: "Хранилище",
    SRC_BOT: "Бот-продавец",
    SRC_RELAY: "Релей бота",
}

_PREVIEW = 140


def _ts(v):
    """Время для сортировки: naive считаем UTC, None — самое раннее."""
    if v is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def _decrypt(enc):
    if not enc:
        return ""
    try:
        from services.token_vault import decrypt_token
        return decrypt_token(enc) or ""
    except Exception:
        return ""


# ── Запись ─────────────────────────────────────────────────────────────────

async def record_incoming(pool, owner_id: int, account_id: int, peer: dict,
                          msg_id: int, text: str, msg_date=None) -> bool:
    """Сохранить входящее личное сообщение аккаунта флота. True — записано.

    Идемпотентно по (account_id, peer_user_id, msg_id): повторная доставка
    апдейта Telegram (а она бывает после переподключения) дубля не даёт.
    """
    pid = peer.get("peer_user_id")
    if not pid or not msg_id:
        return False
    from services.token_vault import encrypt_token
    text_enc = encrypt_token(text) if text else None
    status = await pool.execute(
        """INSERT INTO inbox_messages
               (owner_id, account_id, peer_user_id, peer_name, peer_username,
                msg_id, text_enc, msg_date)
           VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8, now()))
           ON CONFLICT (account_id, peer_user_id, msg_id) DO NOTHING""",
        int(owner_id), int(account_id), int(pid), peer.get("peer_name"),
        peer.get("peer_username"), int(msg_id), text_enc, msg_date)
    return str(status).endswith(" 1")


async def mark_read(pool, owner_id: int, peer_user_id: int) -> int:
    """Отметить прочитанными все входящие от человека. Возвращает сколько."""
    status = await pool.execute(
        "UPDATE inbox_messages SET is_read=TRUE "
        "WHERE owner_id=$1 AND peer_user_id=$2 AND NOT is_read",
        int(owner_id), int(peer_user_id))
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


# ── Чистое слияние ─────────────────────────────────────────────────────────

def merge_threads(fleet=None, vault=None, bots=None, contacts=None,
                  only_waiting: bool = False, limit: int = 100,
                  relay=None) -> list[dict]:
    """Свести разговоры трёх источников в один список по человеку.

    fleet: [{peer_user_id, peer_name, peer_username, account_id, last_at,
             last_text, unread, total}] — по одной строке на (человек, аккаунт);
    vault: [{peer_user_id, peer_name, peer_username, last_at, last_text,
             last_direction, total}];
    bots:  [{peer_user_id, peer_name, peer_username, bot_id, bot_username,
             last_at, last_text, last_role, handed_off, total}];
    relay: [{peer_user_id, peer_name, peer_username, bot_id, bot_username,
             session_id, last_at, last_text, last_direction, total}];
    contacts: {peer_user_id: contact_id}.

    «Ждёт ответа» — последнее слово в разговоре за человеком: входящее на
    аккаунт флота (исходящих аккаунта мы не видим, поэтому непрочитанное
    входящее и есть ожидание), входящее в Хранилище, реплика покупателя боту
    или передача бота человеку (handoff).
    """
    contacts = contacts or {}
    by: dict[int, dict] = {}

    def _slot(pid, name, username):
        t = by.get(pid)
        if t is None:
            t = {"peer_user_id": pid, "name": name or "", "username": username or "",
                 "last_at": None, "last_text": "", "last_source": None,
                 "channels": [], "unread": 0, "total": 0, "waiting": False,
                 "contact_id": contacts.get(pid)}
            by[pid] = t
        if not t["name"] and name:
            t["name"] = name
        if not t["username"] and username:
            t["username"] = username
        return t

    def _touch(t, at, text, src, waiting):
        if t["last_at"] is None or _ts(at) >= _ts(t["last_at"]):
            t["last_at"] = at
            t["last_text"] = (text or "")[:_PREVIEW]
            t["last_source"] = src
            t["waiting"] = bool(waiting)

    for r in fleet or []:
        pid = r.get("peer_user_id")
        if not pid:
            continue
        t = _slot(pid, r.get("peer_name"), r.get("peer_username"))
        unread = int(r.get("unread") or 0)
        t["unread"] += unread
        t["total"] += int(r.get("total") or 0)
        t["channels"].append({"source": SRC_FLEET, "account_id": r.get("account_id"),
                              "label": f"Аккаунт #{r.get('account_id')}"})
        _touch(t, r.get("last_at"), r.get("last_text"), SRC_FLEET, unread > 0)

    for r in vault or []:
        pid = r.get("peer_user_id")
        if not pid:
            continue
        t = _slot(pid, r.get("peer_name"), r.get("peer_username"))
        t["total"] += int(r.get("total") or 0)
        t["channels"].append({"source": SRC_VAULT, "label": "Хранилище"})
        _touch(t, r.get("last_at"), r.get("last_text"), SRC_VAULT,
               r.get("last_direction") == "in")

    for r in bots or []:
        pid = r.get("peer_user_id")
        if not pid:
            continue
        t = _slot(pid, r.get("peer_name"), r.get("peer_username"))
        t["total"] += int(r.get("total") or 0)
        bu = r.get("bot_username")
        t["channels"].append({"source": SRC_BOT, "bot_id": r.get("bot_id"),
                              "label": f"Бот @{bu}" if bu else "Бот-продавец"})
        _touch(t, r.get("last_at"), r.get("last_text"), SRC_BOT,
               bool(r.get("handed_off")) or r.get("last_role") == "user")

    for r in relay or []:
        pid = r.get("peer_user_id")
        if not pid:
            continue
        t = _slot(pid, r.get("peer_name"), r.get("peer_username"))
        t["total"] += int(r.get("total") or 0)
        bu = r.get("bot_username")
        t["channels"].append({"source": SRC_RELAY, "bot_id": r.get("bot_id"),
                              "session_id": r.get("session_id"),
                              "label": f"Релей @{bu}" if bu else "Релей бота"})
        _touch(t, r.get("last_at"), r.get("last_text"), SRC_RELAY,
               r.get("last_direction") == "in")

    out = list(by.values())
    if only_waiting:
        out = [t for t in out if t["waiting"]]
    # Ждущие ответа — первыми, внутри — самые свежие сверху.
    out.sort(key=lambda t: (not t["waiting"], -_ts(t["last_at"]).timestamp()))
    return out[: max(1, int(limit))]


def merge_messages(fleet=None, vault=None, bots=None, limit: int = 200,
                   relay=None) -> list[dict]:
    """Одна лента сообщений разговора из всех источников, по времени (старые сверху).

    Каждый элемент: {at, direction: in|out, text, source, channel}.
    """
    items: list[dict] = []
    for r in fleet or []:
        items.append({"at": r.get("msg_date"), "direction": "in",
                      "text": r.get("text") or "", "source": SRC_FLEET,
                      "channel": f"Аккаунт #{r.get('account_id')}"})
    for r in vault or []:
        items.append({"at": r.get("msg_date"), "direction": r.get("direction") or "in",
                      "text": r.get("text") or "", "source": SRC_VAULT,
                      "channel": "Хранилище"})
    for r in bots or []:
        bu = r.get("bot_username")
        for turn in r.get("history") or []:
            role = turn.get("role")
            if role not in ("user", "assistant"):
                continue
            items.append({"at": turn.get("at") or r.get("last_at"),
                          "direction": "in" if role == "user" else "out",
                          "text": str(turn.get("content") or ""), "source": SRC_BOT,
                          "channel": f"Бот @{bu}" if bu else "Бот-продавец"})
    for r in relay or []:
        bu = r.get("bot_username")
        items.append({"at": r.get("created_at"), "direction": r.get("direction") or "in",
                      "text": r.get("text") or "", "source": SRC_RELAY,
                      "channel": f"Релей @{bu}" if bu else "Релей бота"})
    # Сортировка устойчивая: реплики бота без своего времени остаются в
    # исходном порядке диалога.
    items.sort(key=lambda m: _ts(m["at"]))
    return items[-max(1, int(limit)):]


# ── Выборка из базы ────────────────────────────────────────────────────────

async def _safe(pool, q, *a):
    """Источник, которого нет (таблица не создана), не роняет весь инбокс."""
    try:
        return [dict(r) for r in (await pool.fetch(q, *a) or [])]
    except Exception:
        log.debug("unified_inbox: источник недоступен: %s", q[:60], exc_info=True)
        return []


async def list_threads(pool, owner_id: int, *, only_waiting: bool = False,
                       limit: int = 100) -> dict:
    """Разговоры владельца из всех источников + счётчики для шапки."""
    uid = int(owner_id)
    fleet = await _safe(pool,
        """SELECT DISTINCT ON (peer_user_id, account_id)
                  peer_user_id, account_id, peer_name, peer_username,
                  msg_date AS last_at, text_enc,
                  COUNT(*) FILTER (WHERE NOT is_read)
                      OVER (PARTITION BY peer_user_id, account_id) AS unread,
                  COUNT(*) OVER (PARTITION BY peer_user_id, account_id) AS total
             FROM inbox_messages
            WHERE owner_id=$1
            ORDER BY peer_user_id, account_id, msg_date DESC""", uid)
    for r in fleet:
        r["last_text"] = _decrypt(r.pop("text_enc", None))
    vault = await _safe(pool,
        """SELECT DISTINCT ON (chat_id)
                  chat_id AS peer_user_id, peer_name, peer_username,
                  COALESCE(msg_date, created_at) AS last_at, text_enc,
                  direction AS last_direction,
                  COUNT(*) OVER (PARTITION BY chat_id) AS total
             FROM vault_messages
            WHERE owner_id=$1 AND chat_id > 0 AND NOT is_deleted
            ORDER BY chat_id, COALESCE(msg_date, created_at) DESC""", uid)
    for r in vault:
        r["last_text"] = _decrypt(r.pop("text_enc", None))
    bots = await _safe(pool,
        """SELECT d.customer_chat_id AS peer_user_id, d.bot_id, b.username AS bot_username,
                  d.last_at, d.history, d.handed_off, d.msg_count AS total,
                  bu.first_name AS peer_name, bu.username AS peer_username
             FROM bot_sales_dialogs d
             LEFT JOIN managed_bots b ON b.bot_id = d.bot_id
             LEFT JOIN bot_users bu ON bu.bot_id = d.bot_id AND bu.user_id = d.customer_chat_id
            WHERE d.owner_id=$1
            ORDER BY d.last_at DESC LIMIT 500""", uid)
    for r in bots:
        hist = _as_list(r.pop("history", None))
        last = hist[-1] if hist else {}
        r["last_text"] = str(last.get("content") or "")
        r["last_role"] = last.get("role")
    relay = await _safe(pool,
        """SELECT DISTINCT ON (s.id)
                  s.id AS session_id, s.user_id AS peer_user_id, s.bot_id,
                  b.username AS bot_username, s.first_name AS peer_name,
                  s.username AS peer_username, s.messages_count AS total,
                  m.created_at AS last_at, m.text AS last_text,
                  m.direction AS last_direction
             FROM relay_sessions s
             JOIN managed_bots b ON b.bot_id = s.bot_id
             JOIN relay_messages m ON m.session_id = s.id
            WHERE b.added_by=$1
            ORDER BY s.id, m.created_at DESC""", uid)
    pids = sorted({r["peer_user_id"] for r in fleet + vault + bots + relay
                   if r.get("peer_user_id")})
    contacts = {}
    if pids:
        rows = await _safe(pool,
            "SELECT telegram_user_id, id FROM unified_contacts "
            "WHERE owner_id=$1 AND telegram_user_id = ANY($2::bigint[])", uid, pids)
        contacts = {r["telegram_user_id"]: str(r["id"]) for r in rows}
    all_threads = merge_threads(fleet, vault, bots, contacts, limit=100_000,
                                relay=relay)
    threads = ([t for t in all_threads if t["waiting"]] if only_waiting
               else all_threads)[: max(1, int(limit))]
    return {
        "threads": [_iso(t) for t in threads],
        "total": len(all_threads),
        "waiting": sum(1 for t in all_threads if t["waiting"]),
        "unread": sum(t["unread"] for t in all_threads),
    }


async def get_thread(pool, owner_id: int, peer_user_id: int, limit: int = 200) -> dict:
    """Вся переписка с человеком из всех источников одной лентой."""
    uid, pid = int(owner_id), int(peer_user_id)
    fleet = await _safe(pool,
        """SELECT account_id, msg_date, text_enc, peer_name, peer_username
             FROM inbox_messages WHERE owner_id=$1 AND peer_user_id=$2
            ORDER BY msg_date DESC LIMIT 200""", uid, pid)
    vault = await _safe(pool,
        """SELECT direction, COALESCE(msg_date, created_at) AS msg_date, text_enc,
                  peer_name, peer_username
             FROM vault_messages WHERE owner_id=$1 AND chat_id=$2 AND NOT is_deleted
            ORDER BY COALESCE(msg_date, created_at) DESC LIMIT 200""", uid, pid)
    for r in fleet + vault:
        r["text"] = _decrypt(r.pop("text_enc", None))
    bots = await _safe(pool,
        """SELECT d.bot_id, b.username AS bot_username, d.history, d.last_at
             FROM bot_sales_dialogs d LEFT JOIN managed_bots b ON b.bot_id = d.bot_id
            WHERE d.owner_id=$1 AND d.customer_chat_id=$2""", uid, pid)
    for r in bots:
        r["history"] = _as_list(r.get("history"))
    relay = await _safe(pool,
        """SELECT m.direction, m.text, m.created_at, b.username AS bot_username
             FROM relay_messages m
             JOIN relay_sessions s ON s.id = m.session_id
             JOIN managed_bots b ON b.bot_id = s.bot_id
            WHERE b.added_by=$1 AND s.user_id=$2
            ORDER BY m.created_at DESC LIMIT 200""", uid, pid)
    name = username = ""
    for r in fleet + vault:
        name = name or (r.get("peer_name") or "")
        username = username or (r.get("peer_username") or "")
    cid = None
    row = await _safe(pool,
        "SELECT id, display_name, first_name, username FROM unified_contacts "
        "WHERE owner_id=$1 AND telegram_user_id=$2 LIMIT 1", uid, pid)
    if row:
        cid = str(row[0]["id"])
        name = name or row[0].get("display_name") or row[0].get("first_name") or ""
        username = username or row[0].get("username") or ""
    msgs = merge_messages(fleet, vault, bots, limit=limit, relay=relay)
    return {
        "peer_user_id": pid, "name": name, "username": username, "contact_id": cid,
        "messages": [{**m, "at": _iso_v(m["at"])} for m in msgs],
        # Каждый источник взят с потолком 200: достигнут хоть один — ранние
        # сообщения в ленту не попали, и экран обязан это сказать.
        "truncated": any(len(x) >= 200 for x in (fleet, vault, relay))
                     or len(msgs) >= int(limit),
    }


def _as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            import json
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except ValueError:
            return []
    return []


def _iso_v(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _iso(t: dict) -> dict:
    return {**t, "last_at": _iso_v(t.get("last_at"))}
