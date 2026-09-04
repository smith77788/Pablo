"""«Хранилище» (Echo Vault) — движок архива сообщений через Telegram Business.

Пользователь подключает бота в настройках бизнес-аккаунта; бот получает
business-апдейты и зеркалит переписку сюда. Архив переживает удаление чата у
пользователя: на deleted_business_messages мы лишь ставим флаг is_deleted, а
контент (уже сохранённый) остаётся.

Приватность (жёсткие гейты): текст/подпись шифруются at-rest (token_vault,
AES-256-GCM); чтение строго по owner_id; сырой контент и file_id не логируем.
Медиа хранится ССЫЛКОЙ (file_id) + метаданные — без скачивания байтов.

Хелперы разбора апдейта (media_of/direction_of/peer_of/text_of) — чистые и
duck-typed: тестируются без aiogram (в тест-среде его нет), принимают любой
объект с нужными атрибутами.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from services.token_vault import encrypt_token, decrypt_token

log = logging.getLogger(__name__)

# Порядок важен: подпись/тип берём по первому совпавшему медиа-атрибуту.
_MEDIA_ATTRS = (
    "photo", "video", "animation", "video_note", "voice", "audio",
    "document", "sticker",
)


# ── Чистые хелперы разбора сообщения ─────────────────────────────────────────

def text_of(message: Any) -> str | None:
    """Текст или подпись сообщения (что есть). None → нет текстовой части."""
    return getattr(message, "text", None) or getattr(message, "caption", None) or None


def direction_of(message: Any, owner_id: int) -> str:
    """'out' если сообщение отправил владелец бизнес-аккаунта, иначе 'in'."""
    frm = getattr(message, "from_user", None)
    if frm is not None and getattr(frm, "id", None) == owner_id:
        return "out"
    return "in"


def peer_of(message: Any) -> dict[str, Any]:
    """Собеседник = chat сообщения (в ЛС chat.id = user_id собеседника)."""
    chat = getattr(message, "chat", None)
    if chat is None:
        return {"peer_user_id": None, "peer_name": None, "peer_username": None}
    name = (getattr(chat, "full_name", None)
            or " ".join(x for x in (getattr(chat, "first_name", None),
                                    getattr(chat, "last_name", None)) if x)
            or getattr(chat, "title", None)
            or None)
    return {
        "peer_user_id": getattr(chat, "id", None),
        "peer_name": name,
        "peer_username": getattr(chat, "username", None),
    }


def media_of(message: Any) -> dict[str, Any]:
    """Тип и метаданные медиа (file_id + размер/mime/имя). Пусто → текст без медиа.

    Для photo Telegram отдаёт список размеров — берём самый крупный (последний).
    """
    for attr in _MEDIA_ATTRS:
        m = getattr(message, attr, None)
        if not m:
            continue
        obj = m[-1] if attr == "photo" and isinstance(m, (list, tuple)) and m else m
        return {
            "media_type": attr,
            "media_file_id": getattr(obj, "file_id", None),
            "media_unique_id": getattr(obj, "file_unique_id", None),
            "media_size": getattr(obj, "file_size", None),
            "media_mime": getattr(obj, "mime_type", None),
            "media_name": getattr(obj, "file_name", None),
        }
    return {"media_type": None, "media_file_id": None, "media_unique_id": None,
            "media_size": None, "media_mime": None, "media_name": None}


def _msg_date(message: Any):
    """datetime сообщения (edit_date приоритетнее, если есть)."""
    return getattr(message, "edit_date", None) or getattr(message, "date", None)


# ── Подключения ──────────────────────────────────────────────────────────────

async def upsert_connection(pool, connection_id: str, owner_id: int,
                            user_chat_id: int | None, can_reply: bool,
                            is_enabled: bool, rights: dict | None) -> None:
    await pool.execute(
        """INSERT INTO business_connections
               (connection_id, owner_id, user_chat_id, can_reply, is_enabled, rights, updated_at)
           VALUES ($1,$2,$3,$4,$5,$6::jsonb, now())
           ON CONFLICT (connection_id) DO UPDATE SET
               owner_id=EXCLUDED.owner_id, user_chat_id=EXCLUDED.user_chat_id,
               can_reply=EXCLUDED.can_reply, is_enabled=EXCLUDED.is_enabled,
               rights=EXCLUDED.rights, updated_at=now()""",
        connection_id, owner_id, user_chat_id, bool(can_reply), bool(is_enabled),
        json.dumps(rights or {}),
    )


async def get_connection(pool, connection_id: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT connection_id, owner_id, user_chat_id, can_reply, is_enabled "
        "FROM business_connections WHERE connection_id=$1", connection_id)
    return dict(row) if row else None


async def active_connection_for_owner(pool, owner_id: int) -> dict | None:
    """Активное подключение владельца — для ответа из хранилища (нужен conn_id)."""
    row = await pool.fetchrow(
        "SELECT connection_id, owner_id, can_reply, is_enabled "
        "FROM business_connections WHERE owner_id=$1 AND is_enabled=TRUE "
        "ORDER BY updated_at DESC LIMIT 1", owner_id)
    return dict(row) if row else None


async def diagnostics(pool, owner_id: int) -> dict:
    """Почему хранилище «зависло»: есть ли подключение, включено ли, когда
    последний раз что-то приходило. Отвечает на «неделю назад перестало писать».

    health:
      never       — подключения не было вовсе (нужно подключить бота в Business);
      disabled    — подключение есть, но выключено (бот отключён / истёк Premium);
      stale       — включено, но давно (>2 дней) ничего не приходит (тихо отвалилось);
      ok          — включено и есть свежая активность.
    """
    conn = await pool.fetchrow(
        "SELECT is_enabled, updated_at FROM business_connections "
        "WHERE owner_id=$1 ORDER BY updated_at DESC LIMIT 1", owner_id)
    row = await pool.fetchrow(
        "SELECT MAX(msg_date) AS last_at, COUNT(*) AS total "
        "FROM vault_messages WHERE owner_id=$1", owner_id)
    last_at = row["last_at"] if row else None
    total = int(row["total"]) if row and row["total"] is not None else 0

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    def _age_days(ts):
        if not ts:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return max(0, int((now - ts).total_seconds() // 86400))

    stale_days = _age_days(last_at)
    if conn is None:
        health = "never"
    elif not conn["is_enabled"]:
        health = "disabled"
    elif stale_days is not None and stale_days >= 2:
        health = "stale"
    elif stale_days is None and total == 0:
        # включено, но НИ ОДНОГО сообщения не пришло — считаем несвежим (свежее
        # подключение без трафика тоже сюда, это ок как сигнал «проверь»).
        health = "stale"
    else:
        health = "ok"

    return {
        "health": health,
        "is_enabled": bool(conn["is_enabled"]) if conn else False,
        "connection_updated_at": conn["updated_at"].isoformat()
            if conn and conn["updated_at"] else None,
        "last_archived_at": last_at.isoformat() if last_at else None,
        "stale_days": stale_days,
        "total_messages": total,
    }


# ── Архивация сообщения ──────────────────────────────────────────────────────

async def archive_message(pool, message: Any, owner_id: int,
                          connection_id: str | None) -> None:
    """Сохранить одно business-сообщение. Идемпотентно по (owner, chat, msg_id).

    Повторный апдейт того же msg_id (напр. повторная доставка) обновляет строку,
    а не плодит дубли; текст шифруется.
    """
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    msg_id = getattr(message, "message_id", None)
    if chat_id is None or msg_id is None:
        return
    peer = peer_of(message)
    media = media_of(message)
    text = text_of(message)
    text_enc = encrypt_token(text) if text else None
    await pool.execute(
        """INSERT INTO vault_messages
               (owner_id, connection_id, chat_id, msg_id, direction, peer_user_id,
                peer_name, peer_username, text_enc, media_type, media_file_id,
                media_unique_id, media_size, media_mime, media_name, msg_date)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
           ON CONFLICT (owner_id, chat_id, msg_id) DO UPDATE SET
               text_enc=EXCLUDED.text_enc, media_type=EXCLUDED.media_type,
               media_file_id=EXCLUDED.media_file_id, peer_name=EXCLUDED.peer_name,
               peer_username=EXCLUDED.peer_username""",
        owner_id, connection_id, chat_id, msg_id, direction_of(message, owner_id),
        peer["peer_user_id"], peer["peer_name"], peer["peer_username"], text_enc,
        media["media_type"], media["media_file_id"], media["media_unique_id"],
        media["media_size"], media["media_mime"], media["media_name"], _msg_date(message),
    )


async def record_edit(pool, message: Any, owner_id: int) -> dict:
    """Правка сообщения: старую версию текста складываем в edit_history, ставим
    новый текст и is_edited. Так видно и что было, и что стало.

    Возвращает {changed, direction, peer_name, old, new} — чтобы «ловец» мог
    прислать «было → стало» (только для входящих правок собеседника)."""
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    msg_id = getattr(message, "message_id", None)
    if chat_id is None or msg_id is None:
        return {"changed": False}
    row = await pool.fetchrow(
        "SELECT text_enc, edit_history, direction, peer_name FROM vault_messages "
        "WHERE owner_id=$1 AND chat_id=$2 AND msg_id=$3", owner_id, chat_id, msg_id)
    new_text = text_of(message)
    if not row:
        # Правка сообщения, которого нет в архиве (пришло до подключения) — просто
        # сохраняем как новое, чтобы не потерять текущий текст.
        await archive_message(pool, message, owner_id, getattr(message, "business_connection_id", None))
        await pool.execute(
            "UPDATE vault_messages SET is_edited=TRUE, edited_at=now() "
            "WHERE owner_id=$1 AND chat_id=$2 AND msg_id=$3", owner_id, chat_id, msg_id)
        return {"changed": False}
    old_text = decrypt_token(row["text_enc"]) if row["text_enc"] else ""
    hist = _load_json(row["edit_history"]) or []
    if row["text_enc"]:
        hist.append(row["text_enc"])          # прошлые версии — тоже зашифрованы
    await pool.execute(
        "UPDATE vault_messages SET text_enc=$4, is_edited=TRUE, edited_at=now(), "
        "edit_history=$5::jsonb WHERE owner_id=$1 AND chat_id=$2 AND msg_id=$3",
        owner_id, chat_id, msg_id,
        encrypt_token(new_text) if new_text else None, json.dumps(hist[-20:]),
    )
    return {
        "changed": (old_text or "") != (new_text or ""),
        "direction": row["direction"],
        "peer_name": row["peer_name"],
        "old": old_text or "",
        "new": new_text or "",
    }


async def mark_deleted(pool, owner_id: int, chat_id: int, msg_ids: list[int]) -> int:
    """Пометить удалённые у пользователя сообщения. КОНТЕНТ НЕ ТРОГАЕМ — в этом
    весь смысл хранилища. Возвращает, сколько строк помечено."""
    if not msg_ids:
        return 0
    res = await pool.execute(
        "UPDATE vault_messages SET is_deleted=TRUE, deleted_at=now() "
        "WHERE owner_id=$1 AND chat_id=$2 AND msg_id=ANY($3::bigint[]) AND is_deleted=FALSE",
        owner_id, chat_id, [int(m) for m in msg_ids])
    try:
        return int(str(res).split()[-1])       # 'UPDATE N'
    except (ValueError, IndexError):
        return 0


async def deleted_incoming_rows(pool, owner_id: int, chat_id: int,
                                msg_ids: list[int]) -> list[dict]:
    """Расшифрованные ВХОДЯЩИЕ сообщения из указанных удалённых — для уведомления
    «ловца» (owner удалил своё — не сигнал; собеседник удалил — сигнал)."""
    if not msg_ids:
        return []
    rows = await pool.fetch(
        "SELECT msg_id, text_enc, media_type, peer_name FROM vault_messages "
        "WHERE owner_id=$1 AND chat_id=$2 AND msg_id=ANY($3::bigint[]) AND direction='in'",
        owner_id, chat_id, [int(m) for m in msg_ids])
    out = []
    for r in rows:
        out.append({
            "msg_id": r["msg_id"],
            "peer_name": r["peer_name"],
            "text": decrypt_token(r["text_enc"]) if r["text_enc"] else "",
            "media_label": _MEDIA_LABEL.get(r["media_type"], "📎 Вложение") if r["media_type"] else "",
        })
    return out


# ── Настройки уведомлений «ловца» ────────────────────────────────────────────

async def get_notify_prefs(pool, owner_id: int) -> dict:
    row = await pool.fetchrow(
        "SELECT user_chat_id, notify_deleted, notify_edited FROM business_connections "
        "WHERE owner_id=$1 AND is_enabled=TRUE ORDER BY updated_at DESC LIMIT 1", owner_id)
    if not row:
        return {"user_chat_id": None, "notify_deleted": False, "notify_edited": False}
    return {"user_chat_id": row["user_chat_id"],
            "notify_deleted": bool(row["notify_deleted"]),
            "notify_edited": bool(row["notify_edited"])}


async def set_notify_prefs(pool, owner_id: int, notify_deleted: bool | None = None,
                           notify_edited: bool | None = None) -> None:
    sets, args, i = [], [owner_id], 2
    if notify_deleted is not None:
        sets.append(f"notify_deleted=${i}"); args.append(bool(notify_deleted)); i += 1
    if notify_edited is not None:
        sets.append(f"notify_edited=${i}"); args.append(bool(notify_edited)); i += 1
    if not sets:
        return
    await pool.execute(
        f"UPDATE business_connections SET {', '.join(sets)}, updated_at=now() WHERE owner_id=$1",
        *args)


# ── Лента «Недавно удалённое / изменённое» (кросс-чат) ───────────────────────

async def recent_activity(pool, owner_id: int, kind: str = "deleted",
                          limit: int = 100) -> list[dict]:
    """Последние удалённые (kind='deleted') или изменённые (kind='edited') сообщения
    по всем чатам — экран «ловца»."""
    if kind == "edited":
        cond, order = "is_edited=TRUE", "edited_at DESC"
    else:
        cond, order = "is_deleted=TRUE", "deleted_at DESC"
    rows = await pool.fetch(
        f"""SELECT chat_id, msg_id, direction, text_enc, edit_history, media_type,
                   peer_name, peer_username, msg_date, deleted_at, edited_at,
                   is_deleted, is_edited
            FROM vault_messages WHERE owner_id=$1 AND {cond}
            ORDER BY {order} NULLS LAST LIMIT $2""",
        owner_id, min(int(limit), 500))
    out = []
    for r in rows:
        d = _ui_message(r)
        d["chat_id"] = r["chat_id"]
        d["peer_name"] = r["peer_name"] or (f"@{r['peer_username']}" if r["peer_username"] else str(r["chat_id"]))
        d["when"] = (r["deleted_at"] if kind != "edited" else r["edited_at"])
        d["when"] = d["when"].isoformat() if d["when"] else None
        if kind == "edited":
            hist = _load_json(r["edit_history"]) or []
            d["was"] = decrypt_token(hist[-1]) if hist else ""   # прошлая версия
        out.append(d)
    return out


# ── Чтение архива (для мини-аппа), строго owner-scoped ───────────────────────

async def list_chats(pool, owner_id: int, limit: int = 100) -> list[dict]:
    """Список чатов в хранилище с последним сообщением и счётчиками.

    ОДИН запрос: агрегаты + последнее сообщение через DISTINCT ON (раньше был
    N+1 — отдельный запрос за последним сообщением на КАЖДЫЙ чат)."""
    rows = await pool.fetch(
        """WITH agg AS (
               SELECT chat_id,
                      MAX(peer_name)     AS peer_name,
                      MAX(peer_username) AS peer_username,
                      COUNT(*)           AS total,
                      COUNT(*) FILTER (WHERE is_deleted) AS deleted,
                      MAX(msg_date)      AS last_date
               FROM vault_messages WHERE owner_id=$1 GROUP BY chat_id
           ),
           last AS (
               SELECT DISTINCT ON (chat_id) chat_id, text_enc, media_type, is_deleted,
                      direction
               FROM vault_messages WHERE owner_id=$1
               ORDER BY chat_id, msg_date DESC NULLS LAST, msg_id DESC
           )
           SELECT a.chat_id, a.peer_name, a.peer_username, a.total, a.deleted, a.last_date,
                  l.text_enc, l.media_type, l.direction AS last_direction
           FROM agg a LEFT JOIN last l USING (chat_id)
           ORDER BY a.last_date DESC NULLS LAST LIMIT $2""",
        owner_id, limit)
    out = []
    for r in rows:
        out.append({
            "chat_id": r["chat_id"],
            "peer_name": r["peer_name"] or (f"@{r['peer_username']}" if r["peer_username"] else str(r["chat_id"])),
            "peer_username": r["peer_username"],
            "total": int(r["total"] or 0),
            "deleted": int(r["deleted"] or 0),
            "last_date": r["last_date"].isoformat() if r["last_date"] else None,
            "last_preview": _preview(r),
            "last_direction": r["last_direction"],
            # Ждёт ответа: последнее сообщение — от собеседника (входящее).
            "awaiting_reply": (r["last_direction"] == "in"),
        })
    return out


async def list_messages(pool, owner_id: int, chat_id: int,
                        limit: int = 200, offset: int = 0,
                        filters: dict | None = None,
                        newest_first: bool = False) -> dict:
    """Сообщения чата с опциональными фильтрами: media_only / deleted_only /
    edited_only / direction ('in'|'out'). Всё скоупится по owner_id.

    `newest_first=True` — брать сообщения С КОНЦА переписки (как любой
    мессенджер), а `offset` тогда означает «сколько последних пропустить»,
    то есть шаг назад по истории. Результат ВСЕГДА возвращается в
    хронологическом порядке — переворот делается здесь, чтобы вызывающий не
    думал о направлении выборки.

    Раньше выборка всегда шла с НАЧАЛА архива по возрастанию: в переписке из
    500 сообщений экран показывал 200 самых СТАРЫХ, прокручивал их в конец —
    и создавал полное впечатление, что это и есть весь чат. Свежие сообщения
    были недостижимы.

    Возвращает {"messages": [...], "has_more": bool}.
    """
    f = filters or {}
    cond = ["owner_id=$1", "chat_id=$2"]
    if f.get("media_only"):
        cond.append("media_type IS NOT NULL")
    if f.get("deleted_only"):
        cond.append("is_deleted=TRUE")
    if f.get("edited_only"):
        cond.append("is_edited=TRUE")
    args = [owner_id, chat_id]
    if f.get("direction") in ("in", "out"):
        args.append(f["direction"]); cond.append(f"direction=${len(args)}")
    lim = min(max(int(limit), 1), 1000)
    # Берём на одну строку больше запрошенного — так узнаём про «есть ещё»
    # без второго COUNT-запроса по чату.
    args.append(lim + 1); lim_i = len(args)
    args.append(max(int(offset), 0)); off_i = len(args)
    order = ("msg_date DESC NULLS LAST, msg_id DESC" if newest_first
             else "msg_date ASC NULLS FIRST, msg_id ASC")
    rows = await pool.fetch(
        f"""SELECT msg_id, direction, text_enc, media_type, media_file_id, media_name,
                   media_size, msg_date, is_deleted, is_edited
            FROM vault_messages WHERE {' AND '.join(cond)}
            ORDER BY {order} LIMIT ${lim_i} OFFSET ${off_i}""",
        *args)
    has_more = len(rows) > lim
    rows = rows[:lim]
    out = [_ui_message(r) for r in rows]
    if newest_first:
        out.reverse()          # наружу — всегда хронологический порядок
    return {"messages": out, "has_more": has_more}


# Потолок экспорта. Раньше выборка была неограниченной: «Экспорт всего архива»
# на большой переписке тянул В ПАМЯТЬ каждую строку, расшифровывал её и собирал
# из этого одну HTML-страницу — предсказуемый способ уронить процесс. При
# упирании в потолок берём САМОЕ СВЕЖЕЕ (оно нужнее) и честно это помечаем.
_EXPORT_LIMIT = 50000


async def export_data(pool, owner_id: int, chat_id: int | None = None,
                      limit: int = _EXPORT_LIMIT) -> dict:
    """Данные для экспорта архива (весь или один чат): расшифрованные сообщения,
    сгруппированные по чатам. Owner-scoped.

    Возвращает {..., "truncated": bool, "exported": int, "total": int}.
    """
    if chat_id is not None:
        total = int(await pool.fetchval(
            "SELECT COUNT(*) FROM vault_messages WHERE owner_id=$1 AND chat_id=$2",
            owner_id, chat_id) or 0)
        rows = await pool.fetch(
            """SELECT chat_id, peer_name, peer_username, msg_id, direction, text_enc,
                      media_type, media_name, msg_date, is_deleted, is_edited
               FROM vault_messages WHERE owner_id=$1 AND chat_id=$2
               ORDER BY msg_date DESC NULLS LAST, msg_id DESC LIMIT $3""",
            owner_id, chat_id, limit + 1)
    else:
        total = int(await pool.fetchval(
            "SELECT COUNT(*) FROM vault_messages WHERE owner_id=$1", owner_id) or 0)
        rows = await pool.fetch(
            """SELECT chat_id, peer_name, peer_username, msg_id, direction, text_enc,
                      media_type, media_name, msg_date, is_deleted, is_edited
               FROM vault_messages WHERE owner_id=$1
               ORDER BY msg_date DESC NULLS LAST, msg_id DESC LIMIT $2""",
            owner_id, limit + 1)
    truncated = len(rows) > limit
    rows = list(rows[:limit])
    # Выбирали с конца (чтобы при усечении досталось самое свежее), но в файле
    # переписка должна читаться сверху вниз — возвращаем хронологический порядок.
    rows.reverse()
    chats: dict = {}
    for r in rows:
        c = chats.setdefault(r["chat_id"], {
            "chat_id": r["chat_id"],
            "peer_name": r["peer_name"] or (f"@{r['peer_username']}" if r["peer_username"] else str(r["chat_id"])),
            "messages": [],
        })
        c["messages"].append({
            "msg_id": r["msg_id"], "direction": r["direction"],
            "text": decrypt_token(r["text_enc"]) if r["text_enc"] else "",
            "media": _MEDIA_LABEL.get(r["media_type"], "") if r["media_type"] else "",
            "media_name": r["media_name"],
            "date": r["msg_date"].isoformat() if r["msg_date"] else None,
            "deleted": bool(r["is_deleted"]), "edited": bool(r["is_edited"]),
        })
    return {
        "owner_id": owner_id,
        "chats": list(chats.values()),
        "exported": len(rows),
        "total": total,
        # Молча урезанный экспорт хуже отсутствующего: человек будет думать,
        # что сохранил всю переписку.
        "truncated": truncated,
    }


# Поиск идёт страницами: текст зашифрован, ILIKE по нему невозможен, поэтому
# строки расшифровываются в приложении. Раньше бралось ЖЁСТКОЕ окно из 4000
# последних сообщений — молча: письмо годичной давности «не находилось», и
# понять, что поиск до него просто не дошёл, было нельзя.
_SEARCH_PAGE = 2000
_SEARCH_MAX_SCAN = 40000   # потолок на один запрос, чтобы не жечь CPU впустую


async def search_messages(pool, owner_id: int, query: str, limit: int = 100,
                          max_scan: int = _SEARCH_MAX_SCAN) -> dict:
    """Поиск по архиву. Возвращает {results, scanned, total, complete}.

    Совпадением считается вхождение в текст сообщения ЛИБО в имя/ник
    собеседника: искать переписку по имени клиента — самый частый способ, а
    раньше работал только текст.

    `complete=False` означает, что архив просмотрен не до конца (упёрлись в
    потолок) — вызывающий обязан сказать об этом пользователю, иначе «ничего не
    найдено» будет неправдой.
    """
    q = (query or "").strip().lower()
    if not q:
        return {"results": [], "scanned": 0, "total": 0, "complete": True}

    total = int(await pool.fetchval(
        "SELECT COUNT(*) FROM vault_messages WHERE owner_id=$1", owner_id) or 0)

    out: list[dict] = []
    scanned = 0
    offset = 0
    while scanned < max_scan and len(out) < limit:
        rows = await pool.fetch(
            """SELECT chat_id, msg_id, direction, text_enc, media_type, media_name,
                      peer_name, peer_username, msg_date, is_deleted, is_edited
                 FROM vault_messages WHERE owner_id=$1
             ORDER BY msg_date DESC NULLS LAST, msg_id DESC
                LIMIT $2 OFFSET $3""",
            owner_id, _SEARCH_PAGE, offset)
        if not rows:
            break
        for r in rows:
            scanned += 1
            text = decrypt_token(r["text_enc"]) if r["text_enc"] else ""
            peer = " ".join(filter(None, (r["peer_name"], r["peer_username"])))
            if q in (text or "").lower() or q in peer.lower():
                d = _ui_message(r)
                d["chat_id"] = r["chat_id"]
                d["peer_name"] = r["peer_name"] or (
                    f"@{r['peer_username']}" if r["peer_username"] else str(r["chat_id"]))
                out.append(d)
                if len(out) >= limit:
                    break
        if len(rows) < _SEARCH_PAGE:
            break          # архив кончился
        offset += _SEARCH_PAGE

    return {
        "results": out,
        "scanned": scanned,
        "total": total,
        # Просмотрели всё либо набрали лимит совпадений — в обоих случаях
        # «ничего не найдено дальше» не вводит в заблуждение.
        "complete": scanned >= total or len(out) >= limit,
    }


# ── Медиа из архива ───────────────────────────────────────────────────────────

# Bot API отдаёт файлы не больше 20 МБ — больше скачать физически нельзя.
_MEDIA_DOWNLOAD_LIMIT = 20 * 1024 * 1024

_MEDIA_MIME = {
    "photo": ("image/jpeg", ".jpg"),
    "video": ("video/mp4", ".mp4"),
    "animation": ("video/mp4", ".mp4"),
    "video_note": ("video/mp4", ".mp4"),
    "voice": ("audio/ogg", ".ogg"),
    "audio": ("audio/mpeg", ".mp3"),
    "sticker": ("image/webp", ".webp"),
    "document": ("application/octet-stream", ""),
}


async def fetch_media(pool, bot, owner_id: int, chat_id: int, msg_id: int) -> dict:
    """Достать файл сообщения из архива.

    Смысл хранилища — сохранить то, что собеседник потом удалил. Но media_file_id
    писался в базу и НИКУДА не отдавался: в архиве была видна подпись «📷 Фото»,
    а самого фото не существовало ни в одном экране. Для удалённых сообщений это
    ломало главное обещание модуля.

    Возвращает {ok, data, filename, mime} либо {ok: False, error}.
    Скоуп владельца обязателен: chat_id приходит из URL.
    """
    row = await pool.fetchrow(
        """SELECT media_file_id, media_type, media_name, media_size
             FROM vault_messages
            WHERE owner_id=$1 AND chat_id=$2 AND msg_id=$3""",
        owner_id, chat_id, msg_id)
    if not row:
        return {"ok": False, "error": "Сообщение не найдено", "status": 404}
    file_id = row["media_file_id"]
    if not file_id:
        return {"ok": False, "error": "В сообщении нет вложения", "status": 404}
    size = int(row["media_size"] or 0)
    if size > _MEDIA_DOWNLOAD_LIMIT:
        return {"ok": False, "status": 413,
                "error": f"Файл {size // 1024 // 1024} МБ — Telegram отдаёт ботам не больше 20 МБ"}

    mime, ext = _MEDIA_MIME.get(row["media_type"] or "", ("application/octet-stream", ""))
    name = row["media_name"] or f"vault_{chat_id}_{msg_id}{ext}"
    try:
        f = await bot.get_file(file_id)
        buf = await bot.download_file(f.file_path)
    except Exception as exc:
        # Файл мог протухнуть на стороне Telegram (особенно у давно удалённых
        # сообщений). Это не сбой сервера — объясняем причину, а не 500.
        log.info("vault fetch_media owner=%s chat=%s msg=%s: %s",
                 owner_id, chat_id, msg_id, exc)
        return {"ok": False, "status": 410,
                "error": "Telegram больше не отдаёт этот файл (мог быть удалён на его стороне)"}
    data = buf.read() if hasattr(buf, "read") else bytes(buf or b"")
    if not data:
        return {"ok": False, "status": 410, "error": "Пустой файл"}
    return {"ok": True, "data": data, "filename": name, "mime": mime}


# ── Ответ из хранилища (от имени пользователя) ───────────────────────────────

async def send_reply(pool, bot, owner_id: int, chat_id: int, text: str) -> dict:
    """Отправить сообщение собеседнику ОТ ИМЕНИ пользователя через business-
    соединение (нужно право can_reply). Возвращает {ok, error?}."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Пустое сообщение"}
    conn = await active_connection_for_owner(pool, owner_id)
    if not conn:
        return {"ok": False, "error": "Нет активного бизнес-подключения — переподключите бота."}
    if not conn.get("can_reply"):
        return {"ok": False, "error": "У бота нет права отвечать. Включите его в настройках бизнес-аккаунта."}
    try:
        sent = await bot.send_message(chat_id, text[:4096],
                                      business_connection_id=conn["connection_id"])
    except Exception as exc:
        log.warning("vault send_reply owner=%s chat=%s failed: %s", owner_id, chat_id, exc)
        return {"ok": False, "error": f"Не отправлено: {str(exc)[:120]}"}
    # Своё же исходящее тоже кладём в архив (business_message-эхо может не прийти).
    try:
        await pool.execute(
            """INSERT INTO vault_messages
                   (owner_id, connection_id, chat_id, msg_id, direction, text_enc, msg_date)
               VALUES ($1,$2,$3,$4,'out',$5, now())
               ON CONFLICT (owner_id, chat_id, msg_id) DO NOTHING""",
            owner_id, conn["connection_id"], chat_id, getattr(sent, "message_id", 0),
            encrypt_token(text[:4096]))
    except Exception:
        log.debug("vault send_reply: локальное эхо не записано")
    return {"ok": True, "message_id": getattr(sent, "message_id", None)}


# ── внутреннее ────────────────────────────────────────────────────────────────

def _load_json(val) -> Any:
    if val is None or isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except (ValueError, TypeError):
        return None


def _preview(row) -> str:
    if not row:
        return ""
    if row["text_enc"]:
        t = decrypt_token(row["text_enc"]) or ""
        t = " ".join(t.split())
        return (t[:79] + "…") if len(t) > 80 else t
    mt = row["media_type"]
    return _MEDIA_LABEL.get(mt, "📎 Вложение") if mt else ""


_MEDIA_LABEL = {
    "photo": "📷 Фото", "video": "🎬 Видео", "animation": "🎞 GIF",
    "video_note": "⭕ Кружок", "voice": "🎤 Голосовое", "audio": "🎵 Аудио",
    "document": "📎 Файл", "sticker": "🃏 Стикер",
}


def _ui_message(r) -> dict:
    """Строка архива → форма для мини-аппа (текст расшифрован для владельца)."""
    return {
        "msg_id": r["msg_id"],
        "direction": r["direction"],
        "text": (decrypt_token(r["text_enc"]) if r["text_enc"] else ""),
        "media_type": r["media_type"],
        "media_label": _MEDIA_LABEL.get(r["media_type"], "📎 Вложение") if r["media_type"] else "",
        "media_name": r["media_name"] if "media_name" in r.keys() else None,
        "media_size": (int(r["media_size"]) if "media_size" in r.keys() and r["media_size"] else None),
        # Есть ли что скачивать. Сам file_id наружу не отдаём: он одноразово
        # обменивается на файл только через бота, и светить его в мини-аппе
        # незачем — за файлом клиент ходит на наш эндпоинт по (chat_id, msg_id).
        "has_media": bool(
            ("media_file_id" in r.keys() and r["media_file_id"]) or r["media_type"]
        ),
        "date": r["msg_date"].isoformat() if r["msg_date"] else None,
        "deleted": bool(r["is_deleted"]),
        "edited": bool(r["is_edited"]),
    }
