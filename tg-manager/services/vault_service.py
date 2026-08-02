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


async def record_edit(pool, message: Any, owner_id: int) -> None:
    """Правка сообщения: старую версию текста складываем в edit_history, ставим
    новый текст и is_edited. Так видно и что было, и что стало."""
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    msg_id = getattr(message, "message_id", None)
    if chat_id is None or msg_id is None:
        return
    row = await pool.fetchrow(
        "SELECT text_enc, edit_history FROM vault_messages "
        "WHERE owner_id=$1 AND chat_id=$2 AND msg_id=$3", owner_id, chat_id, msg_id)
    if not row:
        # Правка сообщения, которого нет в архиве (пришло до подключения) — просто
        # сохраняем как новое, чтобы не потерять текущий текст.
        await archive_message(pool, message, owner_id, getattr(message, "business_connection_id", None))
        await pool.execute(
            "UPDATE vault_messages SET is_edited=TRUE, edited_at=now() "
            "WHERE owner_id=$1 AND chat_id=$2 AND msg_id=$3", owner_id, chat_id, msg_id)
        return
    hist = _load_json(row["edit_history"]) or []
    if row["text_enc"]:
        hist.append(row["text_enc"])          # прошлые версии — тоже зашифрованы
    text = text_of(message)
    await pool.execute(
        "UPDATE vault_messages SET text_enc=$4, is_edited=TRUE, edited_at=now(), "
        "edit_history=$5::jsonb WHERE owner_id=$1 AND chat_id=$2 AND msg_id=$3",
        owner_id, chat_id, msg_id,
        encrypt_token(text) if text else None, json.dumps(hist[-20:]),
    )


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


# ── Чтение архива (для мини-аппа), строго owner-scoped ───────────────────────

async def list_chats(pool, owner_id: int, limit: int = 100) -> list[dict]:
    """Список чатов в хранилище с последним сообщением и счётчиками."""
    rows = await pool.fetch(
        """SELECT chat_id,
                  MAX(peer_name)      AS peer_name,
                  MAX(peer_username)  AS peer_username,
                  COUNT(*)            AS total,
                  COUNT(*) FILTER (WHERE is_deleted) AS deleted,
                  MAX(msg_date)       AS last_date
           FROM vault_messages WHERE owner_id=$1
           GROUP BY chat_id ORDER BY last_date DESC NULLS LAST LIMIT $2""",
        owner_id, limit)
    out = []
    for r in rows:
        last = await pool.fetchrow(
            "SELECT text_enc, media_type, is_deleted FROM vault_messages "
            "WHERE owner_id=$1 AND chat_id=$2 ORDER BY msg_date DESC NULLS LAST LIMIT 1",
            owner_id, r["chat_id"])
        out.append({
            "chat_id": r["chat_id"],
            "peer_name": r["peer_name"] or (f"@{r['peer_username']}" if r["peer_username"] else str(r["chat_id"])),
            "peer_username": r["peer_username"],
            "total": int(r["total"] or 0),
            "deleted": int(r["deleted"] or 0),
            "last_date": r["last_date"].isoformat() if r["last_date"] else None,
            "last_preview": _preview(last),
        })
    return out


async def list_messages(pool, owner_id: int, chat_id: int,
                        limit: int = 200, offset: int = 0) -> list[dict]:
    rows = await pool.fetch(
        """SELECT msg_id, direction, text_enc, media_type, media_file_id, media_name,
                  media_size, msg_date, is_deleted, is_edited
           FROM vault_messages WHERE owner_id=$1 AND chat_id=$2
           ORDER BY msg_date ASC NULLS FIRST, msg_id ASC LIMIT $3 OFFSET $4""",
        owner_id, chat_id, min(int(limit), 1000), max(int(offset), 0))
    return [_ui_message(r) for r in rows]


async def search_messages(pool, owner_id: int, query: str, limit: int = 100) -> list[dict]:
    """Поиск по расшифрованному тексту. Текст зашифрован (нельзя ILIKE в SQL),
    поэтому тянем недавние строки owner'а и фильтруем в приложении после расшифровки.
    Ограничиваем окно, чтобы не расшифровывать весь архив на каждый запрос."""
    q = (query or "").strip().lower()
    if not q:
        return []
    rows = await pool.fetch(
        """SELECT chat_id, msg_id, direction, text_enc, media_type, media_name,
                  peer_name, peer_username, msg_date, is_deleted, is_edited
           FROM vault_messages WHERE owner_id=$1 AND text_enc IS NOT NULL
           ORDER BY msg_date DESC NULLS LAST LIMIT 4000""",
        owner_id)
    out = []
    for r in rows:
        text = decrypt_token(r["text_enc"]) if r["text_enc"] else ""
        if q in (text or "").lower():
            d = _ui_message(r)
            d["chat_id"] = r["chat_id"]
            d["peer_name"] = r["peer_name"] or (f"@{r['peer_username']}" if r["peer_username"] else str(r["chat_id"]))
            out.append(d)
            if len(out) >= limit:
                break
    return out


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
        "date": r["msg_date"].isoformat() if r["msg_date"] else None,
        "deleted": bool(r["is_deleted"]),
        "edited": bool(r["is_edited"]),
    }
