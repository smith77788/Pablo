"""Поиск по своим данным: один запрос вместо шести экранов.

ЗАЧЕМ. Поиск в мини-аппе был у каждого списка свой, и чтобы найти «тот
аккаунт», «того бота» или «ту рассылку», надо было вспомнить, в каком разделе
искать, дойти до него и набрать запрос там. Экран «🔎 Поиск Telegram» ищет
ЧУЖОЕ — публичные каналы и ботов; по своему хозяйству поиска не было нигде.
Здесь он есть: номер телефона, ник, название канала, имя контакта, слово из
рассылки или название операции — один запрос, ответ группами.

КУДА ВЕДЁТ НАЙДЕННОЕ. Каждая находка несёт имя функции экрана и её аргумент
(`fn`/`arg`) — тот же приём, которым уже присылаются быстрые действия по итогу
операции. Экран зовёт функцию по имени и ПРОВЕРЯЕТ, что такая есть: иначе
переименование на фронте превращало бы находку в кнопку, которая не делает
ничего.

СКОУП — В ЗАПРОСЕ, И ОТДЕЛЬНО У КАЖДОЙ ГРУППЫ. Шесть запросов по шести
таблицам: `owner_id`/`created_by` стоит в каждом, а не в обработчике. Без
этого поиск по «+7999» отдавал бы чужие телефоны — ровно тот класс, которым
уже дырявилась карточка-360 (CLAUDE.md).

ГРУППА ПАДАЕТ ОТДЕЛЬНО. Поиск по шести таблицам — шесть поводов упасть;
упавшая группа отдаёт пусто и не роняет остальные: «ничего не найдено в
аккаунтах» для владельца хуже, чем «поиск не работает», только если
соврать — поэтому у ответа есть `failed`, и экран честно говорит, где не
искал.
"""
from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)

_PER_GROUP = 6
_MAX_PER_GROUP = 25
_MAX_Q = 100


def like(q: str) -> str:
    """Шаблон для ILIKE. `%` и `_` в запросе — это символы, а не джокеры.

    Без экранирования запрос «_» совпадал бы с ЛЮБОЙ строкой, а «100%» искал
    бы «100» и что угодно после — то есть поиск врал бы молча.
    """
    s = str(q or "")
    for ch in ("\\", "%", "_"):
        s = s.replace(ch, "\\" + ch)
    return f"%{s}%"


# Группа: (ключ, подпись, значок, SQL, как собрать строку результата).
# SQL отдаёт поля с именами title/subtitle/arg — сборка одна на все группы.
_GROUPS: tuple[tuple[str, str, str, str, str], ...] = (
    ("accounts", "Аккаунты", "📱", """
        SELECT id AS arg,
               COALESCE(NULLIF(first_name,''), NULLIF('@'||username,'@'), phone,
                        'Аккаунт #'||id) AS title,
               COALESCE(phone,'') AS subtitle
          FROM tg_accounts
         WHERE owner_id = $1
           AND (phone ILIKE $2 ESCAPE '\\' OR first_name ILIKE $2 ESCAPE '\\'
                OR username ILIKE $2 ESCAPE '\\')
         ORDER BY id DESC LIMIT $3
    """, "openAccount"),
    ("bots", "Боты", "🤖", """
        SELECT bot_id AS arg,
               COALESCE(NULLIF('@'||username,'@'), NULLIF(first_name,''),
                        'Бот #'||bot_id) AS title,
               COALESCE(NULLIF(first_name,''), '') AS subtitle
          FROM managed_bots
         WHERE added_by = $1
           AND (username ILIKE $2 ESCAPE '\\' OR first_name ILIKE $2 ESCAPE '\\'
                OR note ILIKE $2 ESCAPE '\\')
         ORDER BY added_at DESC NULLS LAST LIMIT $3
    """, "openBot"),
    ("channels", "Каналы и чаты", "📡", """
        SELECT id AS arg,
               COALESCE(NULLIF(title,''), NULLIF('@'||username,'@'),
                        'Канал #'||id) AS title,
               COALESCE(NULLIF('@'||username,'@'), '') AS subtitle
          FROM managed_channels
         WHERE owner_id = $1
           AND (title ILIKE $2 ESCAPE '\\' OR username ILIKE $2 ESCAPE '\\')
         ORDER BY COALESCE(members_count,0) DESC LIMIT $3
    """, "openChannel"),
    ("contacts", "Контакты", "📇", """
        SELECT id::text AS arg,
               COALESCE(NULLIF(display_name,''),
                        NULLIF(TRIM(COALESCE(first_name,'')||' '||COALESCE(last_name,'')),''),
                        NULLIF('@'||username,'@'), 'Контакт') AS title,
               COALESCE(NULLIF(company,''), NULLIF('@'||username,'@'), '') AS subtitle
          FROM unified_contacts
         WHERE owner_id = $1
           AND (display_name ILIKE $2 ESCAPE '\\' OR first_name ILIKE $2 ESCAPE '\\'
                OR last_name ILIKE $2 ESCAPE '\\' OR username ILIKE $2 ESCAPE '\\'
                OR company ILIKE $2 ESCAPE '\\' OR phones::text ILIKE $2 ESCAPE '\\')
         ORDER BY is_favorite DESC NULLS LAST, id DESC LIMIT $3
    """, "openContactDetail"),
    ("broadcasts", "Рассылки", "📢", """
        SELECT b.id AS arg,
               LEFT(REGEXP_REPLACE(COALESCE(b.message_text,''), '\\s+', ' ', 'g'), 70) AS title,
               COALESCE(NULLIF('@'||mb.username,'@'), '') || ' · ' || b.status AS subtitle
          FROM broadcasts b
          JOIN managed_bots mb ON mb.bot_id = b.bot_id
         WHERE b.created_by = $1 AND b.message_text ILIKE $2 ESCAPE '\\'
         ORDER BY b.created_at DESC LIMIT $3
    """, "openBroadcastSearch"),
    ("operations", "Операции", "⚙️", """
        SELECT id AS arg,
               COALESCE(NULLIF(label,''), op_type) AS title,
               status AS subtitle
          FROM operation_queue
         WHERE owner_id = $1
           AND (label ILIKE $2 ESCAPE '\\' OR op_type ILIKE $2 ESCAPE '\\')
         ORDER BY id DESC LIMIT $3
    """, "openOpDetail"),
)


async def search_all(pool, uid: int, q: str, per_group: int = _PER_GROUP) -> dict:
    """Находки группами. Упавшая группа — пустая группа, а не упавший поиск."""
    pattern = like(q)
    groups: list[dict] = []
    failed: list[str] = []
    found = 0
    for key, label, icon, sql, fn in _GROUPS:
        try:
            rows = await pool.fetch(sql, uid, pattern, per_group)
        except Exception as exc:
            log.warning("global search: группа %s uid=%s: %s", key, uid, str(exc)[:160])
            failed.append(label)
            continue
        items = [{
            "title": (r["title"] or "").strip() or "Без названия",
            "subtitle": (r["subtitle"] or "").strip(),
            "fn": fn,
            # У рассылки своего экрана нет: находка ведёт в «Сообщения» с ТЕМ
            # ЖЕ запросом в поиске истории, то есть в список найденного, а не
            # в строку, которую владельцу пришлось бы искать там заново.
            "arg": q if key == "broadcasts" else r["arg"],
        } for r in rows]
        found += len(items)
        if items:
            groups.append({"key": key, "label": label, "icon": icon, "items": items})
    return {"q": q, "groups": groups, "found": found, "failed": failed,
            "per_group": per_group,
            # Есть ли смысл в «показать ещё»: хоть одна группа упёрлась в потолок.
            "capped": any(len(g["items"]) >= per_group for g in groups)}


def setup_routes(app: web.Application, pool) -> None:
    from services.mini_app_api import _err, _get_uid, _json_resp

    async def search_everything(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        q = (request.query.get("q") or "").strip()[:_MAX_Q]
        if len(q) < 2:
            return _err("Введите хотя бы два символа")
        # «Показать ещё» просит ту же выдачу шире, а не вторую страницу: групп
        # шесть, и страница у каждой своя — «ещё» по одной из них означало бы
        # шесть счётчиков смещения на экране ради редкого случая.
        try:
            per = max(_PER_GROUP, min(_MAX_PER_GROUP, int(request.query.get("per") or 0)))
        except (TypeError, ValueError):
            per = _PER_GROUP
        return _json_resp(await search_all(pool, uid, q, per))

    app.router.add_get("/api/miniapp/search/everything", search_everything)
