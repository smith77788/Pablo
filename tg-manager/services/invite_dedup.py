"""Одна память «кого уже приглашали в этот канал» для всех дверей инвайта.

Массовый инвайт мини-аппа (`op_worker._exec_mass_invite`) ведёт журнал
`invite_target_log` и не зовёт человека в канал второй раз. Но в канал
приглашают и другие двери — инвайт из карточки канала в боте и инвайт
контактов в канал (`bot/handlers/channel_ops.py`). Они звали
`account_manager.invite_users_to_channel` напрямую, журнал не читали и не
пополняли: человек, уже приглашённый массовым инвайтом, получал из бота
второе приглашение, и наоборот.

Здесь — общие правила для всех дверей: по каким ключам канал записан в
журнале, кого из списка ещё можно звать и как записать приглашённых.
Хранилище и запись — те же, что у массового инвайта (op_worker), второго
источника правды не заводим.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def dedup_keys(group_ref: str = "", channel_id=None, username: str = "") -> list[str]:
    """Ключи, под которыми канал живёт в журнале приглашений.

    Один канал приходит в разные двери по-разному: @username, ссылкой-
    приглашением, числовым id. Пишем под всеми известными формами, а читаем
    любую из них — тогда приглашённого видит каждая дверь.
    """
    from services.mass_inviter_engine import parse_group_ref

    keys: list[str] = []
    for ref in (group_ref, f"@{str(username).lstrip('@')}" if username else ""):
        if ref:
            k = parse_group_ref(str(ref))
            if k and k not in keys:
                keys.append(k)
    if channel_id:
        try:
            cid = str(abs(int(channel_id)))
            if cid.startswith("100") and len(cid) > 12:
                cid = cid[3:]          # -100… → голый id канала
            if cid not in keys:
                keys.append(cid)
        except (TypeError, ValueError):
            pass
    return keys


async def invited_keys(pool, owner_id: int, keys: list[str]) -> set[str]:
    """Кого уже приглашали в канал (под любым из ключей). Ключи сравнения — lower."""
    if not keys:
        return set()
    from services.contact_opt_out import compare_key
    from services.op_worker import _INVITE_LOG_DDL

    await pool.execute(_INVITE_LOG_DDL)
    rows = await pool.fetch(
        "SELECT target FROM invite_target_log WHERE owner_id=$1 AND group_key = ANY($2::text[])",
        owner_id, list(keys))
    return {compare_key(r["target"]) for r in (rows or [])}


async def filter_new(pool, owner_id: int, keys: list[str], refs: list) -> tuple[list, int, int]:
    """Оставить тех, кого в этот канал ещё не звали и кто не просил не звать.

    Возвращает (кого звать, пропущено уже приглашённых, пропущено из реестра
    «не приглашать»). Повторы внутри самого списка тоже убираются. Сбой чтения
    журнала — список как есть (так же ведёт себя массовый инвайт: лучше не
    сорвать приглашение, чем молча не пригласить никого).
    """
    from services import contact_opt_out as coo

    try:
        already = await invited_keys(pool, owner_id, keys)
    except Exception:
        log.warning("invite_dedup: журнал приглашений не прочитан", exc_info=True)
        already = set()
    try:
        opted = {coo.compare_key(t) for t in await coo.load_opted_out(pool, owner_id)}
    except Exception:
        opted = set()
    out: list = []
    seen: set = set()
    dup = opt = 0
    for r in refs:
        k = coo.compare_key(str(r))
        if k in opted:
            opt += 1
            continue
        if k in already:
            dup += 1
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out, dup, opt


async def remember(pool, owner_id: int, keys: list[str], refs, op_id=None) -> bool:
    """Записать приглашённых под всеми ключами канала. True — запись подтверждена."""
    from services.op_worker import _record_invited_targets

    targets = [str(r) for r in (refs or []) if r]
    if not targets or not keys:
        return True
    ok = True
    for k in keys:
        ok = await _record_invited_targets(pool, owner_id, k, op_id, targets) and ok
    return ok
