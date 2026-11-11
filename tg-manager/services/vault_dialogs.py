"""«Хранилище» как список диалогов Telegram: папки, непрочитанные, ждущие ответа.

Раньше Хранилище было архивом сообщений с одной эвристикой «ждёт ответа =
последнее сообщение входящее». На ней же стояло напоминание «N клиентов ждут
ответа», и оно приходило про людей, которым уже ответили (по телефону, с
другого аккаунта, лично), про тех, кто просто написал «спасибо», и про тех, кто
уже купил. Здесь одно место, где решается, ждёт ли диалог ответа, — его читают
и список в мини-аппе, и экран в боте, и напоминание организма. Число в
уведомлении и список под кнопкой «Открыть» обязаны совпадать.

Состояние, которое ставит владелец (прочитано, отвечено, купил, закреплён, в
архиве, без напоминаний), живёт в `vault_chat_state` (schema_v317).

Всё строго по owner_id; текст расшифровывается только для превью владельцу.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from services.token_vault import decrypt_token

log = logging.getLogger(__name__)

FOLDERS = ("all", "unread", "waiting", "deleted", "archived")

# ── Завершающие реплики ──────────────────────────────────────────────────────
# «Спасибо», «ок, договорились», «👍» после ответа — это конец разговора, а не
# вопрос. Последним сообщением в чате оно стоит у каждого довольного клиента, и
# считать такой чат «ждущим ответа» значит напоминать именно про тех, с кем всё
# уже решено.
_CLOSING_WORDS = frozenset("""
спасибо спасибочки спасиб спс пасиб пасибо благодарю благодарим большое огромное
вам тебе всё все ок окей окай ok okay okey хорошо хорошего понял поняла поняли
понятно ясно ясненько договорились отлично супер класс круто ага угу да добро
принял приняла приняли принято хор отл до встречи связи на будь здоров
спокойной ночи дня вечера доброй доброго всего thanks thank you thx ty
""".split())
_WORD_RE = re.compile(r"[a-zа-яё]+", re.IGNORECASE)


def is_closing_message(text: str | None, media_type: str | None = None) -> bool:
    """True, если входящее сообщение закрывает разговор и ответа не ждёт.

    Строго: в реплике только слова благодарности/согласия (до шести слов) или
    одни эмодзи. Вопросительный знак — всегда «ждёт». Стикер без текста —
    закрывающий (так обычно и отвечают «спасибо»), фото/голосовое — нет.
    """
    t = (text or "").strip()
    if not t:
        return media_type == "sticker"
    if "?" in t:
        return False
    words = _WORD_RE.findall(t.lower())
    if not words:
        # Одни эмодзи/знаки: «👍», «🙏», «❤️», «+».
        return len(t) <= 12
    if len(words) > 6:
        return False
    return all(w in _CLOSING_WORDS for w in words)


# ── Список диалогов ──────────────────────────────────────────────────────────

_DIALOGS_SQL = """
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


def _iso(ts):
    return ts.isoformat() if ts else None


def _peer_name(r) -> str:
    return (r["peer_name"]
            or (f"@{r['peer_username']}" if r["peer_username"] else str(r["chat_id"])))


def _preview(enc, media_type) -> str:
    from services.vault_service import _MEDIA_LABEL
    if enc:
        t = " ".join((decrypt_token(enc) or "").split())
        return (t[:79] + "…") if len(t) > 80 else t
    return _MEDIA_LABEL.get(media_type, "📎 Вложение") if media_type else ""


def waiting_reason(d: dict) -> str | None:
    """Почему диалог НЕ ждёт ответа (None — ждёт). Чистая функция над строкой
    списка: её же проверяют тесты, и по ней же решает напоминание."""
    if not d.get("last_in"):
        return "no_incoming"
    if d.get("live_direction") != "in":
        return "answered"
    if d.get("last_out") and d["last_out"] >= d["last_in"]:
        return "answered"
    if d.get("handled_at") and d["handled_at"] >= d["last_in"]:
        return "handled"
    if d.get("purchased"):
        return "purchased"
    if d.get("archived"):
        return "archived"
    if d.get("muted"):
        return "muted"
    if is_closing_message(d.get("_live_text"), d.get("live_media_type")):
        return "closing"
    return None


async def purchased_chats(pool, owner_id: int, chat_ids: list[int]) -> set[int]:
    """Чаты, где собеседник уже покупал: оплаченный заказ бота-продавца или
    «Купил» в воронке контакта. Каждый источник — отдельно и fail-open: старая
    схема без одной из таблиц не должна ронять список."""
    ids = [int(c) for c in chat_ids if c is not None]
    if not ids:
        return set()
    out: set[int] = set()
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT customer_chat_id AS cid FROM bot_sales_orders "
            "WHERE owner_id=$1 AND customer_chat_id = ANY($2::bigint[]) "
            "AND paid_at IS NOT NULL", owner_id, ids)
        out.update(int(r["cid"]) for r in rows if r["cid"] is not None)
    except Exception:
        log.debug("vault_dialogs: orders lookup failed owner=%s", owner_id)
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT uc.telegram_user_id AS cid FROM unified_contacts uc "
            "JOIN virtual_states vs ON vs.owner_id=uc.owner_id "
            "  AND vs.entity_type='user' AND vs.entity_id=uc.id::text "
            "  AND vs.state_key='funnel' AND vs.value='purchased' "
            "WHERE uc.owner_id=$1 AND uc.telegram_user_id = ANY($2::bigint[])",
            owner_id, ids)
        out.update(int(r["cid"]) for r in rows if r["cid"] is not None)
    except Exception:
        log.debug("vault_dialogs: funnel lookup failed owner=%s", owner_id)
    return out


async def _rows(pool, owner_id: int, chat_id: int | None = None) -> list[dict]:
    rows = await pool.fetch(_DIALOGS_SQL, owner_id, chat_id)
    out = []
    for r in rows:
        d = dict(r)
        d["_live_text"] = (decrypt_token(r["live_text_enc"])
                           if r["live_direction"] == "in" and r["live_text_enc"] else None)
        out.append(d)
    bought = await purchased_chats(
        pool, owner_id, [d["chat_id"] for d in out if not d.get("purchased_at")])
    for d in out:
        d["purchased"] = bool(d.get("purchased_at")) or d["chat_id"] in bought
    return out


def _ui(d: dict) -> dict:
    reason = waiting_reason(d)
    return {
        "chat_id": d["chat_id"],
        "peer_name": _peer_name(d),
        "peer_username": d["peer_username"],
        "total": int(d["total"] or 0),
        "deleted": int(d["deleted"] or 0),
        "media": int(d["media"] or 0),
        "unread": int(d["unread"] or 0),
        "first_date": _iso(d["first_date"]),
        "last_date": _iso(d["last_date"]),
        "last_preview": _preview(d["text_enc"], d["media_type"]),
        "last_direction": d["last_direction"],
        "last_deleted": bool(d["last_deleted"]),
        "pinned": bool(d["pinned"]),
        "archived": bool(d["archived"]),
        "muted": bool(d["muted"]),
        "purchased": bool(d["purchased"]),
        "handled": bool(d["handled_at"] and d["last_in"] and d["handled_at"] >= d["last_in"]),
        "note": d.get("note") or "",
        "awaiting_reply": reason is None,
        "waiting_since": _iso(d["last_in"]) if reason is None else None,
        # «Ответили, но последнее слово за клиентом — и это “спасибо”»: в списке
        # видно, почему чат не попал в ждущие.
        "not_waiting_reason": reason,
    }


def _matches(c: dict, q: str) -> bool:
    q = q.lower()
    return (q in (c["peer_name"] or "").lower()
            or q in (c["peer_username"] or "").lower()
            or q in (c["last_preview"] or "").lower()
            or q in (c["note"] or "").lower())


async def dialogs(pool, owner_id: int, *, folder: str = "all", q: str = "",
                  limit: int = 200, offset: int = 0) -> dict:
    """Список диалогов как в Telegram: закреплённые сверху, дальше по свежести.

    Возвращает {chats, counts, total} — counts по всем папкам сразу, чтобы
    вкладки показывали числа без лишних запросов.
    """
    folder = folder if folder in FOLDERS else "all"
    items = [_ui(d) for d in await _rows(pool, owner_id)]
    counts = {
        "all": sum(1 for c in items if not c["archived"]),
        "unread": sum(1 for c in items if c["unread"] and not c["archived"]),
        "waiting": sum(1 for c in items if c["awaiting_reply"]),
        "deleted": sum(1 for c in items if c["deleted"]),
        "archived": sum(1 for c in items if c["archived"]),
        "unread_messages": sum(c["unread"] for c in items
                               if not c["archived"] and not c["muted"]),
    }
    if folder == "archived":
        view = [c for c in items if c["archived"]]
    elif folder == "unread":
        view = [c for c in items if c["unread"] and not c["archived"]]
    elif folder == "waiting":
        view = [c for c in items if c["awaiting_reply"]]
    elif folder == "deleted":
        view = [c for c in items if c["deleted"]]
    else:
        view = [c for c in items if not c["archived"]]
    if q:
        view = [c for c in view if _matches(c, q.strip())]
    view.sort(key=lambda c: (c["last_date"] or ""), reverse=True)
    if folder == "waiting":
        # Ждущие — по давности ожидания: дольше всех ждёт тот, кто выше.
        view.sort(key=lambda c: c["waiting_since"] or "")
    else:
        view.sort(key=lambda c: not c["pinned"])
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    return {"chats": view[off:off + lim], "counts": counts, "total": len(view)}


async def waiting_chats(pool, owner_id: int, days: int = 7) -> list[dict]:
    """Кто ждёт ответа прямо сейчас — для напоминания организма.

    Окно в днях: про клиента, который написал месяц назад, «пока лид тёплый»
    уже неправда. Возвращает [{chat_id, peer_name, since}] — старшие первыми.
    """
    import datetime as _dt
    border = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=int(days))
    out = []
    for d in await _rows(pool, owner_id):
        last_in = d.get("last_in")
        if not last_in:
            continue
        if last_in.tzinfo is None:
            last_in = last_in.replace(tzinfo=_dt.timezone.utc)
        if last_in < border or waiting_reason(d) is not None:
            continue
        out.append({"chat_id": d["chat_id"], "peer_name": _peer_name(d),
                    "since": d["last_in"]})
    out.sort(key=lambda w: w["since"])
    return out


# ── Состояние диалога ────────────────────────────────────────────────────────

_FLAGS = ("pinned", "archived", "muted")


async def set_state(pool, owner_id: int, chat_id: int, **changes: Any) -> dict:
    """Поменять состояние диалога. Поддерживает:
    pinned/archived/muted: bool; read: True (прочитано сейчас);
    handled: bool (отвечено — входящие до сейчас не ждут ответа);
    purchased: bool; note: str.
    Возвращает итоговое состояние."""
    sets: list[str] = []
    args: list[Any] = [owner_id, int(chat_id)]
    for k in _FLAGS:
        if k in changes and changes[k] is not None:
            args.append(bool(changes[k]))
            sets.append(f"{k}=${len(args)}")
    if changes.get("read"):
        sets.append("read_at=now()")
    if "handled" in changes and changes["handled"] is not None:
        sets.append("handled_at=now()" if changes["handled"] else "handled_at=NULL")
    if "purchased" in changes and changes["purchased"] is not None:
        sets.append("purchased_at=COALESCE(purchased_at, now())"
                    if changes["purchased"] else "purchased_at=NULL")
    if "note" in changes and changes["note"] is not None:
        args.append(str(changes["note"])[:2000])
        sets.append(f"note=${len(args)}")
    if not sets:
        return await get_state(pool, owner_id, chat_id)
    # Строка создаётся первой отметкой, дальше — обычный UPDATE. Два простых
    # запроса вместо собранного на лету INSERT … ON CONFLICT с теми же полями.
    await pool.execute(
        "INSERT INTO vault_chat_state (owner_id, chat_id) VALUES ($1, $2) "
        "ON CONFLICT (owner_id, chat_id) DO NOTHING", owner_id, int(chat_id))
    await pool.execute(
        f"UPDATE vault_chat_state SET {', '.join(sets)}, updated_at=now() "
        "WHERE owner_id=$1 AND chat_id=$2", *args)
    if changes.get("purchased"):
        await _purchase_to_layer(pool, owner_id, int(chat_id))
    return await get_state(pool, owner_id, chat_id)


async def get_state(pool, owner_id: int, chat_id: int) -> dict:
    row = await pool.fetchrow(
        "SELECT pinned, archived, muted, read_at, handled_at, purchased_at, note "
        "FROM vault_chat_state WHERE owner_id=$1 AND chat_id=$2", owner_id, int(chat_id))
    if not row:
        return {"pinned": False, "archived": False, "muted": False, "read_at": None,
                "handled_at": None, "purchased_at": None, "note": ""}
    d = dict(row)
    for k in ("read_at", "handled_at", "purchased_at"):
        d[k] = _iso(d[k])
    d["note"] = d.get("note") or ""
    return d


async def _purchase_to_layer(pool, owner_id: int, chat_id: int) -> None:
    """«Купил» из Хранилища — тот же сигнал, что оплата: воронка контакта
    переходит в «Купил», отметка «ходит по кругу» снимается."""
    try:
        cid = await pool.fetchval(
            "SELECT id::text FROM unified_contacts WHERE owner_id=$1 "
            "AND telegram_user_id=$2 ORDER BY updated_at DESC NULLS LAST LIMIT 1",
            owner_id, chat_id)
        if not cid:
            return
        from services import virtual_layer
        await virtual_layer.mark_purchased(pool, owner_id, virtual_layer.USER, cid,
                                           source="vault")
    except Exception:
        log.debug("vault_dialogs: purchase→layer failed owner=%s", owner_id,
                  exc_info=True)


# ── Карточка диалога ─────────────────────────────────────────────────────────

async def dialog_info(pool, owner_id: int, chat_id: int) -> dict | None:
    """Сводка по одному диалогу: объёмы, даты, скорость ответа, связь с CRM.

    То, что конкуренты показывают по команде «информация о диалоге»
    (сообщений, дата создания), плюс то, чего у них нет: кто сколько пишет,
    как быстро вы отвечаете, что удалено, купил ли человек.
    """
    rows = await _rows(pool, owner_id, int(chat_id))
    if not rows:
        return None
    d = rows[0]
    base = _ui(d)
    agg = await pool.fetchrow(
        """SELECT COUNT(*) FILTER (WHERE direction='in')  AS incoming,
                  COUNT(*) FILTER (WHERE direction='out') AS outgoing,
                  COUNT(*) FILTER (WHERE is_edited)       AS edited,
                  COUNT(*) FILTER (WHERE is_deleted AND direction='in') AS deleted_by_peer,
                  COUNT(DISTINCT (msg_date AT TIME ZONE 'UTC')::date) AS active_days,
                  COUNT(*) FILTER (WHERE msg_date > now() - interval '7 days')  AS last_7d,
                  COUNT(*) FILTER (WHERE msg_date > now() - interval '30 days') AS last_30d
             FROM vault_messages WHERE owner_id=$1 AND chat_id=$2""",
        owner_id, int(chat_id))
    media_rows = await pool.fetch(
        "SELECT media_type, COUNT(*) AS c FROM vault_messages "
        "WHERE owner_id=$1 AND chat_id=$2 AND media_type IS NOT NULL "
        "GROUP BY media_type ORDER BY c DESC", owner_id, int(chat_id))
    seq = await pool.fetch(
        "SELECT direction, msg_date FROM vault_messages "
        "WHERE owner_id=$1 AND chat_id=$2 AND msg_date IS NOT NULL "
        "ORDER BY msg_date DESC LIMIT 2000", owner_id, int(chat_id))
    from services import vault_analytics
    from services.vault_service import _MEDIA_LABEL
    resp = vault_analytics.response_stats(
        [{"direction": r["direction"], "msg_date": r["msg_date"]} for r in reversed(seq)])
    info = dict(base)
    info.update({
        "incoming": int(agg["incoming"] or 0),
        "outgoing": int(agg["outgoing"] or 0),
        "edited": int(agg["edited"] or 0),
        "deleted_by_peer": int(agg["deleted_by_peer"] or 0),
        "active_days": int(agg["active_days"] or 0),
        "last_7d": int(agg["last_7d"] or 0),
        "last_30d": int(agg["last_30d"] or 0),
        "media_by_type": [{"type": r["media_type"],
                           "label": _MEDIA_LABEL.get(r["media_type"], "📎 Вложение"),
                           "count": int(r["c"])} for r in media_rows],
        "last_in": _iso(d["last_in"]),
        "last_out": _iso(d["last_out"]),
        **resp,
    })
    info["contact"] = await _contact_link(pool, owner_id, int(chat_id))
    return info


async def _contact_link(pool, owner_id: int, chat_id: int) -> dict | None:
    """Карточка в «Контактах» и где человек в воронке — fail-open."""
    try:
        row = await pool.fetchrow(
            "SELECT uc.id::text AS id, vs.value AS funnel FROM unified_contacts uc "
            "LEFT JOIN virtual_states vs ON vs.owner_id=uc.owner_id "
            "  AND vs.entity_type='user' AND vs.entity_id=uc.id::text "
            "  AND vs.state_key='funnel' "
            "WHERE uc.owner_id=$1 AND uc.telegram_user_id=$2 "
            "ORDER BY uc.updated_at DESC NULLS LAST LIMIT 1", owner_id, chat_id)
    except Exception:
        return None
    if not row:
        return None
    label = None
    try:
        from services.virtual_layer import VALUE_LABEL
        label = VALUE_LABEL.get(row["funnel"], row["funnel"]) if row["funnel"] else None
    except Exception:
        pass
    out = {"id": row["id"], "funnel": row["funnel"], "funnel_label": label,
           "orders_paid": 0, "paid_cents": 0}
    try:
        o = await pool.fetchrow(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total_cents),0) AS s FROM bot_sales_orders "
            "WHERE owner_id=$1 AND customer_chat_id=$2 AND paid_at IS NOT NULL",
            owner_id, chat_id)
        out["orders_paid"] = int(o["n"] or 0)
        out["paid_cents"] = int(o["s"] or 0)
    except Exception:
        pass
    return out


def info_text(info: dict) -> str:
    """Карточка диалога для бота (HTML). Даты — в UTC, как и у конкурентов,
    с явной пометкой: часового пояса владельца бот не знает."""
    import html

    def esc(s):
        return html.escape(str(s or ""))

    def dt(s):
        if not s:
            return "—"
        try:
            import datetime as _dt
            v = _dt.datetime.fromisoformat(s)
            return v.astimezone(_dt.timezone.utc).strftime("%d.%m.%Y, %H:%M")
        except Exception:
            return esc(s)

    def dur(mins):
        if mins is None:
            return "—"
        if mins < 1:
            return "меньше минуты"
        if mins < 60:
            return f"{round(mins)} мин"
        if mins < 60 * 24:
            return f"{mins / 60:.1f} ч".replace(".", ",")
        return f"{mins / 1440:.1f} дн".replace(".", ",")

    who = esc(info["peer_name"])
    if info.get("peer_username"):
        who += f" (@{esc(info['peer_username'])})"
    lines = [f"💬 <b>Диалог с {who}</b>", ""]
    status = []
    if info.get("awaiting_reply"):
        status.append("⏳ ждёт ответа")
    if info.get("unread"):
        status.append(f"🔵 непрочитанных: {info['unread']}")
    if info.get("purchased"):
        status.append("💰 купил")
    if info.get("pinned"):
        status.append("📌 закреплён")
    if info.get("archived"):
        status.append("🗄 в архиве")
    if info.get("muted"):
        status.append("🔕 без напоминаний")
    if status:
        lines += [" · ".join(status), ""]
    lines += [
        f"Сообщений в диалоге: <b>{info['total']}</b> "
        f"(от собеседника {info['incoming']}, ваших {info['outgoing']})",
        f"Начало диалога (UTC): <b>{dt(info.get('first_date'))}</b>",
        f"Последнее сообщение (UTC): <b>{dt(info.get('last_date'))}</b>",
        f"Активных дней: <b>{info['active_days']}</b> · за 7 дн.: {info['last_7d']} "
        f"· за 30 дн.: {info['last_30d']}",
        "",
        f"⚡ Ваш ответ, медиана: <b>{dur(info.get('owner_reply_median_min'))}</b>",
        f"↩️ Ответ собеседника, медиана: <b>{dur(info.get('peer_reply_median_min'))}</b>",
    ]
    extra = []
    if info.get("deleted_by_peer"):
        extra.append(f"🗑 удалено собеседником: {info['deleted_by_peer']}")
    if info.get("edited"):
        extra.append(f"✏️ изменено: {info['edited']}")
    if info.get("media_by_type"):
        extra.append("📎 " + ", ".join(f"{m['label']} {m['count']}"
                                       for m in info["media_by_type"][:4]))
    if extra:
        lines += [""] + extra
    c = info.get("contact")
    if c:
        bits = []
        if c.get("funnel_label"):
            bits.append(f"воронка: {esc(c['funnel_label'])}")
        if c.get("orders_paid"):
            bits.append(f"оплаченных заказов: {c['orders_paid']}")
        if bits:
            lines += ["", "👤 " + " · ".join(bits)]
    if info.get("note"):
        lines += ["", f"📝 {esc(info['note'][:300])}"]
    return "\n".join(lines)
