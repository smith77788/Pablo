"""Сети и экосистемы каналов — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 26 маршрутов — обзор сети, экосистемы
(список, карточка, создание, удаление, автопоиск участников, снятие
участника, пересечение аудиторий), сети каналов (список, карточка, план
развёртывания, развёртывание, узлы и связи), шаблоны и экземпляры сетей,
сводка по сети и кросспостинг.

`_eco_member_names` уехал вместе с ними: других потребителей у него нет.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты сетей и экосистем на приложении мини-аппа."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _safe_count, _safe_fetch,
        _safe_fetchrow, log,
    )
    from services.security import validate_integer, validate_string

    # Где искать имя объекта каждого рода. Экосистема хранит только тип и id, и
    # экран показывал «account #17» — номер строки в базе, по которому владелец
    # не узнает ни аккаунт, ни канал.
    _ECO_NAME_SQL = {
        "account": "SELECT id AS k, COALESCE(NULLIF(first_name,''), phone, '') AS n "
                   "FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[])",
        "bot": "SELECT bot_id AS k, COALESCE(NULLIF(username,''), NULLIF(first_name,''), '') AS n "
               "FROM managed_bots WHERE added_by=$1 AND bot_id = ANY($2::bigint[])",
        "channel": "SELECT channel_id AS k, COALESCE(NULLIF(title,''), NULLIF(username,''), '') AS n "
                   "FROM managed_channels WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])",
    }
    _ECO_NAME_SQL["group"] = _ECO_NAME_SQL["channel"]

    async def network_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bots = await pool.fetch(
                """SELECT bot_id, username, first_name, is_active, bot_role, cluster, swarm_enabled, swarm_weight
                   FROM managed_bots WHERE added_by=$1 ORDER BY cluster, bot_role""",
                uid,
            )
        except Exception:
            log.exception("network_overview uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)
        # Group by cluster
        clusters: dict = {}
        for r in bots:
            cl = r["cluster"] or "default"
            if cl not in clusters:
                clusters[cl] = []
            clusters[cl].append({
                "bot_id": r["bot_id"],
                "name": r["username"] or r["first_name"] or str(r["bot_id"]),
                "is_active": bool(r["is_active"]),
                "role": r["bot_role"] or "general",
                "swarm": bool(r["swarm_enabled"]),
                "weight": float(r["swarm_weight"] or 1.0),
            })
        return _json_resp({
            "clusters": [
                {"name": name, "bots": bot_list}
                for name, bot_list in clusters.items()
            ],
            "total_bots": len(bots),
        })

    async def ecosystems_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT e.id, e.name, e.ecosystem_type, e.status, e.health_score,
                       e.risk_level, e.region, e.created_at, e.updated_at,
                       COUNT(em.id) AS member_count
                FROM ecosystems e
                LEFT JOIN ecosystem_members em ON em.ecosystem_id = e.id
                WHERE e.owner_id=$1
                GROUP BY e.id, e.name, e.ecosystem_type, e.status, e.health_score,
                         e.risk_level, e.region, e.created_at, e.updated_at
                ORDER BY e.updated_at DESC NULLS LAST
                LIMIT 30
                """,
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            log.exception("ecosystems_list uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def _eco_member_names(uid: int, members: list) -> None:
        """Дописать каждому участнику человеческое имя (поле name).

        Выборки — по владельцу: чужой объект имени не получит. Fail-open: без
        имени строка остаётся с типом и номером, экран не падает."""
        by_type: dict[str, list[int]] = {}
        for m in members:
            t = str(m.get("object_type") or "").strip().lower()
            try:
                by_type.setdefault(t, []).append(int(m.get("object_id") or 0))
            except (TypeError, ValueError):
                continue
        found: dict[tuple[str, int], str] = {}
        for t, ids in by_type.items():
            sql = _ECO_NAME_SQL.get(t)
            if not sql or not ids:
                continue
            for r in await _safe_fetch(pool, sql, uid, ids):
                found[(t, int(r["k"]))] = str(r["n"] or "")
        for m in members:
            t = str(m.get("object_type") or "").strip().lower()
            try:
                oid = int(m.get("object_id") or 0)
            except (TypeError, ValueError):
                oid = 0
            m["name"] = found.get((t, oid), "")

    async def ecosystem_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            eco_id = int(request.match_info["eco_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор экосистемы", 400)
        try:
            eco = await pool.fetchrow(
                "SELECT * FROM ecosystems WHERE id=$1 AND owner_id=$2", eco_id, uid
            )
            if not eco:
                return _err("Не найдено", 404)
            rows = await pool.fetch(
                "SELECT object_type, object_id, role, added_at "
                "FROM ecosystem_members WHERE ecosystem_id=$1 ORDER BY added_at DESC LIMIT 50",
                eco_id,
            )
            members = [dict(m) for m in rows]
            await _eco_member_names(uid, members)
            members_total = await _safe_count(
                pool, "SELECT COUNT(*) FROM ecosystem_members WHERE ecosystem_id=$1", eco_id)
            try:
                events = await pool.fetch(
                    "SELECT event_type, severity, title, occurred_at "
                    "FROM ecosystem_events WHERE ecosystem_id=$1 ORDER BY occurred_at DESC LIMIT 20",
                    eco_id,
                )
            except Exception:
                events = []
            return _json_resp({
                "eco": dict(eco),
                "members": members,
                "members_total": int(members_total or len(members)),
                "events": [dict(ev) for ev in events],
            })
        except Exception:
            log.exception("ecosystem_detail uid=%d eco=%d", uid, eco_id)
            return _err(_INTERNAL_ERROR, 500)

    async def ecosystem_member_drop(request: web.Request) -> web.Response:
        """Убрать объект из экосистемы: {object_type, object_id}.

        Экран показывал состав только на чтение: добавить можно было
        авто-наполнением, а убрать лишнее — ничем."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            eco_id = int(request.match_info["eco_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор экосистемы", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        otype = validate_string(body.get("object_type"), max_len=32) or ""
        otype = otype.strip().lower()
        if otype not in ("account", "bot", "channel", "group"):
            return _err("Неизвестный тип объекта", 400)
        try:
            oid = validate_integer(body.get("object_id"), min_val=1, max_val=2 ** 63 - 1)
        except (TypeError, ValueError):
            return _err("Неверный идентификатор объекта", 400)
        own = await _safe_fetchrow(
            pool, "SELECT id FROM ecosystems WHERE id=$1 AND owner_id=$2", eco_id, uid)
        if not own:
            return _err("Не найдено", 404)
        from services import ecosystem_brain
        await ecosystem_brain.remove_member(pool, eco_id, uid, otype, int(oid))
        return _json_resp({"ok": True})

    async def ecosystem_auto_discover(request: web.Request) -> web.Response:
        """Авто-наполнение экосистемы объектами (аккаунты/каналы/боты по region/пулам).
        Возможность ecosystem_brain.auto_discover_members раньше не была выведена в UI."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            eco_id = int(request.match_info["eco_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор экосистемы", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM ecosystems WHERE id=$1 AND owner_id=$2", eco_id, uid)
        if not owns:
            return _err("Экосистема не найдена", 404)
        try:
            from services import ecosystem_brain
            added = await ecosystem_brain.auto_discover_members(pool, eco_id, uid)
            total = sum(int(v) for v in (added or {}).values())
            return _json_resp({"ok": True, "added": added or {}, "total": total})
        except Exception:
            log.exception("ecosystem_auto_discover uid=%d eco=%d", uid, eco_id)
            return _err(_INTERNAL_ERROR, 500)

    async def ecosystem_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        name = (body.get("name") or "").strip()
        description = (body.get("description") or "").strip()
        ecosystem_type = (body.get("ecosystem_type") or "custom").strip()
        region = (body.get("region") or "").strip()
        if not name:
            return _err("Нужно название")
        try:
            row = await pool.fetchrow(
                """INSERT INTO ecosystems(owner_id, name, description, ecosystem_type, region)
                   VALUES($1,$2,$3,$4,$5) RETURNING id""",
                uid, name, description or None, ecosystem_type, region or None,
            )
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("ecosystem_create uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def ecosystem_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            eco_id = int(request.match_info["eco_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор экосистемы", 400)
        try:
            await pool.execute(
                "DELETE FROM ecosystems WHERE id=$1 AND owner_id=$2", eco_id, uid
            )
            # Экосистема связывает ботов и даёт доступ к ним её участникам:
            # её удаление — изменение прав, а не правка настройки.
            from database.db import record_manual_action

            await record_manual_action(
                pool, uid, "ecosystem_delete", target=str(eco_id))
            return _json_resp({"ok": True})
        except Exception:
            log.exception("ecosystem_delete uid=%d eco=%d", uid, eco_id)
            return _err(_INTERNAL_ERROR, 500)

    async def ecosystem_overlaps(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import ecosystem_brain as _eb
            eco_id = request.match_info.get("eco_id")
            if eco_id:
                # Проверка владения: eco_id из пути нельзя доверять — иначе
                # любой юзер прочитает каналы и overlap чужой экосистемы.
                owns = await pool.fetchval(
                    "SELECT 1 FROM ecosystems WHERE id=$1 AND owner_id=$2",
                    int(eco_id),
                    uid,
                )
                if not owns:
                    return _err("Не найдено", 404)
                # Реальное членство каналов — ecosystem_members (object_type='channel').
                # Прежде читалось из ecosystem_channels — мёртвой таблицы, куда никто
                # не пишет → ch_ids всегда пуст → overlap всегда {} (фича 4A мертва).
                ch_rows = await pool.fetch(
                    "SELECT object_id AS channel_id FROM ecosystem_members "
                    "WHERE ecosystem_id=$1 AND object_type='channel'", int(eco_id)
                )
                ch_ids = [r["channel_id"] for r in ch_rows]
                if ch_ids:
                    overlaps = await _eb.analyze_audience_overlap(pool, ch_ids)
                    # Пары приходят номерами каналов. Номер владельцу ничего не
                    # говорит — подставляем названия (выборка по владельцу,
                    # чужой канал имени не получит).
                    names = {}
                    for r in await _safe_fetch(
                            pool,
                            "SELECT channel_id, COALESCE(NULLIF(title,''), "
                            "       NULLIF('@' || username,'@'), '') AS n "
                            "FROM managed_channels "
                            "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])",
                            uid, ch_ids):
                        names[int(r["channel_id"])] = str(r["n"] or "")
                    for pr in overlaps.get("pairs") or []:
                        a, b = int(pr["channel_a"]), int(pr["channel_b"])
                        pr["name_a"] = names.get(a) or f"Канал #{a}"
                        pr["name_b"] = names.get(b) or f"Канал #{b}"
                    overlaps["channels"] = len(ch_ids)
                    return _json_resp(overlaps)
                # Каналов в экосистеме нет — считать пересечения не на чем.
                return _json_resp({"pairs": [], "channels": len(ch_ids),
                                   "total_subscribers": 0, "unique_subscribers": 0})
            return _json_resp({"pairs": [], "channels": 0,
                               "total_subscribers": 0, "unique_subscribers": 0})
        except Exception:
            log.exception("ecosystem_overlaps")
            return _err(_INTERNAL_ERROR, 500)

    async def networks_list(request: web.Request) -> web.Response:
        """Network Builder — список сеток владельца (форма экрана s-network-builder)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await _safe_fetch(pool,
                """SELECT ni.id, ni.name, COALESCE(ni.status,'active') AS status,
                     COALESCE(ni.description,'') AS description,
                     (SELECT COUNT(*) FROM network_nodes n WHERE n.instance_id=ni.id) AS node_count,
                     (SELECT COUNT(*) FROM network_edges e WHERE e.instance_id=ni.id) AS edge_count
                   FROM network_instances ni WHERE ni.owner_id=$1 ORDER BY ni.created_at DESC""",
                uid)
            return _json_resp({"networks": [dict(r) for r in (rows or [])]})
        except Exception:
            log.exception("networks_list uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def network_create_plural(request: web.Request) -> web.Response:
        """Создать сетку. body: {name, template?, description?}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        name = validate_string(body.get("name"), max_len=100)
        if not name:
            return _err("Укажите название сети", 400)
        from services import network_builder as _nb
        # Шаблон и описание экран присылал с самого начала, а обработчик читал
        # только название: человек выбирал «🎯 Хаб-спицы», получал пустую сеть и
        # подпись «✅ Сеть по шаблону создана». Неизвестный шаблон — отказ, а не
        # тихо пустая сеть.
        template = (validate_string(body.get("template"), max_len=40,
                                    required=False) or "").strip().lower()
        if template and not _nb.topology_template(template):
            return _err("Неизвестный шаблон сети", 400)
        description = validate_string(body.get("description"), max_len=1000,
                                      required=False) or ""
        try:
            nid = await pool.fetchval(
                "INSERT INTO network_instances(owner_id, name, status, description) "
                "VALUES($1,$2,'active',$3) RETURNING id", uid, name, description)
        except Exception:
            log.exception("network_create uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        out = {"ok": True, "id": nid, "nodes": 0, "edges": 0}
        if template:
            res = await _nb.apply_template(pool, nid, template)
            if not res.get("ok"):
                # Сеть уже создана: сообщаем ровно то, что получилось, а не
                # «готово» и не «ошибка» — иначе человек не знает, что у него есть.
                log.warning("network_create template=%s uid=%s: %s",
                            template, uid, res.get("error"))
                out["template_error"] = res.get("error") or "Шаблон не применён"
            else:
                out.update(template=template, template_label=res.get("label"),
                           nodes=res.get("nodes", 0), edges=res.get("edges", 0))
        return _json_resp(out)

    async def network_detail_plural(request: web.Request) -> web.Response:
        """Детали сети: узлы/рёбра в форме, которую рисует граф (from_id/to_id/labels)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            nid = int(request.match_info["net_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        from services import network_builder as _nb
        d = await _nb.get_instance_detail(pool, uid, nid)
        if not d:
            return _err("Сеть не найдена", 404)
        label_by_id = {n["id"]: (n.get("label") or n.get("node_type") or "#") for n in d["nodes"]}
        nodes = [{"id": n["id"], "type": n.get("node_type"),
                  "label": n.get("label") or "", "object_id": n.get("ref_id")}
                 for n in d["nodes"]]
        edges = [{"id": e["id"], "from_id": e.get("source_node_id"),
                  "to_id": e.get("target_node_id"), "type": e.get("edge_type"),
                  "from_label": label_by_id.get(e.get("source_node_id"), "#"),
                  "to_label": label_by_id.get(e.get("target_node_id"), "#")}
                 for e in d["edges"]]
        inst = d["instance"]
        return _json_resp({"id": nid, "name": inst.get("name"),
                           "status": inst.get("status"),
                           "description": inst.get("description") or "",
                           "nodes": nodes, "edges": edges})

    async def network_deploy_plan(request: web.Request) -> web.Response:
        """Превью-план развёртывания связки: топология → упорядоченные шаги.

        Чистый network_builder.plan_deployment по узлам/рёбрам инстанса —
        показывает, что создать (через какие фабрики) и как связать. Фаза 1:
        только план (исполнитель — отдельно)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            nid = int(request.match_info["net_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        from services import network_builder as _nb
        d = await _nb.get_instance_detail(pool, uid, nid)
        if not d:
            return _err("Сеть не найдена", 404)
        nodes = [{"id": n["id"], "type": n.get("node_type"),
                  "label": n.get("label") or "", "object_id": n.get("ref_id")}
                 for n in d["nodes"]]
        edges = [{"from_id": e.get("source_node_id"), "to_id": e.get("target_node_id"),
                  "type": e.get("edge_type")} for e in d["edges"]]
        return _json_resp(_nb.plan_deployment(nodes, edges))

    async def network_deploy(request: web.Request) -> web.Response:
        """Развернуть связку (Фаза 2): поставить op deploy_network в очередь.
        Создаёт недостающие каналы/группы и вяжет admin-рёбра под губернатором."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            nid = int(request.match_info["net_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM network_instances WHERE id=$1 AND owner_id=$2", nid, uid)
        if not owns:
            return _err("Сеть не найдена", 404)
        from services import network_builder as _nb
        d = await _nb.get_instance_detail(pool, uid, nid)
        nodes = [{"id": n["id"], "type": n.get("node_type"), "label": n.get("label") or "",
                  "object_id": n.get("ref_id")} for n in (d["nodes"] if d else [])]
        edges = [{"from_id": e.get("source_node_id"), "to_id": e.get("target_node_id"),
                  "type": e.get("edge_type")} for e in (d["edges"] if d else [])]
        plan = _nb.plan_deployment(nodes, edges)
        if not plan.get("steps"):
            return _err("Нечего разворачивать — добавьте узлы/рёбра", 400)
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "deploy_network", {"instance_id": nid},
                total_items=len(plan["steps"]),
                label=f"Развернуть связку #{nid}: +{plan['create']} объектов")
            return _json_resp({"ok": True, "op_id": op_id,
                               "create": plan["create"], "wire": plan["wire"]})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("network_deploy uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def crosspost_links_list(request: web.Request) -> web.Response:
        """Правила кросспостинга владельца (связки 2c)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            "SELECT id, source_channel_id, target_channel_id, enabled, "
            "forwarded_total, last_run_at FROM crosspost_links "
            "WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 100", uid)
        out = [dict(r) for r in (rows or [])]
        # Подмешать названия каналов, если есть в managed_channels.
        titles = {}
        trows = await _safe_fetch(pool,
            "SELECT channel_id, title, username FROM managed_channels WHERE owner_id=$1", uid)
        for t in (trows or []):
            titles[int(t["channel_id"])] = t["title"] or (("@" + t["username"]) if t["username"] else str(t["channel_id"]))
        for d in out:
            d["source_title"] = titles.get(int(d["source_channel_id"]), str(d["source_channel_id"]))
            d["target_title"] = titles.get(int(d["target_channel_id"]), str(d["target_channel_id"]))
        return _json_resp({"links": out})

    async def crosspost_run(request: web.Request) -> web.Response:
        """Прогнать кросспостинг: op crosspost_run через шину (под губернатором)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        n = await _safe_count(pool,
            "SELECT COUNT(*) FROM crosspost_links WHERE owner_id=$1 AND enabled", uid)
        if not n:
            return _err("Нет активных правил кросспостинга", 400)
        p = {}
        if str(body.get("link_id") or "").isdigit():
            p["link_id"] = int(body["link_id"])
        # Масштаб — по тому, что реально пройдёт: иначе «1 из 12» или «50 из 80».
        _total = 1 if "link_id" in p else min(int(n), 50)
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(pool, uid, "crosspost_run", p,
                                               total_items=_total, label="Кросспостинг: прогон")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("crosspost_run uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def network_add_node(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            nid = int(request.match_info["net_id"])
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM network_instances WHERE id=$1 AND owner_id=$2", nid, uid)
        if not owns:
            return _err("Сеть не найдена", 404)
        from services import network_builder as _nb
        typ = validate_string(body.get("type"), max_len=40) or "node"
        label = validate_string(body.get("label"), max_len=200) or ""
        try:
            ref = int(body.get("object_id")) if str(body.get("object_id") or "").strip() else None
        except (TypeError, ValueError):
            ref = None
        res = await _nb.add_node(pool, nid, typ, label, ref_id=ref)
        return _json_resp(res)

    async def network_delete_node(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        await pool.execute(
            "DELETE FROM network_nodes WHERE id=$1 AND instance_id IN "
            "(SELECT id FROM network_instances WHERE owner_id=$2)", node_id, uid)
        return _json_resp({"ok": True})

    async def network_add_edge(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            nid = int(request.match_info["net_id"])
            body = await request.json()
            frm = int(body.get("from_id")); to = int(body.get("to_id"))
        except Exception:
            return _err("Неверный запрос", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM network_instances WHERE id=$1 AND owner_id=$2", nid, uid)
        if not owns:
            return _err("Сеть не найдена", 404)
        from services import network_builder as _nb
        typ = validate_string(body.get("type"), max_len=40) or "forward"
        res = await _nb.add_edge(pool, nid, frm, to, edge_type=typ)
        return _json_resp(res)

    async def network_delete_edge(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            edge_id = int(request.match_info["edge_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        await pool.execute(
            "DELETE FROM network_edges WHERE id=$1 AND instance_id IN "
            "(SELECT id FROM network_instances WHERE owner_id=$2)", edge_id, uid)
        return _json_resp({"ok": True})

    async def network_templates(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.network_builder import get_templates
            templates = await get_templates(pool, uid)
            return _json_resp({'templates': templates})
        except Exception:
            log.exception("network_templates")
            return _err(_INTERNAL_ERROR, 500)

    async def network_template_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.network_builder import create_template
            result = await create_template(pool, uid, data.get('name', ''),
                                           data.get('description', ''),
                                           data.get('template_type', 'channel_group'),
                                           data.get('nodes'), data.get('edges'))
            return _json_resp(result)
        except Exception:
            log.exception("network_template_create")
            return _err(_INTERNAL_ERROR, 500)

    async def network_template_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            template_id = int(request.match_info['template_id'])
            from services.network_builder import delete_template
            result = await delete_template(pool, uid, template_id)
            return _json_resp(result)
        except Exception:
            log.exception("network_template_delete")
            return _err(_INTERNAL_ERROR, 500)

    async def network_instances(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.network_builder import get_instances
            instances = await get_instances(pool, uid)
            return _json_resp({'instances': instances})
        except Exception:
            log.exception("network_instances")
            return _err(_INTERNAL_ERROR, 500)

    async def network_instance_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.network_builder import create_instance
            result = await create_instance(pool, uid, data.get('template_id', 0),
                                           data.get('name', ''))
            return _json_resp(result)
        except Exception:
            log.exception("network_instance_create")
            return _err(_INTERNAL_ERROR, 500)

    async def network_instance_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            instance_id = int(request.match_info['instance_id'])
            from services.network_builder import get_instance_detail
            detail = await get_instance_detail(pool, uid, instance_id)
            if not detail:
                return _err("Не найдено", 404)
            return _json_resp(detail)
        except Exception:
            log.exception("network_instance_detail")
            return _err(_INTERNAL_ERROR, 500)

    async def network_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.network_builder import get_network_stats
            stats = await get_network_stats(pool, uid)
            return _json_resp(stats)
        except Exception:
            log.exception("network_stats")
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/network", network_overview)
    app.router.add_get("/api/miniapp/ecosystems", ecosystems_list)
    app.router.add_get("/api/miniapp/ecosystem/{eco_id}", ecosystem_detail)
    app.router.add_post("/api/miniapp/ecosystem/{eco_id}/auto_discover", ecosystem_auto_discover)
    app.router.add_post("/api/miniapp/ecosystem/{eco_id}/member/drop", ecosystem_member_drop)
    app.router.add_post("/api/miniapp/ecosystem", ecosystem_create)
    app.router.add_delete("/api/miniapp/ecosystem/{eco_id}", ecosystem_delete)
    app.router.add_get("/api/miniapp/networks", networks_list)
    app.router.add_post("/api/miniapp/networks", network_create_plural)
    app.router.add_delete("/api/miniapp/networks/nodes/{node_id}", network_delete_node)
    app.router.add_delete("/api/miniapp/networks/edges/{edge_id}", network_delete_edge)
    app.router.add_get("/api/miniapp/networks/{net_id}", network_detail_plural)
    app.router.add_get("/api/miniapp/networks/{net_id}/deploy_plan", network_deploy_plan)
    app.router.add_post("/api/miniapp/networks/{net_id}/deploy", network_deploy)
    app.router.add_get("/api/miniapp/crosspost/links", crosspost_links_list)
    app.router.add_post("/api/miniapp/crosspost/run", crosspost_run)
    app.router.add_post("/api/miniapp/networks/{net_id}/nodes", network_add_node)
    app.router.add_post("/api/miniapp/networks/{net_id}/edges", network_add_edge)
    app.router.add_get("/api/miniapp/ecosystem/{eco_id}/overlaps", ecosystem_overlaps)
    app.router.add_get("/api/miniapp/network/templates", network_templates)
    app.router.add_post("/api/miniapp/network/template", network_template_create)
    app.router.add_delete("/api/miniapp/network/template/{template_id}", network_template_delete)
    app.router.add_get("/api/miniapp/network/instances", network_instances)
    app.router.add_post("/api/miniapp/network/instance", network_instance_create)
    app.router.add_get("/api/miniapp/network/instance/{instance_id}", network_instance_detail)
    app.router.add_get("/api/miniapp/network/stats", network_stats)
