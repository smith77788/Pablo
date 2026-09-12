"""Облако, флот-транспорт (Wave 2): куски хранятся в приватных каналах аккаунтов.

Каждый аккаунт-хранитель держит приватный канал-«склад»; зашифрованный кусок
кладётся туда документом. Реплики ОДНОГО куска распределяются по РАЗНЫМ
аккаунтам — бан одного хранителя не лишает доступа к файлу (сборка берёт любую
живую реплику; см. services/tg_cloud.retrieve_file / heal_file).

Контракт манифеста — общий с DbTransport: транспорт возвращает локатор
(acc_id/channel_id/message_id + строка locator), а tg_cloud хранит избыточность и
собирает файл, не зная, где физически лежат куски.

ВСЕ обращения к Telegram вынесены в СЕАМЫ (_upload_blob/_download_blob/
_delete_blob/_ensure_storage_channel): в проде они зовут telethon через
account_manager, в тестах подменяются заглушками — так проверяется оркестрация
(распределение реплик по разным аккаунтам, манифест, mark_dead→heal) без живого
telethon, который в песочнице собрать нельзя.

Выбор хранителей уважает здоровье флота: забаненные/карантинные аккаунты в
хранители не берутся (is_account_quarantined, fail-open).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Имя документа-куска в канале-складе (косметика; содержимое — шифртекст).
_BLOB_NAME = "ic.bin"


# ── Выбор аккаунтов-хранителей ────────────────────────────────────────────────
async def select_storekeepers(pool, owner_id: int, need: int) -> list[dict]:
    """До `need` аккаунтов владельца под хранение — через флуд-осознанное ядро
    (resource_selector → flood_engine): оно учитывает кулдаун, доверие и серии
    флудов и не отдаёт аккаунт, которому сейчас нельзя работать (единая «дверь к
    аккаунту», см. tests/test_account_selection_single_door.py).

    Возвращает dict'ы аккаунта с РАСШИФРОВАННЫМ session_str (клиент строим из него).
    action_type='cloud_store' — отдельный класс действия для учёта нагрузки."""
    from services import resource_selector, token_vault
    accs = await resource_selector.select_accounts(
        pool, owner_id, max(1, int(need)), action_type="cloud_store")
    out: list[dict] = []
    for a in accs:
        acc = dict(a)
        try:
            acc["session_str"] = token_vault.decrypt_token(a.get("session_str") or "")
        except Exception:
            acc["session_str"] = a.get("session_str") or ""
        out.append(acc)
    return out


# ── Сеамы Telegram (подменяются в тестах) ─────────────────────────────────────
async def _ensure_storage_channel(pool, acc: dict) -> tuple[int, int]:
    """Вернуть (channel_id, access_hash) канала-склада аккаунта, создав при нужде.

    Привязку кэшируем в tg_cloud_storekeepers. В проде создаёт приватный канал
    через account_manager.create_channel."""
    acc_id = int(acc["id"])
    row = await pool.fetchrow(
        "SELECT channel_id, access_hash FROM tg_cloud_storekeepers WHERE acc_id=$1", acc_id)
    if row:
        return int(row["channel_id"]), int(row["access_hash"] or 0)
    from services import account_manager
    res = await account_manager.create_channel(
        acc.get("session_str") or "", title="infra-store", about="",
        megagroup=False, _acc=acc)
    if res.get("error"):
        raise RuntimeError(f"не создан канал-склад acc={acc_id}: {res['error']}")
    ch_id = int(res["channel_id"])
    ch_hash = int(res.get("access_hash") or 0)
    await pool.execute(
        "INSERT INTO tg_cloud_storekeepers(acc_id, owner_id, channel_id, access_hash) "
        "VALUES($1,$2,$3,$4) ON CONFLICT (acc_id) DO UPDATE SET channel_id=EXCLUDED.channel_id, "
        "access_hash=EXCLUDED.access_hash",
        acc_id, int(acc["owner_id"]), ch_id, ch_hash)
    return ch_id, ch_hash


async def _upload_blob(acc: dict, channel_id: int, access_hash: int, blob: bytes) -> int:
    """Загрузить blob документом в канал-склад, вернуть message_id."""
    from services import account_manager
    return await account_manager.upload_cloud_blob(
        acc.get("session_str") or "", channel_id, access_hash, blob, _BLOB_NAME, _acc=acc)


async def _download_blob(acc: dict, channel_id: int, access_hash: int, message_id: int) -> bytes:
    """Скачать документ сообщения message_id из канала-склада."""
    from services import account_manager
    return await account_manager.download_cloud_blob(
        acc.get("session_str") or "", channel_id, access_hash, message_id, _acc=acc)


async def _delete_blob(acc: dict, channel_id: int, access_hash: int, message_id: int) -> None:
    from services import account_manager
    await account_manager.delete_cloud_blob(
        acc.get("session_str") or "", channel_id, access_hash, message_id, _acc=acc)


async def reconcile(pool, *, heal: bool = True, max_files: int = 50) -> dict:
    """Самовосстановление облака на флот-бэкенде:
      1) локации забаненных/мёртвых хранителей помечаются dead;
      2) у деградировавших файлов избыточность доливается heal'ом на живые аккаунты.
    Так доступ к файлам держится сам, без ручных действий после бана флота.
    Возвращает сводку. max_files ограничивает один проход (heal ходит в Telegram)."""
    from services import tg_cloud
    marked = await tg_cloud.mark_dead_for_banned_storekeepers(pool)
    files = restored = unhealable = 0
    if heal:
        for row in await tg_cloud.list_degraded_files(pool, limit=max_files):
            files += 1
            try:
                res = await tg_cloud.heal_file(
                    pool, row["owner_id"], row["file_id"], FleetTransport(row["owner_id"]))
                restored += res.get("restored", 0)
                unhealable += res.get("unhealable", 0)
            except Exception:
                log.warning("cloud reconcile: heal файла %s не прошёл",
                            row["file_id"], exc_info=True)
    return {"marked_dead": marked, "files_touched": files,
            "restored": restored, "unhealable": unhealable}


# ── Транспорт ─────────────────────────────────────────────────────────────────
class FleetTransport:
    """Реплики кусков — в приватных каналах РАЗНЫХ аккаунтов флота.

    Экземпляр живёт в пределах одной операции store/heal: держит карту «какой
    аккаунт уже хранит какой кусок», чтобы реплики одного куска ложились на разные
    аккаунты. Кандидаты — здоровые аккаунты владельца (select_storekeepers).
    """
    def __init__(self, owner_id: int, candidates: list[dict] | None = None):
        self.owner_id = int(owner_id)
        self._candidates = candidates           # None → ленивая загрузка из pool
        self._by_ord: dict[int, set[int]] = {}  # ord → множество acc_id уже использованных
        self._seeded: set[int] = set()          # ord'ы, для которых подтянули занятые аккаунты из БД
        self._acc_by_id: dict[int, dict] = {}
        self._rr = 0                            # round-robin указатель для размазывания

    async def _ensure_candidates(self, pool, need: int) -> list[dict]:
        if self._candidates is None:
            self._candidates = await select_storekeepers(pool, self.owner_id, max(need, 8))
        for a in self._candidates:
            self._acc_by_id[int(a["id"])] = a
        return self._candidates

    def _pick(self, ord_: int) -> dict | None:
        """Аккаунт под новую реплику куска ord_: не тот, что уже хранит этот кусок."""
        used = self._by_ord.setdefault(ord_, set())
        cands = self._candidates or []
        if not cands:
            return None
        n = len(cands)
        for i in range(n):
            a = cands[(self._rr + i) % n]
            if int(a["id"]) not in used:
                self._rr = (self._rr + i + 1) % n
                used.add(int(a["id"]))
                return a
        return None  # все кандидаты уже держат этот кусок — больше реплик не разместить

    async def _seed_used_from_db(self, pool, file_id: int, ord: int) -> None:
        """Подтянуть аккаунты, уже держащие этот кусок (любой статус, включая dead),
        чтобы новая реплика не легла ни на того, кто уже хранит, ни на забаненного.
        Нужно для heal (свежий транспорт не знает истории); для store — no-op."""
        if ord in self._seeded:
            return
        self._seeded.add(ord)
        rows = await pool.fetch(
            "SELECT DISTINCT l.acc_id FROM tg_cloud_chunk_locs l "
            "JOIN tg_cloud_chunks c ON c.id = l.chunk_id "
            "WHERE c.file_id = $1 AND c.ord = $2 AND l.acc_id IS NOT NULL", file_id, ord)
        used = self._by_ord.setdefault(ord, set())
        for r in rows:
            used.add(int(r["acc_id"]))

    async def put(self, pool, owner_id: int, file_id: int, ord: int, blob: bytes,
                  replica: int = 0) -> dict:
        await self._ensure_candidates(pool, need=replica + 1)
        await self._seed_used_from_db(pool, file_id, ord)
        acc = self._pick(ord)
        if acc is None:
            raise RuntimeError(
                "недостаточно здоровых аккаунтов-хранителей для размещения реплики "
                f"куска {ord} (нужны РАЗНЫЕ аккаунты под разные реплики)")
        ch_id, ch_hash = await _ensure_storage_channel(pool, acc)
        msg_id = await _upload_blob(acc, ch_id, ch_hash, blob)
        acc_id = int(acc["id"])
        return {"acc_id": acc_id, "channel_id": ch_id, "message_id": int(msg_id),
                "locator": f"tg:{acc_id}:{ch_id}:{int(msg_id)}"}

    async def _acc_for(self, pool, acc_id: int) -> dict | None:
        if acc_id in self._acc_by_id:
            return self._acc_by_id[acc_id]
        r = await pool.fetchrow(
            "SELECT id, session_str, phone, owner_id FROM tg_accounts WHERE id=$1", acc_id)
        if not r:
            return None
        acc = dict(r)
        try:
            from services import token_vault
            acc["session_str"] = token_vault.decrypt_token(r["session_str"] or "")
        except Exception:
            acc["session_str"] = r["session_str"] or ""
        self._acc_by_id[acc_id] = acc
        return acc

    @staticmethod
    def _parse(locator: str) -> tuple[int, int, int] | None:
        try:
            _tag, a, c, m = locator.split(":")
            return int(a), int(c), int(m)
        except (ValueError, AttributeError):
            return None

    async def get(self, pool, locator: str) -> bytes:
        p = self._parse(locator)
        if not p:
            return b""
        acc_id, ch_id, msg_id = p
        acc = await self._acc_for(pool, acc_id)
        if not acc:
            return b""
        ch_hash = int(await pool.fetchval(
            "SELECT access_hash FROM tg_cloud_storekeepers WHERE acc_id=$1", acc_id) or 0)
        try:
            return await _download_blob(acc, ch_id, ch_hash, msg_id)
        except Exception as e:
            log.info("fleet get: реплика недоступна %s (%s)", locator, str(e)[:80])
            return b""

    async def delete(self, pool, locator: str) -> None:
        p = self._parse(locator)
        if not p:
            return
        acc_id, ch_id, msg_id = p
        acc = await self._acc_for(pool, acc_id)
        if not acc:
            return
        ch_hash = int(await pool.fetchval(
            "SELECT access_hash FROM tg_cloud_storekeepers WHERE acc_id=$1", acc_id) or 0)
        try:
            await _delete_blob(acc, ch_id, ch_hash, msg_id)
        except Exception:
            log.debug("fleet delete: не удалён %s", locator)
