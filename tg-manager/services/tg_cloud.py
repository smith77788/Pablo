"""Telegram-облако: распределённое зашифрованное хранилище файлов.

Модель: файл → SHA-256 → нарезка на куски → шифрование каждого (AES-256-GCM) →
транспорт кладёт кусок в хранилище (v1 — БД `tg_cloud_blobs`; далее — приватные
каналы аккаунтов флота) → манифест (tg_cloud_files/tg_cloud_chunks). Скачивание —
обратная сборка с проверкой целостности.

Транспорт абстрагирован (ChunkTransport): манифест/API/UI не зависят от того, где
физически лежат куски. Флот-Telethon-транспорт подключается, НЕ меняя манифест.

Доступ: владелец платформы (is_platform_admin) — безлимит (личное пользование);
остальные — платно (tg_cloud_quota.paid=TRUE или тариф) с лимитом объёма.
"""
from __future__ import annotations

import hashlib
import logging

from services import token_vault

log = logging.getLogger(__name__)

# 2 ГБ — предел одного файла в Telegram для юзер-аккаунта (флот-транспорт).
CHUNK_SIZE = 2 * 1024 * 1024 * 1024
# Дефолтный лимит платного доступа, если не задан явно (100 ГБ).
DEFAULT_PAID_LIMIT = 100 * 1024 * 1024 * 1024


# ── Чистые помощники: нарезка + шифрование ────────────────────────────────────
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def iter_chunks(data: bytes, size: int = CHUNK_SIZE):
    """Режет байты на куски по size. ЧИСТАЯ. Возвращает (ord, chunk_bytes)."""
    size = max(1, int(size))
    n = 0
    for i in range(0, len(data or b""), size):
        yield n, data[i:i + size]
        n += 1


def pack_chunk(chunk: bytes) -> bytes:
    """Зашифровать кусок для хранения. ЧИСТАЯ (кроме nonce)."""
    return token_vault.encrypt_bytes(chunk)


def unpack_chunk(blob: bytes) -> bytes:
    """Расшифровать кусок. ЧИСТАЯ. Бросает при неверном ключе/порче."""
    return token_vault.decrypt_bytes(blob)


# ── Транспорт кусков (абстракция) ─────────────────────────────────────────────
class ChunkTransport:
    """put кладёт зашифрованный blob и возвращает dict-локатор (что записать в
    манифест: locator и, для флота, acc_id/channel_id/message_id). get достаёт
    blob по строке `locator`. delete удаляет."""
    async def put(self, pool, owner_id: int, file_id: int, ord: int, blob: bytes) -> dict:
        raise NotImplementedError

    async def get(self, pool, locator: str) -> bytes:
        raise NotImplementedError

    async def delete(self, pool, locator: str) -> None:
        raise NotImplementedError


class DbTransport(ChunkTransport):
    """v1-бэкенд: куски в таблице tg_cloud_blobs. Работает без флота; годится для
    небольших файлов и как эталон контракта для флот-транспорта."""
    async def put(self, pool, owner_id: int, file_id: int, ord: int, blob: bytes) -> dict:
        loc = f"db:{owner_id}:{file_id}:{ord}"
        await pool.execute(
            "INSERT INTO tg_cloud_blobs(locator, owner_id, data) VALUES($1,$2,$3) "
            "ON CONFLICT (locator) DO UPDATE SET data=EXCLUDED.data",
            loc, owner_id, blob)
        return {"locator": loc}

    async def get(self, pool, locator: str) -> bytes:
        row = await pool.fetchval(
            "SELECT data FROM tg_cloud_blobs WHERE locator=$1", locator)
        return bytes(row) if row is not None else b""

    async def delete(self, pool, locator: str) -> None:
        await pool.execute("DELETE FROM tg_cloud_blobs WHERE locator=$1", locator)


# ── Доступ и квоты ────────────────────────────────────────────────────────────
async def get_quota(pool, owner_id: int) -> dict:
    r = await pool.fetchrow(
        "SELECT limit_bytes, used_bytes, paid FROM tg_cloud_quota WHERE owner_id=$1",
        owner_id)
    if not r:
        return {"limit_bytes": 0, "used_bytes": 0, "paid": False}
    return {"limit_bytes": int(r["limit_bytes"]), "used_bytes": int(r["used_bytes"]),
            "paid": bool(r["paid"])}


def _is_admin(owner_id: int) -> bool:
    try:
        from bot.utils.subscription import is_platform_admin
        return bool(is_platform_admin(owner_id))
    except Exception:
        return False


async def has_access(pool, owner_id: int) -> bool:
    """Есть ли доступ к облаку: владелец платформы — всегда (личное пользование);
    остальные — при оплате (quota.paid) или платном тарифе."""
    if _is_admin(owner_id):
        return True
    q = await get_quota(pool, owner_id)
    if q["paid"]:
        return True
    try:
        from bot.utils.subscription import require_plan
        return bool(await require_plan(pool, owner_id, "pro"))
    except Exception:
        return False


async def _effective_limit(pool, owner_id: int) -> int:
    """0 = безлимит (админ/без явного лимита), иначе байтовый потолок."""
    if _is_admin(owner_id):
        return 0
    q = await get_quota(pool, owner_id)
    if q["limit_bytes"] > 0:
        return q["limit_bytes"]
    return DEFAULT_PAID_LIMIT if q["paid"] else 0


async def set_paid(pool, owner_id: int, paid: bool, *, limit_bytes: int | None = None) -> dict:
    lim = int(limit_bytes) if limit_bytes is not None else DEFAULT_PAID_LIMIT
    await pool.execute(
        """INSERT INTO tg_cloud_quota(owner_id, paid, limit_bytes)
           VALUES($1,$2,$3)
           ON CONFLICT (owner_id) DO UPDATE SET paid=EXCLUDED.paid,
               limit_bytes=EXCLUDED.limit_bytes, updated_at=now()""",
        owner_id, bool(paid), lim)
    return await get_quota(pool, owner_id)


async def _add_used(pool, owner_id: int, delta: int) -> None:
    await pool.execute(
        """INSERT INTO tg_cloud_quota(owner_id, used_bytes) VALUES($1, GREATEST($2,0))
           ON CONFLICT (owner_id) DO UPDATE
             SET used_bytes = GREATEST(tg_cloud_quota.used_bytes + $2, 0),
                 updated_at = now()""",
        owner_id, int(delta))


class QuotaExceeded(Exception):
    pass


class AccessDenied(Exception):
    pass


# ── Манифест ──────────────────────────────────────────────────────────────────
async def list_files(pool, owner_id: int, *, limit: int = 200) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, name, size_bytes, chunk_count, sha256, status, created_at "
        "FROM tg_cloud_files WHERE owner_id=$1 ORDER BY id DESC LIMIT $2",
        owner_id, max(1, min(1000, limit)))
    return [dict(r) for r in rows]


async def get_file(pool, owner_id: int, file_id: int) -> dict | None:
    r = await pool.fetchrow(
        "SELECT * FROM tg_cloud_files WHERE id=$1 AND owner_id=$2", file_id, owner_id)
    return dict(r) if r else None


# ── Оркестрация: сохранить / достать / удалить ────────────────────────────────
async def store_file(pool, owner_id: int, name: str, data: bytes,
                     transport: ChunkTransport | None = None,
                     *, chunk_size: int = CHUNK_SIZE) -> dict:
    """Зашифровать, нарезать, разложить транспортом, записать манифест. Проверяет
    доступ и квоту. Возвращает запись файла."""
    if not await has_access(pool, owner_id):
        raise AccessDenied("нет доступа к облаку (нужна оплата)")
    data = data or b""
    size = len(data)
    limit = await _effective_limit(pool, owner_id)
    if limit > 0:
        used = (await get_quota(pool, owner_id))["used_bytes"]
        if used + size > limit:
            raise QuotaExceeded(f"превышен лимит хранилища ({used + size} > {limit})")
    transport = transport or DbTransport()
    file_id = int(await pool.fetchval(
        "INSERT INTO tg_cloud_files(owner_id, name, size_bytes, sha256, status) "
        "VALUES($1,$2,$3,$4,'pending') RETURNING id",
        owner_id, (name or "file").strip()[:200], size, sha256_hex(data)))
    n = 0
    for ordi, chunk in iter_chunks(data, chunk_size):
        blob = pack_chunk(chunk)
        loc = await transport.put(pool, owner_id, file_id, ordi, blob)
        await pool.execute(
            """INSERT INTO tg_cloud_chunks(file_id, owner_id, ord, size_bytes, sha256,
                   acc_id, channel_id, message_id, locator, status)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'stored')""",
            file_id, owner_id, ordi, len(chunk), sha256_hex(chunk),
            loc.get("acc_id"), loc.get("channel_id"), loc.get("message_id"),
            loc.get("locator") or "")
        n = ordi + 1
    await pool.execute(
        "UPDATE tg_cloud_files SET status='stored', chunk_count=$2, updated_at=now() "
        "WHERE id=$1", file_id, n)
    await _add_used(pool, owner_id, size)
    return await get_file(pool, owner_id, file_id)


async def retrieve_file(pool, owner_id: int, file_id: int,
                        transport: ChunkTransport | None = None) -> bytes:
    """Собрать файл обратно: куски по порядку → расшифровать → склеить → сверить
    SHA-256. Бросает при рассинхроне/порче."""
    f = await get_file(pool, owner_id, file_id)
    if not f:
        raise FileNotFoundError("файл не найден")
    transport = transport or DbTransport()
    rows = await pool.fetch(
        "SELECT ord, locator, sha256 FROM tg_cloud_chunks WHERE file_id=$1 ORDER BY ord",
        file_id)
    out = bytearray()
    for r in rows:
        blob = await transport.get(pool, r["locator"])
        chunk = unpack_chunk(blob)
        if r["sha256"] and sha256_hex(chunk) != r["sha256"]:
            raise ValueError(f"кусок {r['ord']} повреждён (хэш не сошёлся)")
        out.extend(chunk)
    res = bytes(out)
    if f["sha256"] and sha256_hex(res) != f["sha256"]:
        raise ValueError("файл собран, но хэш целого не сошёлся")
    return res


async def delete_file(pool, owner_id: int, file_id: int,
                      transport: ChunkTransport | None = None) -> bool:
    f = await get_file(pool, owner_id, file_id)
    if not f:
        return False
    transport = transport or DbTransport()
    rows = await pool.fetch(
        "SELECT locator FROM tg_cloud_chunks WHERE file_id=$1", file_id)
    for r in rows:
        try:
            await transport.delete(pool, r["locator"])
        except Exception:
            log.debug("tg_cloud: не удалён blob %s", r["locator"])
    await pool.execute("DELETE FROM tg_cloud_files WHERE id=$1 AND owner_id=$2",
                       file_id, owner_id)
    await _add_used(pool, owner_id, -int(f["size_bytes"]))
    return True
