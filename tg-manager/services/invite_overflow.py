"""Переливание инвайта по резерву каналов.

Сценарий владельца: выбрать канал, задать его оформление (описание, пост),
выбрать аудиторию и аккаунты, запустить. Когда канал упирается в лимит
приглашённых или ловит флуд, Infragram берёт следующий ПУСТОЙ канал из
резерва, оформляет его так же и приглашает туда оставшихся, и так по кругу,
пока аудитория не кончится или не кончится резерв.

Здесь только то, что не зависит от цикла инвайта: учёт цепочки каналов в
базе, выбор следующего канала и его оформление. Сам переход (права
инвайтеров, ссылка, счётчики) делает исполнитель `op_worker._exec_mass_invite`.

Цепочка — это все каналы одной кампании: главный и взятые из резерва. Ключ
цепочки — нормализованная ссылка ГЛАВНОГО канала (тот же ключ, что у дедупа
инвайта), поэтому продолжение на следующий день начинает с того канала, на
котором остановились, а не снова с переполненного главного.
"""
from __future__ import annotations

import logging

from services import account_manager

log = logging.getLogger(__name__)

# Предохранитель: сколько каналов резерва один прогон вправе взять. Без потолка
# системная беда (например, все аккаунты без прав или неверная классификация
# ошибки) за один прогон пролистала бы весь резерв владельца.
MAX_SWITCHES_PER_RUN = 10

# Сколько каналов можно положить в резерв одной операции.
MAX_RESERVE = 50

# Статусы канала в цепочке.
ST_ACTIVE = "active"   # сейчас в него приглашаем
ST_FULL = "full"       # упёрся в лимит приглашённых — больше не берём
ST_BURNED = "burned"   # закрыт / флуд / недоступен

_LIMITS = {"title": 128, "about": 255, "post": 4000}


def clean_setup(raw) -> dict:
    """Оформление канала из запроса: только известные поля, с потолками Telegram.

    Пустой словарь — оформлять нечего (канал берётся как есть).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, cap in _LIMITS.items():
        val = str(raw.get(key) or "").strip()
        if val:
            out[key] = val[:cap]
    if out.get("post") and str(raw.get("pin", True)).lower() not in ("0", "false", "no", "off"):
        out["pin"] = True
    return out


def clean_reserve_ids(raw) -> list[int]:
    """Список id каналов резерва: целые, без повторов, порядок оператора."""
    out: list[int] = []
    for x in (raw or []) if isinstance(raw, (list, tuple)) else []:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v and v not in out:
            out.append(v)
    return out[:MAX_RESERVE]


async def chain_rows(pool, owner_id: int, chain_key: str) -> list[dict]:
    """Каналы цепочки в порядке подключения."""
    rows = await pool.fetch(
        "SELECT channel_ref, channel_id, status, prepared, invited_ok "
        "FROM invite_overflow_channels WHERE owner_id=$1 AND chain_key=$2 "
        "ORDER BY id", owner_id, chain_key)
    return [dict(r) for r in (rows or [])]


async def busy_channel_ids(pool, owner_id: int, chain_key: str) -> set[int]:
    """Каналы резерва, которые уже НЕ пустые для этой цепочки.

    Канал, взятый другой кампанией, пустым уже не считается — в него
    приглашали другую аудиторию. Канал этой же цепочки, который ещё active,
    свободен (на нём и продолжаем); full/burned — нет.
    """
    rows = await pool.fetch(
        "SELECT channel_id, chain_key, status FROM invite_overflow_channels "
        "WHERE owner_id=$1 AND channel_id IS NOT NULL", owner_id)
    busy: set[int] = set()
    for r in rows or []:
        if r["chain_key"] != chain_key or r["status"] != ST_ACTIVE:
            busy.add(int(r["channel_id"]))
    return busy


async def taken_elsewhere(pool, owner_id: int, chain_key: str, channel_id: int) -> bool:
    """Канал уже взят ДРУГОЙ кампанией (после старта этого прогона).

    Список занятых читается на старте, а прогон идёт часами: параллельная
    кампания владельца могла за это время взять тот же канал резерва. Проверка
    прямо перед оформлением сужает гонку до мгновения. Сбой — считаем занятым:
    лучше пропустить канал, чем пригласить в него две аудитории.
    """
    try:
        row = await pool.fetchrow(
            "SELECT 1 FROM invite_overflow_channels WHERE owner_id=$1 AND channel_id=$2 "
            "AND chain_key<>$3 LIMIT 1", owner_id, int(channel_id), chain_key)
        return row is not None
    except Exception:
        log.warning("invite_overflow: проверка занятости канала не удалась", exc_info=True)
        return True


async def mark(pool, owner_id: int, chain_key: str, channel_ref: str,
               channel_id: int | None, status: str, *, prepared: bool | None = None,
               ok_delta: int = 0, reason: str = "", op_id: int | None = None) -> None:
    """Записать состояние канала в цепочке. Никогда не бросает."""
    try:
        await pool.execute(
            "INSERT INTO invite_overflow_channels(owner_id, chain_key, channel_ref, "
            "channel_id, status, prepared, invited_ok, reason, op_id) "
            "VALUES($1,$2,$3,$4,$5,COALESCE($6,FALSE),$7,$8,$9) "
            "ON CONFLICT (owner_id, chain_key, channel_ref) DO UPDATE SET "
            "status=EXCLUDED.status, "
            "prepared=COALESCE($6, invite_overflow_channels.prepared), "
            "invited_ok=invite_overflow_channels.invited_ok+EXCLUDED.invited_ok, "
            "reason=CASE WHEN EXCLUDED.reason<>'' THEN EXCLUDED.reason "
            "ELSE invite_overflow_channels.reason END, "
            "op_id=COALESCE(EXCLUDED.op_id, invite_overflow_channels.op_id), "
            "updated_at=now()",
            owner_id, chain_key, channel_ref[:300], channel_id, status, prepared,
            int(ok_delta), (reason or "")[:200], op_id)
    except Exception:
        log.warning("invite_overflow: состояние канала не записано", exc_info=True)


async def load_reserve(pool, owner_id: int, channel_ids: list[int]) -> list[dict]:
    """Каналы резерва владельца в порядке, заданном оператором.

    Только свои каналы (managed_channels.owner_id) с привязанным аккаунтом —
    оформлять и приглашать в канал можно только его админом.
    """
    if not channel_ids:
        return []
    rows = await pool.fetch(
        "SELECT DISTINCT ON (channel_id) channel_id, access_hash, username, title, acc_id "
        "FROM managed_channels WHERE owner_id=$1 AND channel_id=ANY($2::bigint[]) "
        "AND acc_id IS NOT NULL ORDER BY channel_id, is_creator DESC NULLS LAST",
        owner_id, list(channel_ids))
    by_id = {int(r["channel_id"]): dict(r) for r in (rows or [])}
    return [by_id[c] for c in channel_ids if c in by_id]


async def prepare_channel(acc: dict, ch: dict, setup: dict) -> dict:
    """Оформить канал и выдать ссылку, по которой в него приглашают.

    Возвращает {"ok": True, "ref": str, "done": [...], "warnings": [...]} либо
    {"ok": False, "error": str}. Сбой оформления не делает канал негодным:
    пригласить людей можно и без описания, оператор увидит предупреждение.
    Негоден канал, только если на него нельзя получить ссылку.
    """
    sess = acc.get("session_str")
    if not sess:
        return {"ok": False, "error": "у админа канала нет сессии"}
    cid = int(ch["channel_id"])
    ah = int(ch.get("access_hash") or 0)
    uname = str(ch.get("username") or "").lstrip("@")
    done: list[str] = []
    warnings: list[str] = []
    if setup.get("title"):
        try:
            if await account_manager.edit_channel_title(
                    sess, cid, setup["title"], _acc=dict(acc), access_hash=ah, username=uname):
                done.append("название")
            else:
                warnings.append("название не изменено")
        except Exception as e:
            warnings.append(f"название: {str(e)[:60]}")
    if setup.get("about"):
        try:
            if await account_manager.edit_channel_about(
                    sess, cid, setup["about"], _acc=dict(acc), access_hash=ah, username=uname):
                done.append("описание")
            else:
                warnings.append("описание не изменено")
        except Exception as e:
            warnings.append(f"описание: {str(e)[:60]}")
    if setup.get("post"):
        try:
            res = await account_manager.post_to_channel(
                sess, cid, setup["post"], access_hash=ah, username=uname, _acc=dict(acc))
        except Exception as e:
            res = {"error": str(e)[:80]}
        if res.get("msg_id"):
            done.append("пост")
            if setup.get("pin"):
                try:
                    pr = await account_manager.pin_last_channel_post(
                        sess, cid, access_hash=ah, username=uname, _acc=dict(acc))
                    if pr.get("pinned_msg_id"):
                        done.append("закреп")
                    else:
                        warnings.append(f"закреп: {str(pr.get('error') or '')[:60]}")
                except Exception as e:
                    warnings.append(f"закреп: {str(e)[:60]}")
        else:
            warnings.append(f"пост: {str(res.get('error') or '')[:60]}")

    if uname:
        ref = "@" + uname
    else:
        try:
            ref = await account_manager.get_channel_invite_link(
                sess, cid, _acc=dict(acc), access_hash=ah)
        except Exception as e:
            log.warning("invite_overflow: ссылка канала %s не получена: %s", cid, e)
            ref = ""
        if not ref:
            return {"ok": False, "error": "не удалось получить ссылку канала "
                                          "(аккаунт должен быть его админом)"}
    return {"ok": True, "ref": ref, "done": done, "warnings": warnings}


async def reserve_options(pool, owner_id: int, chain_key: str) -> list[dict]:
    """Свои каналы для резерва — с ответом, возьмёт ли их исполнитель и почему нет.

    Раньше мини-апп показывал все свои каналы одинаково, а исполнитель молча
    пропускал занятые другой кампанией, уже заполненные или выбывшие в этой
    цепочке и каналы, чей аккаунт-админ недоступен. Оператор отмечал пять
    каналов резерва, а работало два — и узнавал об этом только по итогу.

    Правило занятости — то же, что у `busy_channel_ids`.
    """
    rows = await pool.fetch(
        "SELECT DISTINCT ON (mc.channel_id) mc.channel_id, mc.title, mc.username, "
        "mc.members_count, mc.is_admin, mc.is_creator, a.is_active, a.acc_status, "
        "(a.session_str IS NOT NULL) AS has_session "
        "FROM managed_channels mc LEFT JOIN tg_accounts a ON a.id = mc.acc_id "
        "WHERE mc.owner_id=$1 ORDER BY mc.channel_id, mc.is_creator DESC NULLS LAST",
        owner_id)
    ov = await pool.fetch(
        "SELECT channel_id, chain_key, status, reason FROM invite_overflow_channels "
        "WHERE owner_id=$1 AND channel_id IS NOT NULL", owner_id)
    used: dict[int, str] = {}
    for r in ov or []:
        cid = int(r["channel_id"])
        if r["chain_key"] != chain_key:
            used.setdefault(cid, "уже взят другой кампанией — в нём другая аудитория")
        elif r["status"] == ST_FULL:
            used[cid] = "заполнен в прошлых прогонах этой кампании"
        elif r["status"] == ST_BURNED:
            used[cid] = "выбыл в этой кампании" + (f": {r['reason']}" if r["reason"] else "")
    out: list[dict] = []
    for r in rows or []:
        cid = int(r["channel_id"])
        why = used.get(cid, "")
        if not why:
            if r["is_active"] is False or not r["has_session"] or \
                    str(r["acc_status"] or "active") in ("banned", "deactivated",
                                                         "session_expired"):
                why = "аккаунт-админ канала недоступен"
            elif r["is_admin"] is False and not r["is_creator"]:
                why = "привязанный аккаунт не админ канала"
        out.append({"channel_id": cid, "title": r["title"] or "",
                    "username": r["username"] or "",
                    "member_count": int(r["members_count"] or 0),
                    "usable": not why, "reason": why})
    return out
