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
# Избыточность: сколько КОПИЙ каждого куска держим на РАЗНЫХ хранителях. Бан
# одного аккаунта не должен лишать доступа к файлу — пока жива хотя бы одна
# реплика, файл собирается; реконсайлер (heal) дотягивает избыточность обратно.
# 2 — разумный дефолт; для флот-транспорта реплики кладутся на разные аккаунты.
REPLICAS = 2


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
    манифест: locator и, для флота, acc_id/channel_id/message_id). `replica` —
    номер копии: транспорт обязан класть разные реплики в РАЗНЫЕ места (флот —
    на разные аккаунты), иначе избыточность мнимая. get достаёт blob по строке
    `locator`. delete удаляет."""
    async def put(self, pool, owner_id: int, file_id: int, ord: int, blob: bytes,
                  replica: int = 0) -> dict:
        raise NotImplementedError

    async def get(self, pool, locator: str) -> bytes:
        raise NotImplementedError

    async def delete(self, pool, locator: str) -> None:
        raise NotImplementedError


class DbTransport(ChunkTransport):
    """v1-бэкенд: куски в таблице tg_cloud_blobs. Работает без флота; годится для
    небольших файлов и как эталон контракта для флот-транспорта. Реплики — разные
    строки blobs под разными локаторами (в проде БД сама избыточна на уровне
    хранилища, но контракт избыточности соблюдаем единообразно)."""
    async def put(self, pool, owner_id: int, file_id: int, ord: int, blob: bytes,
                  replica: int = 0) -> dict:
        loc = f"db:{owner_id}:{file_id}:{ord}:r{replica}"
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
                     *, chunk_size: int = CHUNK_SIZE, replicas: int = REPLICAS) -> dict:
    """Зашифровать, нарезать, разложить транспортом В НЕСКОЛЬКИХ репликах, записать
    манифест. Проверяет доступ и квоту. Квота считает ЛОГИЧЕСКИЙ размер файла, а не
    физические копии: избыточность — расход платформы/флота ради отказоустойчивости,
    а не то, за что платит пользователь. Возвращает запись файла."""
    if not await has_access(pool, owner_id):
        raise AccessDenied("нет доступа к облаку (нужна оплата)")
    data = data or b""
    size = len(data)
    replicas = max(1, int(replicas))
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
        chunk_id = int(await pool.fetchval(
            "INSERT INTO tg_cloud_chunks(file_id, owner_id, ord, size_bytes, sha256, "
            "status) VALUES($1,$2,$3,$4,$5,'stored') RETURNING id",
            file_id, owner_id, ordi, len(chunk), sha256_hex(chunk)))
        for rep in range(replicas):
            loc = await transport.put(pool, owner_id, file_id, ordi, blob, replica=rep)
            await _record_loc(pool, chunk_id, owner_id, rep, loc)
        n = ordi + 1
    await pool.execute(
        "UPDATE tg_cloud_files SET status='stored', chunk_count=$2, updated_at=now() "
        "WHERE id=$1", file_id, n)
    await _add_used(pool, owner_id, size)
    return await get_file(pool, owner_id, file_id)


async def _record_loc(pool, chunk_id: int, owner_id: int, replica: int, loc: dict) -> None:
    """Записать одну локацию (реплику) куска в манифест. ON CONFLICT — для heal,
    который может переиспользовать тот же locator при повторной укладке."""
    await pool.execute(
        """INSERT INTO tg_cloud_chunk_locs(chunk_id, owner_id, replica, acc_id,
               channel_id, message_id, locator, status)
           VALUES($1,$2,$3,$4,$5,$6,$7,'stored')
           ON CONFLICT (chunk_id, locator) DO UPDATE SET status='stored',
               replica=EXCLUDED.replica, acc_id=EXCLUDED.acc_id,
               channel_id=EXCLUDED.channel_id, message_id=EXCLUDED.message_id""",
        chunk_id, owner_id, replica, loc.get("acc_id"), loc.get("channel_id"),
        loc.get("message_id"), loc.get("locator") or f"loc:{chunk_id}:{replica}")


async def _read_chunk(pool, transport: ChunkTransport, chunk_id: int, sha: str):
    """Достать кусок из ЛЮБОЙ живой реплики с проверкой целостности. Возвращает
    (расшифрованный_кусок, зашифрованный_blob, список_живых_локаторов) или
    (None, None, []), если ни одна реплика не поднялась (все хранители мертвы)."""
    locs = await pool.fetch(
        "SELECT locator FROM tg_cloud_chunk_locs WHERE chunk_id=$1 AND status='stored' "
        "ORDER BY replica", chunk_id)
    alive = [r["locator"] for r in locs]
    for locator in alive:
        try:
            blob = await transport.get(pool, locator)
            if not blob:
                continue
            chunk = unpack_chunk(blob)
        except Exception:
            continue
        if sha and sha256_hex(chunk) != sha:
            continue
        return chunk, blob, alive
    return None, None, alive


async def retrieve_file(pool, owner_id: int, file_id: int,
                        transport: ChunkTransport | None = None) -> bytes:
    """Собрать файл обратно: для каждого куска берём ЛЮБУЮ живую реплику →
    расшифровать → склеить → сверить SHA-256. Пока жива хотя бы одна копия куска,
    сборка проходит — баны части флота не мешают. Бросает, только если у куска не
    осталось ни одной живой реплики или не сошёлся хэш целого."""
    f = await get_file(pool, owner_id, file_id)
    if not f:
        raise FileNotFoundError("файл не найден")
    transport = transport or DbTransport()
    rows = await pool.fetch(
        "SELECT id, ord, sha256 FROM tg_cloud_chunks WHERE file_id=$1 ORDER BY ord",
        file_id)
    out = bytearray()
    for r in rows:
        chunk, _blob, _alive = await _read_chunk(pool, transport, r["id"], r["sha256"])
        if chunk is None:
            raise ValueError(
                f"кусок {r['ord']}: не осталось ни одной живой реплики "
                "(все хранители недоступны/забанены) — запустите восстановление")
        out.extend(chunk)
    res = bytes(out)
    if f["sha256"] and sha256_hex(res) != f["sha256"]:
        raise ValueError("файл собран, но хэш целого не сошёлся")
    return res


async def heal_file(pool, owner_id: int, file_id: int,
                    transport: ChunkTransport | None = None,
                    *, replicas: int = REPLICAS) -> dict:
    """Реконсайлер избыточности: для каждого куска, где живых реплик меньше
    нормы, берём уцелевшую копию и доливаем недостающие на новые слоты. Так файл
    возвращает отказоустойчивость после бана части хранителей. Возвращает отчёт:
    сколько реплик восстановлено и сколько кусков восстановить НЕ удалось (все
    реплики мертвы — файл в опасности, нужен сигнал владельцу)."""
    transport = transport or DbTransport()
    replicas = max(1, int(replicas))
    rows = await pool.fetch(
        "SELECT id, ord, sha256 FROM tg_cloud_chunks WHERE file_id=$1 ORDER BY ord",
        file_id)
    restored = 0
    unhealable = 0
    for r in rows:
        chunk_id = r["id"]
        live = await pool.fetchval(
            "SELECT count(*) FROM tg_cloud_chunk_locs WHERE chunk_id=$1 AND status='stored'",
            chunk_id)
        if int(live) >= replicas:
            continue
        chunk, blob, alive = await _read_chunk(pool, transport, chunk_id, r["sha256"])
        if chunk is None:
            unhealable += 1
            continue
        # Занятые номера реплик — чтобы новые не конфликтовали по UNIQUE.
        used_reps = {int(x["replica"]) for x in await pool.fetch(
            "SELECT replica FROM tg_cloud_chunk_locs WHERE chunk_id=$1", chunk_id)}
        rep = 0
        need = replicas - int(live)
        made = 0
        while made < need:
            while rep in used_reps:
                rep += 1
            loc = await transport.put(pool, owner_id, file_id, r["ord"], blob, replica=rep)
            await _record_loc(pool, chunk_id, owner_id, rep, loc)
            used_reps.add(rep)
            made += 1
            restored += 1
    # Статус файла отражает здоровье: degraded, если есть невосстановимые куски.
    if rows:
        await pool.execute(
            "UPDATE tg_cloud_files SET status=$2, updated_at=now() WHERE id=$1",
            file_id, ("degraded" if unhealable else "stored"))
    return {"restored": restored, "unhealable": unhealable}


async def mark_account_dead(pool, acc_id: int) -> int:
    """Аккаунт-хранитель забанен/потерян — пометить все его локации dead, чтобы
    сборка их не трогала, а heal восстановил избыточность с уцелевших реплик.
    Вызывается пайплайном обработки банов. Возвращает число помеченных локаций."""
    status = await pool.execute(
        "UPDATE tg_cloud_chunk_locs SET status='dead' WHERE acc_id=$1 AND status<>'dead'",
        acc_id)
    # asyncpg возвращает команд-тег вида "UPDATE <n>".
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def file_durability(pool, file_id: int) -> dict:
    """Здоровье избыточности файла для UI/мониторинга: минимум живых реплик среди
    кусков (0 = есть недоступный кусок), всего живых локаций, норма."""
    row = await pool.fetchrow(
        """SELECT COALESCE(MIN(live),0) AS min_live, COALESCE(SUM(live),0) AS total_live,
                  count(*) AS chunks
             FROM (SELECT c.id,
                          (SELECT count(*) FROM tg_cloud_chunk_locs l
                            WHERE l.chunk_id=c.id AND l.status='stored') AS live
                     FROM tg_cloud_chunks c WHERE c.file_id=$1) q""",
        file_id)
    return {"min_live": int(row["min_live"]), "total_live": int(row["total_live"]),
            "chunks": int(row["chunks"]), "target": REPLICAS}


async def delete_file(pool, owner_id: int, file_id: int,
                      transport: ChunkTransport | None = None) -> bool:
    f = await get_file(pool, owner_id, file_id)
    if not f:
        return False
    transport = transport or DbTransport()
    rows = await pool.fetch(
        "SELECT l.locator FROM tg_cloud_chunk_locs l "
        "JOIN tg_cloud_chunks c ON c.id=l.chunk_id WHERE c.file_id=$1", file_id)
    for r in rows:
        try:
            await transport.delete(pool, r["locator"])
        except Exception:
            log.debug("tg_cloud: не удалён blob %s", r["locator"])
    # CASCADE по file_id снесёт tg_cloud_chunks → tg_cloud_chunk_locs.
    await pool.execute("DELETE FROM tg_cloud_files WHERE id=$1 AND owner_id=$2",
                       file_id, owner_id)
    await _add_used(pool, owner_id, -int(f["size_bytes"]))
    return True
