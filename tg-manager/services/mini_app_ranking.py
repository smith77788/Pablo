"""Позиции в поиске Telegram: слежение за ключевыми словами.

Постановка слов на слежение, история позиций, сводка и оповещения. Вся работа
идёт через `services/ranking_engine.py`, который обработчики импортируют у себя;
на соседей по `mini_app_api` группа не завязана вовсе.

Вынесено из `services/mini_app_api.py`: тот файл — больше 23 тысяч строк и 926
маршрутов в одной функции, искать в нём дороже, чем править.

Что маршруты не потерялись, стережёт слепок в
`tests/test_miniapp_routes_are_not_lost.py`.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Регистрирует маршруты на общем приложении мини-аппа."""
    # Общие помощники живут в mini_app_api, а он импортирует этот модуль —
    # импорт сверху дал бы цикл. К моменту вызова mini_app_api уже загружен.
    from services.mini_app_api import _err, _get_uid, _json_resp, log

    async def ranking_track(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.ranking_engine import track_keyword
            # Модель подсистемы — «бот + ключевое слово». Прежние channel_id и
            # check_interval не существовали ни в схеме, ни в форме экрана.
            result = await track_keyword(pool, uid, data.get('keyword', ''),
                                         data.get('bot_id'),
                                         data.get('region') or 'ru')
            if not result.get('ok'):
                return _err(result.get('error') or 'Не удалось добавить', 400)
            return _json_resp(result)
        except Exception:
            log.exception("ranking_track uid=%s", uid)
            return _err("Не удалось поставить слово на слежение", 500)

    async def ranking_untrack(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.ranking_engine import untrack_keyword
            try:
                kw_id = int(data.get('keyword_id') or data.get('id') or 0)
            except (TypeError, ValueError):
                return _err('Некорректный keyword_id', 400)
            if not kw_id:
                return _err('Укажите keyword_id', 400)
            result = await untrack_keyword(pool, uid, kw_id)
            if not result.get('ok'):
                return _err('Ключевое слово не найдено', 404)
            return _json_resp(result)
        except Exception:
            log.exception("ranking_untrack uid=%s", uid)
            return _err("Не удалось снять слово со слежения", 500)

    async def ranking_record(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.ranking_engine import record_position
            try:
                kw_id = int(data.get('keyword_id') or 0)
            except (TypeError, ValueError):
                return _err('Некорректный keyword_id', 400)
            if not kw_id:
                return _err('Укажите keyword_id', 400)
            pos = data.get('position')
            result = await record_position(pool, uid, kw_id,
                                           int(pos) if pos is not None else None)
            if not result.get('ok'):
                return _err(result.get('error') or 'Не удалось записать', 400)
            return _json_resp(result)
        except Exception:
            log.exception("ranking_record uid=%s", uid)
            return _err("Не удалось записать позицию", 500)

    async def ranking_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            kw_id = int(request.match_info.get('keyword_id', 0))
            limit = max(1, min(int(request.query.get('limit', 30)), 365))
            from services.ranking_engine import get_position_history
            history = await get_position_history(pool, uid, kw_id, limit)
            return _json_resp({'history': history})
        except Exception:
            log.exception("ranking_history uid=%s", uid)
            return _err("Не удалось загрузить историю позиций", 500)

    async def ranking_positions(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.ranking_engine import get_all_positions
            positions = await get_all_positions(pool, uid)
            return _json_resp({'positions': positions})
        except Exception:
            log.exception("ranking_positions uid=%s", uid)
            return _err("Не удалось загрузить текущие позиции", 500)

    async def ranking_keywords(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.ranking_engine import get_tracked_keywords
            keywords = await get_tracked_keywords(pool, uid)
            return _json_resp({'keywords': keywords})
        except Exception:
            log.exception("ranking_keywords uid=%s", uid)
            return _err("Не удалось загрузить слова на слежении", 500)

    async def ranking_overview(request: web.Request) -> web.Response:
        """Свод для экрана «Рейтинг»: ключи + последние позиции + оповещения.

        Форма ответа — ровно та, что читает фронт (renderRankingList):
        keyword, position, trend, region, bot_username, last_checked.
        trend = предыдущая позиция минус текущая (положительный = рост).
        """
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.ranking_engine import (
                get_tracked_keywords, get_all_positions, get_alerts,
                get_history_for_all)
            tracked = await get_tracked_keywords(pool, uid)
            positions = {p["keyword_id"]: p for p in await get_all_positions(pool, uid)}
            alerts = await get_alerts(pool, uid)
            # История нужна графику позиций. Без неё фронт отфильтровывал все
            # ключи (`k.history && k.history.length > 1`) и холст не рисовался
            # НИ РАЗУ, сколько бы замеров ни накопилось.
            history = await get_history_for_all(pool, uid)
            keywords = []
            for k in tracked:
                p = positions.get(k["id"]) or {}
                cur, prev = p.get("position"), p.get("previous_position")
                last = p.get("last_checked")
                keywords.append({
                    "id": k["id"],
                    "keyword": k.get("keyword"),
                    "bot_id": k.get("bot_id"),
                    "bot_username": k.get("bot_username"),
                    "position": cur,
                    "trend": (prev - cur) if (cur is not None and prev is not None) else 0,
                    "region": k.get("region") or "ru",
                    "last_checked": last.isoformat() if last else None,
                    "history": history.get(k["id"], []),
                })
            return _json_resp({"keywords": keywords, "alerts": alerts})
        except Exception:
            log.exception("ranking_overview uid=%s", uid)
            return _err("Не удалось загрузить сводку по позициям", 500)

    async def ranking_alerts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.ranking_engine import get_alerts
            alerts = await get_alerts(pool, uid)
            return _json_resp({'alerts': alerts})
        except Exception:
            log.exception("ranking_alerts uid=%s", uid)
            return _err("Не удалось загрузить оповещения о позициях", 500)

    async def ranking_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.ranking_engine import get_ranking_stats
            stats = await get_ranking_stats(pool, uid)
            return _json_resp(stats)
        except Exception:
            log.exception("ranking_stats uid=%s", uid)
            return _err("Не удалось загрузить статистику позиций", 500)

    app.router.add_post("/api/miniapp/ranking/track", ranking_track)
    app.router.add_post("/api/miniapp/ranking/untrack", ranking_untrack)
    app.router.add_post("/api/miniapp/ranking/record", ranking_record)
    app.router.add_get("/api/miniapp/ranking/history/{keyword_id}", ranking_history)
    app.router.add_get("/api/miniapp/ranking/positions", ranking_positions)
    app.router.add_get("/api/miniapp/ranking", ranking_overview)
    app.router.add_get("/api/miniapp/ranking/keywords", ranking_keywords)
    # Фронт добавляет ключ POST-ом на этот же путь. Маршрута не было вовсе:
    # кнопка «Добавить» отвечала 405, и экран нельзя было наполнить.
    app.router.add_post("/api/miniapp/ranking/keywords", ranking_track)
    app.router.add_get("/api/miniapp/ranking/alerts", ranking_alerts)
    app.router.add_get("/api/miniapp/ranking/stats", ranking_stats)
