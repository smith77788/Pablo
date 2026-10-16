"""Жалобы, фиксация нарушений и флуд-метки — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 14 маршрутов трёх блоков, которые
владелец видит как одну работу «защититься от нарушителя и от жалоб на
себя».

* `/api/miniapp/strike/*` — жалоба по нарушению: состояние, история, набор
  документов для обращения и запуск. Запуск закрыт правовым гейтом
  (`tests/test_strike_legal_gate.py`) и уходит через `operation_bus.submit`.
* `/api/miniapp/notary/*` — наблюдение за чужим каналом: список, заведение,
  карточка с зафиксированными слепками, проверка сейчас, отмена.
* `/api/miniapp/flood/{bot_id}*` — флуд-метки бота: настройка порога,
  пометка недавних, снятие метки и чистка. Считает их `services.flood_guard`
  (`_fg`) — второго источника правды здесь нет; на метки смотрит и рассылка
  (`tests/test_suspect_subscribers_get_no_broadcast.py`).

Вместе с обработчиками уехало замыкание `_fg_owns(uid, bot_id)` — бот
принадлежит владельцу; по `{bot_id}` из пути без него можно было бы читать и
чистить метки чужого бота. Его не зовёт больше никто.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

import json as _json

from aiohttp import web

from services import account_status as _acc_status
from services import flood_guard as _fg
from services import operation_bus as _obus


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты жалоб, фиксации нарушений и флуд-меток."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _list_limit, _safe_count,
        _safe_fetch, _safe_fetchrow, log,
    )
    from services.security import validate_string


    async def notary_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        lim = _list_limit(request, 100, 2000)
        rows = await _safe_fetch(pool,
            """SELECT id, channel_ref, channel_title, msg_id, advertiser,
                      promised_from, promised_until, status, verdict,
                      first_seen_at, last_seen_at, absent_since,
                      views_first, views_last, checks_done, cert_sig, created_at
                 FROM notary_watches WHERE owner_id=$1
                ORDER BY created_at DESC LIMIT $2""", uid, lim)
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM notary_watches WHERE owner_id=$1", uid)
        from services import notary as _nt
        out = []
        for r in (rows or []):
            d = dict(r)
            for k in ("promised_from", "promised_until", "first_seen_at",
                      "last_seen_at", "absent_since", "created_at"):
                d[k] = d[k].isoformat() if d.get(k) else None
            v = d.get("verdict")
            d["verdict_label"] = _nt.VERDICT_LABEL.get(v, "⏳ Ожидает наблюдений")
            d["signed"] = bool(d.pop("cert_sig", None))
            out.append(d)
        return _json_resp({"ok": True, "watches": out, "total": total})

    async def notary_create(request: web.Request) -> web.Response:
        """Поставить размещение под наблюдение.

        msg_id не обязателен: без него наблюдаем ПОЯВЛЕНИЕ поста, что и даёт
        возможность доказать обратное — «оплачено, но не опубликовано».
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        channel = validate_string(body.get("channel"), max_len=120)
        if not channel:
            return _err("Укажите канал (@username или id)", 400)
        try:
            hours = float(body.get("hours") or 24)
        except (TypeError, ValueError):
            return _err("Некорректный срок", 400)
        if not 0.5 <= hours <= 24 * 30:
            return _err("Срок должен быть от 30 минут до 30 суток", 400)
        msg_id = body.get("msg_id")
        try:
            msg_id = int(msg_id) if msg_id else None
        except (TypeError, ValueError):
            return _err("Некорректный id поста", 400)
        advertiser = validate_string(body.get("advertiser"), max_len=120) or None
        try:
            wid = await pool.fetchval(
                """INSERT INTO notary_watches
                     (owner_id, channel_ref, msg_id, advertiser, promised_from,
                      promised_until, next_check_at)
                   VALUES ($1,$2,$3,$4, now(), now() + ($5 || ' hours')::interval, now())
                   RETURNING id""",
                uid, channel.lstrip("@"), msg_id, advertiser, str(hours))
        except Exception:
            log.exception("notary_create uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "id": wid})

    async def notary_detail(request: web.Request) -> web.Response:
        """Протокол наблюдения: факты, журнал и подпись."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wid = int(request.match_info["watch_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        row = await _safe_fetchrow(pool,
            "SELECT * FROM notary_watches WHERE id=$1 AND owner_id=$2", wid, uid)
        if not row:
            return _err("Наблюдение не найдено", 404)
        watch = dict(row)
        obs = await _safe_fetch(pool,
            "SELECT observed_at, state, views, geo, detail FROM notary_observations "
            "WHERE watch_id=$1 ORDER BY observed_at DESC LIMIT 500", wid)
        from services import notary as _nt, notary_observer as _no
        from datetime import datetime as _dtn, timezone as _tzn
        ordered = list(reversed([dict(o) for o in (obs or [])]))
        folded = _nt.fold(ordered, promised_from=watch["promised_from"],
                          promised_until=watch["promised_until"],
                          now=_dtn.now(_tzn.utc))
        burst = _nt.views_burst(ordered)
        sig = watch.get("cert_sig")
        # Подпись показываем только там, где она проверяема: пересчитываем её
        # заново и сверяем. Протокол с неподтверждённой подписью хуже, чем без.
        payload = _nt.certificate_payload(watch, folded)
        verified = bool(sig) and _no.verify(payload, sig)
        journal = []
        for o in (obs or []):
            d = dict(o)
            d["observed_at"] = d["observed_at"].isoformat() if d.get("observed_at") else None
            journal.append(d)
        return _json_resp({
            "ok": True,
            "watch": {
                "id": watch["id"],
                "channel_ref": watch["channel_ref"],
                "channel_title": watch.get("channel_title"),
                "msg_id": watch.get("msg_id"),
                "advertiser": watch.get("advertiser"),
                "status": watch.get("status"),
                "promised_from": watch["promised_from"].isoformat(),
                "promised_until": watch["promised_until"].isoformat(),
            },
            "verdict": folded["verdict"],
            "verdict_label": _nt.VERDICT_LABEL.get(folded["verdict"], ""),
            "verdict_hint": _nt.VERDICT_HINT.get(folded["verdict"], ""),
            "facts": {
                "first_seen_at": folded["first_seen_at"].isoformat() if folded["first_seen_at"] else None,
                "last_seen_at": folded["last_seen_at"].isoformat() if folded["last_seen_at"] else None,
                "absent_since": folded["absent_since"].isoformat() if folded["absent_since"] else None,
                "held_min_h": folded["held_min_h"],
                "views_first": folded["views_first"],
                "views_last": folded["views_last"],
                "coverage": folded["coverage"],
                "n_present": folded["n_present"],
                "n_absent": folded["n_absent"],
                "n_unknown": folded["n_unknown"],
                "n_total": folded["n_total"],
            },
            "burst": burst,
            "signed": bool(sig),
            "signature_valid": verified,
            "certificate": _nt.render_certificate(
                watch, folded, sig if verified else None, burst),
            "journal": journal,
        })

    async def notary_check_now(request: web.Request) -> web.Response:
        """Проверить размещение немедленно, не дожидаясь такта наблюдателя."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wid = int(request.match_info["watch_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        row = await _safe_fetchrow(pool,
            "SELECT * FROM notary_watches WHERE id=$1 AND owner_id=$2", wid, uid)
        if not row:
            return _err("Наблюдение не найдено", 404)
        from services import notary_observer as _no
        try:
            state = await _no.observe(pool, dict(row))
        except PermissionError as exc:
            return _err(str(exc) or "Недоступно на вашем тарифе", 403)
        except Exception:
            log.exception("notary_check_now uid=%s watch=%s", uid, wid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "state": state})

    async def notary_cancel(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wid = int(request.match_info["watch_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        # Журнал наблюдений не трогаем: протокол, который можно стереть, ничего
        # не стоит. Снимаем только с наблюдения.
        row = await _safe_fetchrow(pool,
            "UPDATE notary_watches SET status='cancelled', updated_at=now() "
            "WHERE id=$1 AND owner_id=$2 AND status='active' RETURNING id", wid, uid)
        if not row:
            return _err("Наблюдение не найдено или уже закрыто", 404)
        return _json_resp({"ok": True})

    async def strike_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            # В работе: strike-операции из очереди (pending/running) — есть прогресс,
            # но ещё нет итоговых метрик.
            active = await _safe_fetch(pool,
                """SELECT id, COALESCE(label, op_type) AS label, status,
                          total_items, done_items, created_at, scheduled_for, params
                   FROM operation_queue
                   WHERE owner_id=$1 AND op_type LIKE 'strike%'
                     AND status IN ('pending','running')
                   ORDER BY created_at DESC LIMIT 15""",
                uid)
            # Завершённые: из таблицы strike_history — с РЕАЛЬНЫМИ метриками
            # эффективности (жалобы, забанен ли таргет). Раньше endpoint их не читал,
            # поэтому UI показывал только «N/N» и Strike казался «неэффективным».
            done = await _safe_fetch(pool,
                """SELECT id, target, reason, accounts_used, peer_reported,
                          msgs_reported, pinned_reported, admins_reported,
                          network_reports, blocked, verified_down, spambot_escalation,
                          duration_s, created_at
                   FROM strike_history
                   WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 30""",
                uid)
        except Exception:
            log.exception("strike_history uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

        import datetime as _dt
        _now = _dt.datetime.now(_dt.timezone.utc)
        operations = []
        for r in (active or []):
            # Отложенный заход настойчивой эскалации: pending + scheduled_for в будущем
            # + persist в params. Показываем как «под эскалацией», а не просто «в очереди».
            _params = r["params"]
            if isinstance(_params, str):
                try:
                    _params = _json.loads(_params or "{}")
                except Exception:
                    _params = {}
            _params = _params or {}
            _sched = r["scheduled_for"]
            _is_escalation = bool(
                _params.get("persist") and _sched and _sched > _now
            )
            operations.append({
                "id": r["id"],
                "label": r["label"] or "Strike",
                "status": r["status"] or "pending",
                "total": r["total_items"] or 0,
                "processed": r["done_items"] or 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "in_progress": True,
                "escalation": _is_escalation,
                "escalation_round": int(_params.get("strike_chain") or 0) if _is_escalation else 0,
                "scheduled_for": _sched.isoformat() if _sched else None,
                "target": _params.get("target"),
            })
        for r in (done or []):
            # Суммарное число доставленных жалоб по всем векторам.
            reports = (int(r["peer_reported"] or 0) + int(r["msgs_reported"] or 0)
                       + int(r["pinned_reported"] or 0) + int(r["admins_reported"] or 0)
                       + int(r["network_reports"] or 0))
            operations.append({
                "id": f"h{r['id']}",
                "label": f"Strike: {r['target']}" + (f" [{r['reason']}]" if r["reason"] else ""),
                "status": "done",
                "target": r["target"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "in_progress": False,
                "metrics": {
                    "accounts_used": int(r["accounts_used"] or 0),
                    "reports_total": reports,
                    "peer_reported": int(r["peer_reported"] or 0),
                    "msgs_reported": int(r["msgs_reported"] or 0),
                    "admins_reported": int(r["admins_reported"] or 0),
                    "network_reports": int(r["network_reports"] or 0),
                    "blocked": bool(r["blocked"]),
                    # Трёхзначный статус цели: снята / активна / не проверено —
                    # bool() терял разницу между «подтверждённо жива» и «не проверяли».
                    "verified_down": bool(r["verified_down"]),
                    "verified_status": (
                        "down" if r["verified_down"] is True
                        else "up" if r["verified_down"] is False
                        else "unknown"
                    ),
                    "spambot_escalation": bool(r["spambot_escalation"]),
                    "duration_s": int(r["duration_s"] or 0),
                },
            })
        # Сортируем объединённый список по времени (свежие сверху).
        operations.sort(key=lambda o: o["created_at"] or "", reverse=True)
        return _json_resp({"operations": operations[:40]})

    async def strike_status(request: web.Request) -> web.Response:
        """Проверяет доступ к Strike и возвращает список категорий."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            row = await pool.fetchrow(
                "SELECT purchased_at, mode FROM strike_access WHERE user_id=$1", uid
            )
            from services.strike_engine import MINI_CATEGORIES
            categories = [
                {"key": k, "label": v["label"], "severity": v.get("severity", "MEDIUM")}
                for k, v in MINI_CATEGORIES.items()
            ]
            # Юридический вектор (письма abuse@/dmca@ Telegram, CSAM→NCMEC) — это
            # то, что РЕАЛЬНО приводит к удалению канала, в отличие от массовых
            # in-app репортов. Он работает только при подключённых SMTP-ящиках;
            # без них Strike бьёт вхолостую слабым вектором. Отдаём флаг, чтобы
            # предупредить ДО запуска.
            try:
                _email_cnt = await pool.fetchval(
                    "SELECT COUNT(*) FROM strike_email_accounts WHERE owner_id=$1 AND is_active=TRUE",
                    uid,
                )
            except Exception:
                _email_cnt = None  # таблицы может не быть — не блокируем статус
            return _json_resp({
                "has_access": row is not None,
                "mode": row["mode"] if row else None,
                "purchased_at": row["purchased_at"].isoformat() if row and row["purchased_at"] else None,
                "categories": categories,
                "email_vector": {
                    "configured": bool(_email_cnt) if _email_cnt is not None else None,
                    "accounts": int(_email_cnt or 0) if _email_cnt is not None else 0,
                },
            })
        except Exception:
            log.exception("strike_status uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def strike_takedown_kit(request: web.Request) -> web.Response:
        """Готовый «пакет жалобы» по цели: App Store/Google Play/DMCA/NCMEC + текст.

        Это честная версия «жалобы в стор»: авто-сабмит в Apple/Google невозможен
        (вход+captcha), поэтому отдаём готовый текст + куда подать. Именно этот
        вектор реально удаляет/блокирует канал, в отличие от массовых in-app жалоб.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            target = (request.query.get("target") or "").strip()
            reason = (request.query.get("reason") or "other").strip()
            if not target or len(target) < 2:
                return _err("Укажите цель", 400)
            from services.strike_engine import build_takedown_kit
            kit = build_takedown_kit(target, reason)
            return _json_resp(kit)
        except Exception:
            log.exception("strike_takedown_kit uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def strike_launch(request: web.Request) -> web.Response:
        """Создаёт Strike операцию и ставит её в очередь."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            target = (body.get("target") or "").strip()
            category = (body.get("category") or "").strip()
            if not target or len(target) < 3:
                return _err("Укажите цель (username или ссылку)", 400)
            if not category:
                return _err("Выберите категорию нарушения", 400)

            # Проверяем доступ
            row = await pool.fetchrow(
                "SELECT mode FROM strike_access WHERE user_id=$1", uid
            )
            if not row:
                return _err("Нет доступа к Strike. Необходима лицензия.", 403)

            from services.strike_engine import MINI_CATEGORIES
            cat = MINI_CATEGORIES.get(category)
            if not cat:
                return _err("Неизвестная категория", 400)

            # Нормализация target
            from services.account_manager import normalize_telegram_join_ref
            ref_kind, ref_value = normalize_telegram_join_ref(target)
            normalized = f"+{ref_value}" if ref_kind == "invite" else ref_value.lstrip("@")
            if not normalized or len(normalized) < 3:
                return _err("Некорректный username или ссылка", 400)

            # Настройки интенсивности (раньше UI слал только target+category):
            # num_waves — эшелонирование (движок читает из params), max_accounts —
            # сколько аккаунтов задействовать (меньше = щадящее, дольше живут).
            try:
                num_waves = max(1, min(5, int(body.get("num_waves") or 3)))
            except (TypeError, ValueError):
                num_waves = 3
            try:
                max_accounts = int(body.get("max_accounts") or 0)
            except (TypeError, ValueError):
                max_accounts = 0
            acc_limit = max_accounts if 0 < max_accounts <= 50 else 50
            # Анти-детект повтор-интервал (движок читает min_restrike_hours из params;
            # 0 = без ограничения) и сознательный обход интервала (force). Раньше UI
            # их не слал — пользователь упирался в «цель атакована в последние 4ч,
            # повтор пропущен» без единой ручки, чтобы это изменить.
            try:
                min_restrike_hours = int(body.get("min_restrike_hours", 4))
            except (TypeError, ValueError):
                min_restrike_hours = 4
            min_restrike_hours = max(0, min(168, min_restrike_hours))
            force = bool(body.get("force"))
            # Настойчивая эскалация: движок переповторяет заход по расписанию, пока
            # цель не снята или пока не исчерпан лимит попыток (реальные тейкдауны
            # идут во времени, а не одним залпом).
            persist = bool(body.get("persist"))

            # Подсчёт доступных аккаунтов
            accs = await pool.fetch(
                """SELECT id FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=true
                     AND """ + _acc_status.sql_not_dead() + """
                   LIMIT $2""",
                uid, acc_limit,
            )
            if not accs:
                return _err("Нет доступных активных аккаунтов для Strike", 400)

            # Создаём операцию в очереди (op_type='strike' — воркер знает этот тип)
            op_id = await _obus.submit(
                pool, uid, "strike",
                {
                    "target": normalized,
                    "reason": cat["tg_reason"],
                    "num_waves": num_waves,
                    "min_restrike_hours": min_restrike_hours,
                    "force": force,
                    "persist": persist,
                    "account_ids": [r["id"] for r in accs],
                },
                total_items=len(accs),
                label=f"Массовые жалобы: {normalized} [{cat['label']}] · {num_waves} волн")
            # Подписанная запись правового основания ДО запуска: жалоба подаётся
            # только по abuse-категории (гейт MINI_CATEGORIES выше), и этот факт
            # фиксируется в аудите вместе с целью — доказательная база кампании.
            try:
                from services import compliance_engine
                await compliance_engine.record(
                    pool, uid, None, "strike_legal_basis", "abuse_report",
                    op_id=op_id,
                    params={"target": normalized, "category": category,
                            "category_label": cat.get("label")})
            except Exception:
                log.debug("strike_launch: compliance record failed op=%s", op_id)
            return _json_resp({"ok": True, "operation_id": op_id, "accounts": len(accs), "num_waves": num_waves})
        except PermissionError as exc:
            # Отказ по тарифу (operation_bus.PlanRequiredError) или предохранитель
            # Ban Weather (ImmunityBlockedError) — это НЕ сбой: честный 403 с
            # причиной вместо сырого 500 с внутренним текстом.
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("strike_launch uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def _fg_owns(uid, bot_id):
        return bool(await _safe_count(
            pool, "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id, uid))

    async def fg_get(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        if not await _fg_owns(uid, bot_id):
            return _err("Не найдено", 404)
        cfg = await _fg.get_config(pool, bot_id)
        ep = await _fg.active_episode(pool, bot_id)
        if ep:
            ep = {"started_at": str(ep.get("started_at")), "last_at": str(ep.get("last_at")),
                  "peak_per_min": ep.get("peak_per_min"),
                  "suspected_count": ep.get("suspected_count"), "mode": ep.get("mode")}
        return _json_resp({"config": cfg, "episode": ep,
                           "suspect_count": await _fg.suspect_count(pool, bot_id)})

    async def fg_set(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _fg_owns(uid, bot_id):
            return _err("Не найдено", 404)
        try:
            cfg = await _fg.set_config(
                pool, bot_id, uid, mode=d.get("mode"),
                threshold_per_min=d.get("threshold_per_min"))
            return _json_resp({"ok": True, "config": cfg})
        except Exception:
            log.exception("fg_set uid=%s bot=%s", uid, bot_id)
            return _err(_INTERNAL_ERROR, 500)

    async def fg_flag_recent(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _fg_owns(uid, bot_id):
            return _err("Не найдено", 404)
        n = await _fg.flag_recent(pool, bot_id, int(d.get("minutes", 10) or 10))
        return _json_resp({"ok": True, "flagged": n})

    async def fg_purge(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _fg_owns(uid, bot_id):
            return _err("Не найдено", 404)
        n = await _fg.purge_suspects(pool, bot_id, hard=bool(d.get("hard", False)))
        return _json_resp({"ok": True, "purged": n})

    async def fg_unflag(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        if not await _fg_owns(uid, bot_id):
            return _err("Не найдено", 404)
        n = await _fg.unflag_all(pool, bot_id)
        return _json_resp({"ok": True, "unflagged": n})

    app.router.add_get("/api/miniapp/strike/history", strike_history)
    app.router.add_get("/api/miniapp/strike/status", strike_status)
    app.router.add_get("/api/miniapp/strike/takedown_kit", strike_takedown_kit)
    app.router.add_post("/api/miniapp/strike/launch", strike_launch)
    app.router.add_get("/api/miniapp/notary", notary_list)
    app.router.add_post("/api/miniapp/notary", notary_create)
    app.router.add_get("/api/miniapp/notary/{watch_id}", notary_detail)
    app.router.add_post("/api/miniapp/notary/{watch_id}/check", notary_check_now)
    app.router.add_delete("/api/miniapp/notary/{watch_id}", notary_cancel)
    app.router.add_get("/api/miniapp/flood/{bot_id}", fg_get)
    app.router.add_patch("/api/miniapp/flood/{bot_id}", fg_set)
    app.router.add_post("/api/miniapp/flood/{bot_id}/flag-recent", fg_flag_recent)
    app.router.add_post("/api/miniapp/flood/{bot_id}/purge", fg_purge)
    app.router.add_post("/api/miniapp/flood/{bot_id}/unflag", fg_unflag)
