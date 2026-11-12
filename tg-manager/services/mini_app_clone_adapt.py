"""«Клон и адаптация» — маршруты мини-аппа.

Модуль копирует профиль одного бота на другие с переработкой текста.
Вся логика — в `services/clone_adapt_engine.py`, исполнение — операцией
`clone_adapt` в очереди (`op_worker._exec_clone_adapt`). Здесь — двери экрана:

* GET  /clone_adapt/profile?bot_id=   — что сейчас у источника (для мастера);
* POST /clone_adapt/preview           — «было → станет» по каждой цели,
                                         ничего не меняя в Telegram;
* POST /clone_adapt/submit            — поставить операцию (много целей);
* GET  /clone_adapt/history           — история с подробностями по полям;
* POST /clone_adapt/rollback          — вернуть как было (по op_id).

Токены ботов в базе зашифрованы — читаются ТОЛЬКО через `fetch_bots`.
"""
from __future__ import annotations

import json

from aiohttp import web

# Сколько целей предпросмотр читает из Telegram за один запрос: на каждую —
# несколько getMy*, и экран ждёт ответа. Остальные цели посчитаны, но
# показаны числом.
PREVIEW_CAP = 10


def _parse_ids(raw) -> list[int]:
    out: list[int] = []
    if not isinstance(raw, (list, tuple)):
        raw = [raw] if raw not in (None, "") else []
    for x in raw:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v and v not in out:
            out.append(v)
    return out


def parse_request(data: dict) -> dict:
    """Тело запроса мастера → нормальный вид. Старый формат (одна цель,
    поля строкой) понимается так же, как новый."""
    from services import clone_adapt_engine as ca

    try:
        source_bot_id = int(data.get("source_bot_id") or 0)
    except (TypeError, ValueError):
        source_bot_id = 0
    targets = _parse_ids(data.get("target_bot_ids"))
    if not targets and data.get("target_bot_id"):
        targets = _parse_ids([data.get("target_bot_id")])
    targets = [t for t in targets if t != source_bot_id][:ca.MAX_TARGETS]
    return {
        "source_bot_id": source_bot_id,
        "target_bot_ids": targets,
        "fields": ca.normalize_fields(data.get("fields", "name,desc")),
        "settings": ca.normalize_settings(data.get("settings")),
    }


def setup_routes(app: web.Application, pool) -> None:
    from services import operation_bus as _obus
    from services import clone_adapt_engine as ca
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _list_limit,
        _list_offset, _safe_count, log,
    )
    from database.db import fetch_bots

    _BOT_SQL = ("SELECT bot_id, token, username, first_name FROM managed_bots "
                "WHERE added_by=$1 AND is_active=TRUE AND bot_id = ANY($2::bigint[])")

    async def _own_bots(uid: int, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        rows = await fetch_bots(pool, _BOT_SQL, uid, ids)
        return {int(r["bot_id"]): r for r in rows}

    def _bot_label(r: dict) -> str:
        return ("@" + r["username"]) if r.get("username") else (
            r.get("first_name") or f"бот {r.get('bot_id')}")

    async def clone_adapt_profile(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.query.get("bot_id") or 0)
        except (TypeError, ValueError):
            bot_id = 0
        if not bot_id:
            return _err("Не выбран бот", 400)
        bots = await _own_bots(uid, [bot_id])
        if bot_id not in bots:
            return _err("Бот не найден", 404)
        b = bots[bot_id]
        import aiohttp as _aio
        try:
            async with _aio.ClientSession() as sess:
                prof = await ca.read_profile(
                    sess, b["token"], list(ca.FIELDS), "all",
                    username=b.get("username") or "")
                photo = await ca.read_photo(sess, b["token"])
        except Exception:
            log.exception("clone_adapt_profile uid=%d bot=%d", uid, bot_id)
            return _err("Не удалось прочитать профиль бота из Telegram", 409)
        base = prof["langs"].get("", {})
        langs = sorted({lc for lc in prof["langs"] if lc}
                       | {lc for lc, c in prof["commands"].items() if lc and c})
        return _json_resp({
            "bot_id": bot_id,
            "label": _bot_label(b),
            "name": base.get("name", ""),
            "description": base.get("description", ""),
            "short_description": base.get("short_description", ""),
            "commands": prof["commands"].get("", []),
            "menu": ca.preview_value(prof.get("menu_button") or {}),
            "has_menu": bool(prof.get("menu_button")),
            "rights_groups": ca.preview_value((prof.get("rights") or {}).get("groups") or {}),
            "rights_channels": ca.preview_value((prof.get("rights") or {}).get("channels") or {}),
            "has_photo": bool(photo),
            "langs": [{"code": lc, "label": ca.LANG_LABELS.get(lc, lc)} for lc in langs],
        })

    async def clone_adapt_preview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        req = parse_request(data if isinstance(data, dict) else {})
        if not req["source_bot_id"]:
            return _err("Выберите бота-источник", 400)
        if not req["target_bot_ids"]:
            return _err("Выберите хотя бы одного бота-цель", 400)
        if not req["fields"]:
            return _err("Выберите, что копировать", 400)
        bots = await _own_bots(uid, [req["source_bot_id"]] + req["target_bot_ids"])
        src = bots.get(req["source_bot_id"])
        if not src:
            return _err("Бот-источник не найден", 404)
        targets = [bots[t] for t in req["target_bot_ids"] if t in bots]
        if not targets:
            return _err("Боты-цели не найдены", 404)
        fields, settings = req["fields"], req["settings"]
        import aiohttp as _aio
        out = []
        try:
            async with _aio.ClientSession() as sess:
                source = await ca.read_profile(sess, src["token"], fields,
                                               settings["langs"],
                                               username=src.get("username") or "")
                if "photo" in fields:
                    source["has_photo"] = bool(await ca.read_photo(sess, src["token"]))
                langs = [lc for lc in ca.effective_langs(settings, source) if lc]
                for i, t in enumerate(targets[:PREVIEW_CAP], start=1):
                    target = await ca.read_profile(sess, t["token"], fields, langs,
                                                   username=t.get("username") or "")
                    plan = ca.build_plan(source, target, fields, settings, index=i)
                    out.append({"bot_id": int(t["bot_id"]), "label": _bot_label(t),
                                "counts": ca.plan_counts(plan),
                                "steps": [ca.public_step(s) for s in plan]})
        except Exception:
            log.exception("clone_adapt_preview uid=%d", uid)
            return _err("Не удалось прочитать профили из Telegram", 409)
        return _json_resp({
            "source": _bot_label(src),
            "total": len(targets),
            "shown": len(out),
            "missing": len(req["target_bot_ids"]) - len(targets),
            "summary": ca.settings_summary(fields, settings),
            "langs": [ca.LANG_LABELS.get(lc, lc) for lc in langs],
            "targets": out,
        })

    async def clone_adapt_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        req = parse_request(data if isinstance(data, dict) else {})
        if not req["source_bot_id"] or not req["target_bot_ids"]:
            return _err("Выберите бота-источник и ботов-целей", 400)
        if not req["fields"]:
            return _err("Выберите, что копировать", 400)
        try:
            bots = await _own_bots(uid, [req["source_bot_id"]] + req["target_bot_ids"])
            src = bots.get(req["source_bot_id"])
            if not src:
                return _err("Бот-источник не найден", 404)
            targets = [t for t in req["target_bot_ids"] if t in bots]
            if not targets:
                return _err("Боты-цели не найдены", 404)
            n = len(targets)
            label = (f"Клон {_bot_label(src)} → {_bot_label(bots[targets[0]])}" if n == 1
                     else f"Клон {_bot_label(src)} → ботов: {n}")
            op_id = await _obus.submit(
                pool, uid, "clone_adapt", {
                    "source_bot_id": req["source_bot_id"],
                    "target_bot_ids": targets,
                    "fields": req["fields"],
                    "settings": req["settings"],
                }, total_items=n, label=label)
            return _json_resp({"op_id": op_id, "ok": True, "targets": n})
        except PermissionError as exc:
            # Отказ по тарифу — не сбой: честный 403 с причиной.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("clone_adapt_submit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def clone_adapt_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        status = (request.query.get("status") or "").strip()
        where = "h.owner_id=$1"
        args: list = [uid]
        if status == "problem":
            where += " AND h.status IN ('error','partial')"
        elif status in ("ok", "same"):
            where += " AND h.status = $2"
            args.append(status)
        try:
            n = len(args)
            rows = await pool.fetch(
                f"""
                SELECT h.id, h.op_id, h.source_bot_id, h.target_bot_id, h.fields,
                       h.status, h.details, h.created_at, h.changes, h.settings,
                       sb.username AS source_uname, sb.first_name AS source_name,
                       tb.username AS target_uname, tb.first_name AS target_name
                FROM clone_adapt_history h
                LEFT JOIN managed_bots sb ON sb.bot_id = h.source_bot_id
                LEFT JOIN managed_bots tb ON tb.bot_id = h.target_bot_id
                WHERE {where} ORDER BY h.created_at DESC, h.id DESC
                LIMIT ${n + 1} OFFSET ${n + 2}
                """,
                *args, _list_limit(request, 30, 1000), _list_offset(request),
            )
            total = await _safe_count(
                pool, f"SELECT COUNT(*) FROM clone_adapt_history h WHERE {where}", *args)
            stats = await pool.fetchrow(
                "SELECT COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE status IN ('ok','same')) AS ok, "
                "COUNT(*) FILTER (WHERE status IN ('error','partial')) AS problem, "
                "COUNT(DISTINCT COALESCE(op_id, -id)) AS runs "
                "FROM clone_adapt_history WHERE owner_id=$1", uid)
            # Откат: по каждой операции — сколько снимков ещё можно вернуть.
            op_ids = sorted({int(r["op_id"]) for r in rows if r["op_id"]})
            rollback: dict[int, dict] = {}
            if op_ids:
                for s in await pool.fetch(
                        "SELECT op_id, COUNT(*) FILTER (WHERE reverted_at IS NULL) AS open, "
                        "COUNT(*) AS total FROM bulk_edit_snapshots "
                        "WHERE owner_id=$1 AND entity_type='bot' AND op_id = ANY($2::bigint[]) "
                        "GROUP BY op_id", uid, op_ids):
                    rollback[int(s["op_id"])] = {"open": int(s["open"]), "total": int(s["total"])}
            items = []
            for r in rows:
                d = dict(r)
                for k in ("changes", "settings"):
                    v = d.get(k)
                    if isinstance(v, str):
                        try:
                            d[k] = json.loads(v)
                        except (TypeError, ValueError):
                            d[k] = None
                fields = ca.normalize_fields(d.get("fields") or "")
                d["fields_label"] = ", ".join(ca.FIELD_LABELS[f].lower() for f in fields)
                rb = rollback.get(int(d["op_id"])) if d.get("op_id") else None
                d["can_rollback"] = bool(rb and rb["open"])
                d["rolled_back"] = bool(rb and rb["total"] and not rb["open"])
                items.append(d)
            return _json_resp({
                "items": items, "total": total,
                "stats": {k: int(stats[k] or 0) for k in ("total", "ok", "problem", "runs")}
                if stats else {"total": 0, "ok": 0, "problem": 0, "runs": 0},
            })
        except Exception:
            log.exception("clone_adapt_history uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def clone_adapt_rollback(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            src_op_id = int((body or {}).get("op_id") or 0)
        except Exception:
            return _err("Неверная операция", 400)
        if not src_op_id:
            return _err("Не указан клон для отката", 400)
        # Снимок обязан принадлежать владельцу И быть снят клоном: чужой
        # op_id и снимок массовой правки здесь не откатываются.
        cnt = await _safe_count(
            pool, "SELECT COUNT(*) FROM bulk_edit_snapshots s "
            "WHERE s.owner_id=$1 AND s.op_id=$2 AND s.entity_type='bot' "
            "AND s.reverted_at IS NULL AND EXISTS (SELECT 1 FROM clone_adapt_history h "
            "WHERE h.owner_id=$1 AND h.op_id=$2)", uid, src_op_id)
        if not cnt:
            return _err("Нечего откатывать: у этого клона нет снимка или он уже откачен", 400)
        try:
            op_id = await _obus.submit(
                pool, uid, "clone_adapt_rollback", {"source_op_id": src_op_id},
                total_items=int(cnt), label=f"Откат клона: ботов {cnt}")
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("clone_adapt_rollback uid=%d src=%d", uid, src_op_id)
            return _err(_INTERNAL_ERROR, 500)
        if not op_id:
            return _err("Не удалось поставить откат", 500)
        return _json_resp({"ok": True, "op_id": op_id, "count": int(cnt)})

    app.router.add_get("/api/miniapp/clone_adapt/profile", clone_adapt_profile)
    app.router.add_post("/api/miniapp/clone_adapt/preview", clone_adapt_preview)
    app.router.add_post("/api/miniapp/clone_adapt/submit", clone_adapt_submit)
    app.router.add_get("/api/miniapp/clone_adapt/history", clone_adapt_history)
    app.router.add_post("/api/miniapp/clone_adapt/rollback", clone_adapt_rollback)
