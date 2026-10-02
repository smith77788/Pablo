"""Ноды-комьюнити: mini-Discord для аудитории.

Узлы сообщества, их каналы, участники и назначение модераторов. Проверка
владения узлом (`_community_owns`) нужна только здесь, поэтому переехала вместе
с группой.

Вынесено из `services/mini_app_api.py`: тот файл — больше 24 тысяч строк и 926
маршрутов в одной функции, искать в нём дороже, чем править. Режем по одной
группе за раз, и только такие, что не завязаны на соседей.

Что маршруты не потерялись, стережёт слепок в
`tests/test_miniapp_routes_are_not_lost.py`.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Регистрирует маршруты на общем приложении мини-аппа."""
    # Общие помощники живут в mini_app_api, а он импортирует этот модуль —
    # импорт сверху дал бы цикл. К моменту вызова mini_app_api уже загружен.
    from services.mini_app_api import _err, _get_uid, _json_resp, _safe_count, _safe_fetch, _safe_fetchrow, log, validate_integer, validate_string

    async def community_nodes_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import nodes_engine
        try:
            return _json_resp({"nodes": await nodes_engine.list_community_nodes(pool, uid)})
        except Exception:
            log.exception("community_nodes_list uid=%d", uid)
            log.exception("community_nodes_list uid=%s", uid)
            return _err("Не удалось загрузить список форумов", 500)

    async def community_node_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        title = validate_string(data.get("title"), max_len=128) or ""
        description = validate_string(data.get("description"), max_len=512) or ""
        try:
            tg_chat_id = int(str(data.get("tg_chat_id") or "").strip())
        except (ValueError, TypeError):
            return _err("tg_chat_id должен быть числом (например -1001234567890)", 400)
        if not title:
            return _err("Укажите название сообщества", 400)
        from services import nodes_engine
        try:
            node = await nodes_engine.register_community_node(pool, uid, tg_chat_id, title, description)
            return _json_resp({"ok": True, "node": node})
        except Exception:
            log.exception("community_node_create uid=%d", uid)
            log.exception("community_node_create uid=%s", uid)
            return _err("Не удалось создать форум", 500)

    async def community_node_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор узла", 400)
        from services import nodes_engine
        await nodes_engine.deactivate_community_node(pool, uid, node_id)
        return _json_resp({"ok": True})

    async def community_node_invite_link(request: web.Request) -> web.Response:
        """Инвайт-ссылка ноды-сообщества, чтобы инвайтить в её чат по ссылке, а
        не голым числовым tg_chat_id.

        Раньше «Пригласить аудиторию» в ноде (inviteToCommunity) подставляло в
        поле инвайта СЫРОЙ tg_chat_id: регистрация ноды (community_node_create)
        просит только число, без привязки аккаунта/username/access_hash. Для
        аккаунта-приглашателя, который никогда не видел этот чат,
        `get_entity(числовой_id)` не резолвится — инвайт падал у всех, кроме
        случайного совпадения.

        Решение переиспользует уже накопленное: `community_node_members`
        (заполняется «Оживить» — services/nodes_engine.livenCommunity) хранит,
        какие аккаунты ФЛОТА реально состоят в этой ноде. Пробуем их по
        порядку (админы сначала — им точно можно экспортировать ссылку),
        останавливаемся на первом успехе. Не «операция» — единичный синхронный
        Telethon-вызов, как и channel_invite_link рядом."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор узла", 400)
        node = await _safe_fetchrow(
            pool, "SELECT id, tg_chat_id FROM community_nodes WHERE id=$1 AND owner_id=$2",
            node_id, uid)
        if not node:
            return _err("Нода не найдена", 404)
        members = await _safe_fetch(pool,
            """SELECT a.id, a.session_str, a.device_model, a.system_version, a.app_version,
                      a.lang_code, a.system_lang_code,
                      (SELECT proxy_url FROM user_proxies up
                       WHERE up.id=a.proxy_id AND up.is_active=TRUE) AS proxy_url
               FROM community_node_members m
               JOIN tg_accounts a ON a.id=m.account_id
               WHERE m.node_id=$1 AND a.owner_id=$2 AND a.is_active=TRUE
                     AND a.session_str IS NOT NULL AND a.session_str <> ''
               ORDER BY CASE m.role WHEN 'admin' THEN 0 WHEN 'moderator' THEN 1 ELSE 2 END,
                        m.joined_at
               LIMIT 8""", node_id, uid) or []
        if not members:
            return _err(
                "Ни один аккаунт флота не отмечен участником этой ноды — сначала "
                "«Оживить» её (добавить аккаунты в чат), иначе инвайтить некому "
                "разрезолвить чат.", 400)
        from services import account_manager
        for acc in members:
            try:
                link = await account_manager.get_channel_invite_link(
                    acc["session_str"], int(node["tg_chat_id"]), _acc=dict(acc))
            except Exception:
                link = ""
            if link:
                return _json_resp({"ok": True, "invite_link": link})
        return _err(
            "Не удалось получить ссылку — ни один известный аккаунт флота не "
            "смог экспортировать приглашение (нет прав или чат недоступен).", 400)

    async def community_channels_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор узла", 400)
        from services import nodes_engine
        return _json_resp({"channels": await nodes_engine.list_community_channels(pool, uid, node_id)})

    async def community_channel_add(request: web.Request) -> web.Response:
        """Добавить канал (форум-топик) в ноду — через op (создание в процессе бота)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
            data = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        name = validate_string(data.get("name"), max_len=128)
        if not name:
            return _err("Укажите название канала", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM community_nodes WHERE id=$1 AND owner_id=$2 AND is_active=TRUE",
            node_id, uid)
        if not owns:
            return _err("Нода не найдена", 404)
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(pool, uid, "community_add_channel",
                {"node_id": node_id, "name": name}, total_items=1,
                label=f"Ноды: канал «{name[:40]}»")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("community_channel_add uid=%d", uid)
            log.exception("community_channel_add uid=%s", uid)
            return _err("Не удалось подключить канал к форуму", 500)

    async def community_members_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор узла", 400)
        from services import nodes_engine
        members = await nodes_engine.list_node_members(pool, uid, node_id)
        stats = await nodes_engine.node_member_stats(pool, node_id)
        return _json_resp({"members": members, "stats": stats})

    async def _community_owns(uid, node_id):
        return await _safe_count(pool,
            "SELECT COUNT(*) FROM community_nodes WHERE id=$1 AND owner_id=$2 AND is_active=TRUE",
            node_id, uid)

    async def community_liven(request: web.Request) -> web.Response:
        """Оживить ноду флотом (ghost-присутствие): op community_liven."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
            data = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _community_owns(uid, node_id):
            return _err("Нода не найдена", 404)
        count = validate_integer(data.get("count", 5), min_val=1, max_val=50) or 5
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(pool, uid, "community_liven",
                {"node_id": node_id, "count": count}, total_items=count,
                label=f"Ноды: оживить × {count}")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("community_liven uid=%d", uid)
            log.exception("community_liven uid=%s", uid)
            return _err("Не удалось запустить оживление", 500)

    async def community_set_staff(request: web.Request) -> web.Response:
        """Назначить участников-флот ноды модераторами/админами: op community_set_staff."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
            data = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _community_owns(uid, node_id):
            return _err("Нода не найдена", 404)
        role = data.get("role") if data.get("role") in ("moderator", "admin") else "moderator"
        acc_ids = [int(x) for x in (data.get("account_ids") or []) if str(x).isdigit()]
        if not acc_ids:
            return _err("Выберите аккаунты", 400)
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(pool, uid, "community_set_staff",
                {"node_id": node_id, "role": role, "account_ids": acc_ids},
                total_items=len(acc_ids), label=f"Ноды: роли {role} × {len(acc_ids)}")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("community_set_staff uid=%d", uid)
            log.exception("community_set_staff uid=%s", uid)
            return _err("Не удалось изменить роль участника", 500)

    app.router.add_get("/api/miniapp/community/nodes", community_nodes_list)
    app.router.add_post("/api/miniapp/community/node", community_node_create)
    app.router.add_delete("/api/miniapp/community/node/{node_id}", community_node_delete)
    app.router.add_get("/api/miniapp/community/node/{node_id}/invite_link", community_node_invite_link)
    app.router.add_get("/api/miniapp/community/node/{node_id}/channels", community_channels_list)
    app.router.add_post("/api/miniapp/community/node/{node_id}/channels", community_channel_add)
    app.router.add_get("/api/miniapp/community/node/{node_id}/members", community_members_list)
    app.router.add_post("/api/miniapp/community/node/{node_id}/liven", community_liven)
    app.router.add_post("/api/miniapp/community/node/{node_id}/staff", community_set_staff)
