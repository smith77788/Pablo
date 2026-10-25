"""Переходы с канала-витрины на основной канал — по каждому посту.

Администратор раньше учился только на просмотрах, реакциях и пересылках, а
задача витрины другая: приводить людей на основной ресурс. Если «куда вести
клиентов» указывает на канал или группу владельца, который тоже заведён в
Infragram, в посте вместо @основного ставится своя ссылка-приглашение этого
поста. Telegram сам считает вступления по каждой ссылке, поэтому это реальные
новые подписчики основного канала, а не клики.

Сбой здесь никогда не мешает публикации: нет прав на приглашения, канал не
заведён, флуд — пост уходит с исходным текстом.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

# Сколько дней после публикации досчитываем вступления по ссылке поста.
TRACK_DAYS = 30
# Ссылок за один заход чтения на основной канал (одно подключение аккаунта).
_REFRESH_BATCH = 60

_TME_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/([A-Za-z0-9_]{4,32})/?$",
    re.IGNORECASE)
_AT_RE = re.compile(r"^@([A-Za-z0-9_]{4,32})$")


def target_username(contact: Any) -> Optional[str]:
    """@username основного ресурса из «куда вести клиентов» или None.

    Понимает @name, t.me/name и https://t.me/name. Приватные приглашения
    (t.me/+…, joinchat), сайты и телефоны — None: по ним вступления в
    канал не посчитать."""
    c = str(contact or "").strip()
    if not c or " " in c:
        return None
    m = _AT_RE.match(c) or _TME_RE.match(c)
    if not m:
        return None
    name = m.group(1)
    if name.lower() in ("joinchat", "addlist", "share", "proxy", "socks"):
        return None
    return name


def _variants_re(uname: str) -> re.Pattern:
    u = re.escape(uname)
    # Длинные формы первыми, чтобы https://t.me/name не превратилось в
    # https://<ссылка>. @name — только целым словом: @name2 не трогаем.
    return re.compile(
        rf"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/{u}\b/?|(?<![\w@])@{u}\b",
        re.IGNORECASE)


def mentions_target(text: str, uname: str) -> bool:
    return bool(uname and text and _variants_re(uname).search(text))


def apply_link(text: str, uname: str, link: str) -> str:
    """Заменить упоминания основного канала в тексте на ссылку поста."""
    if not (text and uname and link):
        return text
    return _variants_re(uname).sub(link, text)


async def resolve_target(pool, owner_id: int, channel_id: int, uname: str) -> Optional[dict]:
    """Основной канал владельца по @username среди его каналов в Infragram."""
    row = await pool.fetchrow(
        "SELECT channel_id FROM managed_channels WHERE owner_id=$1 "
        "AND lower(username)=lower($2) AND channel_id<>$3 LIMIT 1",
        int(owner_id), uname, int(channel_id))
    return {"channel_id": int(row["channel_id"])} if row else None


async def prepare(pool, owner_id: int, channel_id: int, text: str,
                  pillar: Optional[str]) -> tuple[str, Optional[int]]:
    """Подставить в пост ссылку-приглашение поста. Возвращает (текст, id ссылки).

    Ссылка создаётся, только если пост действительно ведёт в основной канал:
    упоминает его, а сам канал заведён в Infragram и не совпадает с витриной."""
    from services import channel_admin as ca

    admin = await ca.get_admin(pool, owner_id, channel_id) or {}
    uname = target_username(admin.get("lead_contact"))
    if not uname or not mentions_target(text, uname):
        return text, None
    target = await resolve_target(pool, owner_id, channel_id, uname)
    if not target:
        return text, None
    acc = await ca.channel_account(pool, owner_id, target["channel_id"])
    if not acc:
        return text, None
    row = await pool.fetchrow(
        "INSERT INTO va_tracked_links(owner_id, channel_id, target_channel_id, pillar) "
        "VALUES($1,$2,$3,$4) RETURNING id",
        int(owner_id), int(channel_id), int(target["channel_id"]), pillar or None)
    link_id = int(row["id"])
    from services import account_manager
    link = await account_manager.export_tracked_invite(
        acc["session_str"], int(target["channel_id"]),
        title=f"Витрина {link_id}", _acc=acc,
        access_hash=int(acc.get("access_hash") or 0), username=acc.get("username") or "")
    if not link:
        await pool.execute("DELETE FROM va_tracked_links WHERE id=$1", link_id)
        return text, None
    await pool.execute("UPDATE va_tracked_links SET link=$2 WHERE id=$1", link_id, link)
    return apply_link(text, uname, link), link_id


async def attach_op(pool, link_id: Optional[int], op_id: int) -> None:
    if link_id:
        await pool.execute("UPDATE va_tracked_links SET op_id=$2 WHERE id=$1",
                           int(link_id), int(op_id))


async def drop(pool, link_id: Optional[int]) -> None:
    """Пост со ссылкой не ушёл в очередь — запись ссылки не нужна."""
    if link_id:
        await pool.execute("DELETE FROM va_tracked_links WHERE id=$1 AND op_id IS NULL",
                           int(link_id))


async def refresh(pool, owner_id: int, channel_id: int) -> int:
    """Перечитать вступления по ссылкам витрины. Возвращает число обновлённых."""
    from services import account_manager
    from services import channel_admin as ca

    rows = await pool.fetch(
        "SELECT id, target_channel_id, link FROM va_tracked_links "
        "WHERE owner_id=$1 AND channel_id=$2 AND link IS NOT NULL "
        "AND created_at > now() - make_interval(days => $3) "
        "ORDER BY created_at DESC LIMIT $4",
        int(owner_id), int(channel_id), TRACK_DAYS, _REFRESH_BATCH)
    by_target: dict[int, list[dict]] = {}
    for r in rows or []:
        by_target.setdefault(int(r["target_channel_id"]), []).append(dict(r))
    updated = 0
    for target_id, items in by_target.items():
        acc = await ca.channel_account(pool, owner_id, target_id)
        if not acc:
            continue
        got = await account_manager.read_invite_usage(
            acc["session_str"], target_id, [i["link"] for i in items], _acc=acc,
            access_hash=int(acc.get("access_hash") or 0), username=acc.get("username") or "")
        for i in items:
            u = got.get(i["link"])
            if u is None:
                continue
            await pool.execute(
                "UPDATE va_tracked_links SET joins=$2, requests=$3, checked_at=now() WHERE id=$1",
                i["id"], int(u["joins"]), int(u["requests"]))
            updated += 1
    return updated


async def summary(pool, owner_id: int, channel_id: int, days: int = 7) -> dict:
    """Вступления в основной канал с постов витрины за период и по рубрикам."""
    rows = await pool.fetch(
        "SELECT pillar, count(*) AS links, sum(joins) AS joins, sum(requests) AS requests "
        "FROM va_tracked_links WHERE owner_id=$1 AND channel_id=$2 AND link IS NOT NULL "
        "AND op_id IS NOT NULL AND created_at > now() - make_interval(days => $3) "
        "GROUP BY pillar", int(owner_id), int(channel_id), int(days))
    by_pillar: dict[str, int] = {}
    links = joins = requests = 0
    for r in rows or []:
        links += int(r["links"] or 0)
        joins += int(r["joins"] or 0)
        requests += int(r["requests"] or 0)
        if r["pillar"]:
            by_pillar[r["pillar"]] = by_pillar.get(r["pillar"], 0) + int(r["joins"] or 0)
    return {"links": links, "joins": joins, "requests": requests, "by_pillar": by_pillar}
