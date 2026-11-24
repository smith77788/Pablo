"""Лента уведомлений владельца в мини-аппе (schema_v385).

ЗАЧЕМ. Уведомления продукта жили только как сообщения в личке бота: «операция
завершилась», «аккаунт ограничен», «новый подписчик», «прокси умер». Владелец,
который был не в сети или промотал чат, не мог посмотреть, что произошло: ни
истории, ни счётчика непрочитанного в приложении не было нигде.

ОДНА ДВЕРЬ ЗАПИСИ. Пишет `database/db.py::_feed_record` из `notify_if_enabled`
— ровно там, где уведомление прошло настройки владельца. Здесь только чтение и
отметка прочитанного, поэтому лента не может разойтись с тем, что владелец
получал в бота.

ЧТО ОТДАЁМ. Текст уведомления — HTML для Telegram (<b>, <code>, <a href>).
Экран мини-аппа вставляет его как текст, а не как разметку, поэтому разметку
снимаем ЗДЕСЬ, на сервере: иначе владелец читал бы «&lt;b&gt;Операция&lt;/b&gt;».
Снимаем в одном месте и для ленты, и для будущих потребителей.
"""
from __future__ import annotations

import html as _html
import json as _json
import logging
import re

from aiohttp import web

log = logging.getLogger(__name__)

_PAGE = 30
_MAX_PAGE = 100

# Подписи типов уведомлений ПО-РУССКИ. Экран показывал бы ключ настройки
# («notify_ops», «notify_error») — то же, что печатать имя столбца вместо
# названия поля. Ключи — из database/db.py::get_notification_settings.
PREF_LABELS: dict[str, str] = {
    "notify_ops": "Операции",
    "notify_error": "Ограничения и защита",
    "notify_newuser": "Новые подписчики",
    "notify_position": "Позиции в поиске",
    "notify_leads": "Лиды и заявки",
    "notify_reports": "Отчёты",
    "notify_advice": "Советы",
}
PREF_ICONS: dict[str, str] = {
    "notify_ops": "⚙️",
    "notify_error": "⚠️",
    "notify_newuser": "👤",
    "notify_position": "📈",
    "notify_leads": "🎯",
    "notify_reports": "📋",
    "notify_advice": "💡",
}

_TAG_RE = re.compile(r"<[^>]+>")

# Куда уведомлению позволено вести. Белый список, а не «любая функция из базы»:
# строка ленты живёт месяцами, за это время экран могли переименовать, а
# «открыть» в ленте — это вызов функции фронта по имени. Экран дополнительно
# проверяет, что такая функция есть (defense in depth).
LINK_FNS = frozenset({
    "openOpDetail", "openAccount", "openBot", "openChannel",
    "openContactDetail", "openBroadcastSearch", "openNotificationsFeed",
})


def safe_link(raw) -> dict | None:
    """Дверь строки ленты или None. Чужое имя функции — это не дверь."""
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    fn = str(raw.get("fn") or "")
    if fn not in LINK_FNS:
        return None
    arg = raw.get("arg")
    if isinstance(arg, bool) or not isinstance(arg, (int, str)):
        return None
    return {"fn": fn, "arg": arg if isinstance(arg, int) else str(arg)[:120]}


def plain_text(raw: str) -> str:
    """HTML уведомления → строка для экрана: без тегов и без &amp;.

    Порядок важен: сначала убираем теги, потом разворачиваем сущности. Наоборот
    — и `&lt;b&gt;` из текста самого уведомления превратился бы в тег, который
    следом снимут, то есть кусок текста владельца исчез бы.
    """
    s = _TAG_RE.sub("", str(raw or ""))
    s = _html.unescape(s)
    return s.strip()


def title_and_body(raw: str) -> tuple[str, str]:
    """Первая строка — заголовок, остальное — тело: так уведомления и пишутся."""
    text = plain_text(raw)
    if not text:
        return "Уведомление", ""
    head, _, rest = text.partition("\n")
    return head.strip()[:120], rest.strip()


def setup_routes(app: web.Application, pool) -> None:
    from services.mini_app_api import _err, _get_uid, _json_resp

    def _int(value, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return default

    async def feed_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        limit = _int(request.query.get("limit"), _PAGE, 1, _MAX_PAGE)
        offset = _int(request.query.get("offset"), 0, 0, 100000)
        unread_only = request.query.get("unread") in ("1", "true", "yes")
        pref = (request.query.get("pref") or "").strip()[:64]
        where = ["owner_id = $1"]
        args: list = [uid]
        if unread_only:
            where.append("read_at IS NULL")
        if pref in PREF_LABELS:
            args.append(pref)
            where.append(f"pref = ${len(args)}")
        cond = " AND ".join(where)
        try:
            # +1 строка сверх страницы — честный признак «есть ещё» без второго
            # COUNT по всей ленте.
            rows = await pool.fetch(
                f"SELECT id, pref, text, created_at, read_at, link FROM notification_feed "
                f"WHERE {cond} ORDER BY created_at DESC, id DESC "
                f"LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
                *args, limit + 1, offset)
            unread = await pool.fetchval(
                "SELECT COUNT(*) FROM notification_feed "
                "WHERE owner_id=$1 AND read_at IS NULL", uid)
        except Exception as exc:
            log.warning("notifications: лента не прочитана uid=%s: %s", uid, exc)
            return _err("Не удалось загрузить уведомления", 500)
        has_more = len(rows) > limit
        items = []
        for r in rows[:limit]:
            head, body = title_and_body(r["text"])
            items.append({
                "id": int(r["id"]),
                "pref": r["pref"] or "",
                "kind": PREF_LABELS.get(r["pref"] or "", "Событие"),
                "icon": PREF_ICONS.get(r["pref"] or "", "🔔"),
                "title": head,
                "body": body,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "unread": r["read_at"] is None,
                # Куда ведёт уведомление: «Операция #412 завершена» открывает
                # саму операцию, а не оставляет владельца искать её руками.
                "link": safe_link(r["link"]),
            })
        return _json_resp({
            "items": items,
            "unread": int(unread or 0),
            "page": {"has_more": has_more, "offset": offset, "limit": limit},
            "kinds": [{"key": k, "label": v, "icon": PREF_ICONS.get(k, "🔔")}
                      for k, v in PREF_LABELS.items()],
        })

    async def feed_read(request: web.Request) -> web.Response:
        """Отметить прочитанным: список id или всё (`{"all": true}`).

        Скоуп держит сам ЗАПРОС (`owner_id=$1`), а не проверка в обработчике:
        id уведомления — обычное целое, его перебирают.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            data = {}
        ids = data.get("ids")
        try:
            if data.get("all"):
                await pool.execute(
                    "UPDATE notification_feed SET read_at = now() "
                    "WHERE owner_id=$1 AND read_at IS NULL", uid)
            elif isinstance(ids, list) and ids:
                clean = []
                for v in ids[:_MAX_PAGE]:
                    try:
                        clean.append(int(v))
                    except (TypeError, ValueError):
                        continue
                if not clean:
                    return _err("Нечего отмечать: список id пуст")
                await pool.execute(
                    "UPDATE notification_feed SET read_at = now() "
                    "WHERE owner_id=$1 AND id = ANY($2::bigint[]) AND read_at IS NULL",
                    uid, clean)
            else:
                return _err("Укажите ids или all")
            n = await pool.fetchval(
                "SELECT COUNT(*) FROM notification_feed "
                "WHERE owner_id=$1 AND read_at IS NULL", uid)
        except Exception as exc:
            log.warning("notifications: отметка прочитанного uid=%s: %s", uid, exc)
            return _err("Не удалось отметить прочитанным", 500)
        return _json_resp({"ok": True, "unread": int(n or 0)})

    app.router.add_get("/api/miniapp/notifications", feed_list)
    app.router.add_post("/api/miniapp/notifications/read", feed_read)
