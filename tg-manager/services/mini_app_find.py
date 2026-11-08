"""Поиск по своим объектам: «найти у себя» одной строкой.

В приложении был поиск ФУНКЦИЙ (каталог «Ещё») и поиск ПУБЛИЧНЫХ каналов
Telegram, но не было поиска по своему: чтобы открыть клиента, бота, аккаунт
или канал по имени, нужно было знать, в каком он разделе, и листать там
список. У лидеров (amoCRM, Notion, Linear) это одна строка поиска сверху.

Ручка одна: ``GET /api/miniapp/find?q=`` ищет подстроку без учёта регистра
в контактах, сделках, ботах, аккаунтах, каналах, ИИ-менеджерах и операциях
владельца и
отдаёт по несколько первых совпадений в группе с ЧЕСТНЫМ счётом всех
совпадений группы: «ещё 37» лучше, чем молча оборванный список.

Только свои строки: каждый запрос фильтрует по владельцу. Подстрока
экранируется: ``%`` и ``_`` в запросе — буквы, а не шаблон.
"""
from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)

MIN_QUERY = 2
MAX_QUERY = 64
PER_GROUP = 5

# (ключ, подпись, SQL совпадений, SQL счёта). $1 — владелец, $2 — шаблон ILIKE,
# $3 — сколько строк. В каждом запросе id — добивочный ключ сортировки.
_GROUPS = (
    ("contacts", "👤 Контакты",
     "SELECT id::text AS id, COALESCE(NULLIF(display_name,''), "
     "NULLIF(TRIM(COALESCE(first_name,'') || ' ' || COALESCE(last_name,'')),''), "
     "'@' || username, 'Без имени') AS title, "
     "CONCAT_WS(' · ', CASE WHEN username <> '' THEN '@' || username END, company) AS sub "
     "FROM unified_contacts WHERE owner_id=$1 AND ("
     "display_name ILIKE $2 ESCAPE '\\' OR first_name ILIKE $2 ESCAPE '\\' "
     "OR last_name ILIKE $2 ESCAPE '\\' OR username ILIKE $2 ESCAPE '\\' "
     "OR company ILIKE $2 ESCAPE '\\') "
     "ORDER BY last_changed_at DESC NULLS LAST, id LIMIT $3",
     "SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND ("
     "display_name ILIKE $2 ESCAPE '\\' OR first_name ILIKE $2 ESCAPE '\\' "
     "OR last_name ILIKE $2 ESCAPE '\\' OR username ILIKE $2 ESCAPE '\\' "
     "OR company ILIKE $2 ESCAPE '\\')"),
    # Сделки — главный объект продавца (amoCRM ищет прежде всего их). Ищутся по
    # названию, клиенту и заметке; в подписи этап доски (те же слова, что в
    # колонках канбана, intent_sensor._stage_label).
    ("deals", "💼 Сделки",
     "SELECT id::text AS id, title, CONCAT_WS(' · ', contact, CASE stage "
     "WHEN 'lead' THEN 'Лид' WHEN 'contact' THEN 'Контакт' "
     "WHEN 'proposal' THEN 'Предложение' WHEN 'negotiation' THEN 'Переговоры' "
     "WHEN 'won' THEN 'Выиграно' WHEN 'lost' THEN 'Проиграно' "
     "ELSE stage END) AS sub "
     "FROM crm_deals WHERE owner_id=$1 AND ("
     "title ILIKE $2 ESCAPE '\\' OR contact ILIKE $2 ESCAPE '\\' "
     "OR notes ILIKE $2 ESCAPE '\\') ORDER BY updated_at DESC, id DESC LIMIT $3",
     "SELECT COUNT(*) FROM crm_deals WHERE owner_id=$1 AND ("
     "title ILIKE $2 ESCAPE '\\' OR contact ILIKE $2 ESCAPE '\\' "
     "OR notes ILIKE $2 ESCAPE '\\')"),
    ("bots", "🤖 Боты",
     "SELECT bot_id::text AS id, COALESCE('@' || NULLIF(username,''), first_name, 'Бот') AS title, "
     "first_name AS sub FROM managed_bots WHERE added_by=$1 AND ("
     "username ILIKE $2 ESCAPE '\\' OR first_name ILIKE $2 ESCAPE '\\') "
     "ORDER BY id DESC LIMIT $3",
     "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND ("
     "username ILIKE $2 ESCAPE '\\' OR first_name ILIKE $2 ESCAPE '\\')"),
    ("accounts", "📱 Аккаунты",
     "SELECT id::text AS id, COALESCE(NULLIF(first_name,''), phone) AS title, "
     "CONCAT_WS(' · ', phone, CASE WHEN username <> '' THEN '@' || username END) AS sub "
     "FROM tg_accounts WHERE owner_id=$1 AND ("
     "phone ILIKE $2 ESCAPE '\\' OR first_name ILIKE $2 ESCAPE '\\' "
     "OR username ILIKE $2 ESCAPE '\\') ORDER BY id DESC LIMIT $3",
     "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND ("
     "phone ILIKE $2 ESCAPE '\\' OR first_name ILIKE $2 ESCAPE '\\' "
     "OR username ILIKE $2 ESCAPE '\\')"),
    ("channels", "📢 Каналы",
     "SELECT channel_id::text AS id, COALESCE(NULLIF(title,''), '@' || username, 'Канал') AS title, "
     "CASE WHEN username <> '' THEN '@' || username END AS sub "
     "FROM managed_channels WHERE owner_id=$1 AND ("
     "title ILIKE $2 ESCAPE '\\' OR username ILIKE $2 ESCAPE '\\') "
     "ORDER BY id DESC LIMIT $3",
     "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND ("
     "title ILIKE $2 ESCAPE '\\' OR username ILIKE $2 ESCAPE '\\')"),
    ("personas", "🧑‍💼 ИИ-менеджеры",
     "SELECT id::text AS id, name AS title, NULL AS sub FROM bot_sales_personas "
     "WHERE owner_id=$1 AND name ILIKE $2 ESCAPE '\\' ORDER BY id DESC LIMIT $3",
     "SELECT COUNT(*) FROM bot_sales_personas WHERE owner_id=$1 AND name ILIKE $2 ESCAPE '\\'"),
    ("operations", "⚙️ Операции",
     "SELECT id::text AS id, COALESCE(NULLIF(label,''), op_type) AS title, status AS sub "
     "FROM operation_queue WHERE owner_id=$1 AND label ILIKE $2 ESCAPE '\\' "
     "ORDER BY id DESC LIMIT $3",
     "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND label ILIKE $2 ESCAPE '\\'"),
)
GROUP_KEYS = tuple(g[0] for g in _GROUPS)


def like_pattern(q: str) -> str:
    """Подстрока для ILIKE: %, _ и \\ из запроса — буквы."""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def clean_query(raw) -> str:
    return " ".join(str(raw or "").split())[:MAX_QUERY]


async def find(pool, owner_id: int, q: str, per_group: int = PER_GROUP) -> dict:
    """Совпадения по группам. Группа, чья таблица недоступна, пропускается,
    остальные отдаются: поиск не падает из-за одного модуля."""
    pat = like_pattern(q)
    groups = []
    for key, label, rows_sql, count_sql in _GROUPS:
        try:
            rows = await pool.fetch(rows_sql, owner_id, pat, per_group)
            if not rows:
                continue
            total = int(await pool.fetchval(count_sql, owner_id, pat) or 0)
        except Exception:
            log.warning("find: group %s failed owner=%s", key, owner_id, exc_info=True)
            continue
        items = [{"id": r["id"], "title": (r["title"] or "")[:80],
                  "sub": (r["sub"] or "")[:80]} for r in rows]
        groups.append({"key": key, "label": label, "items": items,
                       "total": max(total, len(items)),
                       "has_more": total > len(items)})
    return {"query": q, "groups": groups,
            "total": sum(g["total"] for g in groups)}


def setup_routes(app: web.Application, pool) -> None:
    from services.mini_app_api import _INTERNAL_ERROR, _err, _get_uid, _json_resp

    async def find_mine(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        q = clean_query(request.query.get("q"))
        if len(q) < MIN_QUERY:
            return _err(f"Введите хотя бы {MIN_QUERY} символа", 400)
        try:
            d = await find(pool, uid, q)
            # Счёт групп и общий итог — в самом ответе: список с потолком,
            # который молчит о нём, выглядел бы всем найденным.
            return _json_resp({"query": d["query"], "groups": d["groups"], "total": d["total"]})
        except Exception:
            log.exception("find_mine uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/find", find_mine)
