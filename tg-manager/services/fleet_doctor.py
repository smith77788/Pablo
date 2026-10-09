"""Диагностика флота — «почему операции не исполняются».

Операция берёт аккаунты через resource_selector.select_all_active, который
последовательно отсеивает по фильтрам (активность → сессия → статус → кулдаун →
trust). Если на каком-то шаге отсеиваются ВСЕ — операция получает пустой список
и «ничего не исполняет», а пользователь не видит, ГДЕ обрыв. Этот доктор строит
ту же воронку и называет первый шаг, где флот обнуляется, + последние реальные
ошибки операций и залипшие состояния.

compute_funnel / verdict — ЧИСТЫЕ функции (юнит-тесты), diagnose — сбор из БД.
"""
from __future__ import annotations

import logging

from services import account_status as _acc_status

log = logging.getLogger(__name__)

# Воронка обязана повторять фильтры select_all_active слово в слово, иначе
# доктор врёт: со своим коротким списком он держал аккаунт со статусом
# deleted/frozen за живого, шаг «статус» показывал зелёным, а операция всё
# равно получала ноль целей — ровно та невидимость обрыва, против которой
# этот модуль и написан. Список — из общего словаря.
_DEAD = set(_acc_status.DEAD_STATUSES)
# Подписи «чем болен» — владелец читает по-русски, а в базе лежит код статуса.
# Подписи тоже общие: иначе новый статус в словаре приходит на экран кодом.
_DEAD_RU = dict(_acc_status.RU_LABEL)
# Пороги trust (0..1) как в flood_engine._ACTION_MIN_TRUST — операции ниже режут.
_TRUST_JOIN = 0.35
_TRUST_INVITE = 0.50


def compute_funnel(rows: list[dict]) -> dict:
    """Воронка отбора аккаунтов под операцию. ЧИСТАЯ функция.

    rows: [{is_active, has_session, acc_status, cd_active, trust_score, in_operation}]
    Каждый следующий счётчик — подмножество предыдущего (как фильтры select_all_active).
    """
    f = {"total": 0, "active": 0, "with_session": 0, "alive": 0,
         "not_cooldown": 0, "operable_join": 0, "operable_invite": 0,
         "in_operation": 0}
    for r in rows:
        f["total"] += 1
        if r.get("in_operation"):
            f["in_operation"] += 1
        if not r.get("is_active"):
            continue
        f["active"] += 1
        if not r.get("has_session"):
            continue
        f["with_session"] += 1
        if (r.get("acc_status") or "active") in _DEAD:
            continue
        f["alive"] += 1
        if r.get("cd_active"):
            continue
        f["not_cooldown"] += 1
        trust = r.get("trust_score")
        trust = 1.0 if trust is None else float(trust)
        if trust >= _TRUST_JOIN:
            f["operable_join"] += 1
        if trust >= _TRUST_INVITE:
            f["operable_invite"] += 1
    return f


# Шаги воронки в том же порядке, в каком их отсеивает select_all_active.
FUNNEL_STEPS = ("active", "with_session", "alive", "not_cooldown",
                "operable_join", "operable_invite")


def step_blockers(rows: list[dict], step: str) -> list[dict]:
    """Кто ДОШЁЛ до шага и не прошёл его. ЧИСТАЯ функция.

    Экран показывал «Готовы к инвайту: 0» и на этом заканчивался: какие именно
    аккаунты отсеялись и чем они больны — узнать было нечем, хотя это ровно то,
    что нужно, чтобы флот поехал. Условия здесь те же, что в compute_funnel,
    и меняться должны вместе с ней (стережёт тест паритета).
    """
    if step not in FUNNEL_STEPS:
        return []
    out: list[dict] = []
    for r in rows:
        if not r.get("is_active"):
            if step == "active":
                out.append(dict(r, reason="выключен"))
            continue
        if not r.get("has_session"):
            if step == "with_session":
                out.append(dict(r, reason="нет сессии"))
            continue
        st = (r.get("acc_status") or "active")
        if st in _DEAD:
            if step == "alive":
                out.append(dict(r, reason=_DEAD_RU.get(st, st)))
            continue
        if r.get("cd_active"):
            if step == "not_cooldown":
                out.append(dict(r, reason="на кулдауне"))
            continue
        trust = r.get("trust_score")
        trust = 1.0 if trust is None else float(trust)
        if step == "operable_join" and trust < _TRUST_JOIN:
            out.append(dict(r, reason=f"доверие {trust:.2f} ниже {_TRUST_JOIN:.2f}"))
        elif step == "operable_invite" and trust < _TRUST_INVITE:
            out.append(dict(r, reason=f"доверие {trust:.2f} ниже {_TRUST_INVITE:.2f}"))
    return out


def verdict(f: dict) -> dict:
    """Назвать первый шаг, где флот обнуляется, + человекочитаемое действие."""
    if f["total"] == 0:
        return {"key": "no_accounts", "ok": False,
                "text": "Нет аккаунтов — добавьте флот в разделе «Аккаунты»."}
    if f["active"] == 0:
        return {"key": "all_inactive", "ok": False,
                "text": "Все аккаунты выключены. Нажмите «🚀 Поднять флот»."}
    if f["with_session"] == 0:
        return {"key": "no_session", "ok": False,
                "text": "Ни у одного аккаунта нет сессии — переимпортируйте сессии."}
    if f["alive"] == 0:
        return {"key": "all_dead", "ok": False,
                "text": "Все сессии мертвы (бан, удаление аккаунта, отозванная сессия) — переподключите аккаунты."}
    if f["not_cooldown"] == 0:
        return {"key": "all_cooldown", "ok": False,
                "text": "Все аккаунты на кулдауне (флуд/ограничения). Подождите или «🚀 Поднять флот» (сброс кулдауна)."}
    if f["operable_join"] == 0:
        return {"key": "low_trust", "ok": False,
                "text": "Доверие всех аккаунтов ниже порога операций. «🚀 Поднять флот» вернёт его к рабочему уровню."}
    return {"key": "ok", "ok": True,
            "text": f"Флот готов: {f['operable_join']} для вступления, {f['operable_invite']} для инвайта/рассылки."}


def classify_errors(recent_errors: list[dict]) -> dict:
    """Определить ДОМИНИРУЮЩИЙ класс сбоя по текстам последних ошибок. ЧИСТАЯ.

    Возвращает {"class": transport|auth_dup|flood|banned|none, "count": n}.
    Это ловит случай, когда воронка отбора зелёная, но операции всё равно падают
    системно (обычно транспорт/подключение) — тогда вердикт нельзя ставить «готов».
    """
    cnt = {"transport": 0, "auth_dup": 0, "flood": 0, "banned": 0}
    for e in recent_errors or []:
        t = (e.get("error") or "").lower()
        if not t:
            continue
        if ("auth_key_duplicated" in t or "two different ip" in t
                or "duplicated" in t):
            cnt["auth_dup"] += 1
        elif ("сбой подключения" in t or "сбой подключения" in t
              or "подключени" in t or "транспорт" in t or "connect" in t
              or "proxy" in t or "прокси" in t or "timeout" in t
              or "таймаут" in t or "network" in t or "relay" in t):
            cnt["transport"] += 1
        elif "flood" in t or "флуд" in t:
            cnt["flood"] += 1
        elif "ban" in t or "бан" in t or "deactiv" in t:
            cnt["banned"] += 1
    dominant = max(cnt, key=lambda k: cnt[k]) if any(cnt.values()) else "none"
    return {"class": dominant if cnt.get(dominant, 0) else "none",
            "count": cnt.get(dominant, 0), "breakdown": cnt}


def _transport_verdict(err_class: str, transport: dict) -> dict | None:
    """Если воронка зелёная, но операции падают на подключении — назвать транспорт."""
    if err_class == "transport":
        return {"key": "transport_fail", "ok": False,
                "text": "Аккаунты отбираются, но ПОДКЛЮЧЕНИЕ системно падает "
                        "(сеть/прокси/CF-релей/IPv6). Назначьте аккаунтам прокси "
                        "или настройте CF-релей/IPv6 — на общем/недоступном выходе "
                        "флот не может подключиться к Telegram."}
    if err_class == "auth_dup":
        no_transport = transport.get("no_proxy", 0) and not (
            transport.get("cf_relay_configured") or transport.get("ipv6_configured"))
        extra = (" У большинства аккаунтов нет стабильного выходного IP — "
                 "назначьте прокси, иначе сессии видятся Telegram с разных IP."
                 if no_transport else "")
        return {"key": "auth_dup", "ok": False,
                "text": "AUTH_KEY_DUPLICATED: сессия используется с двух IP. Обычно "
                        "это скачущий выходной IP (нет стабильного прокси на аккаунт) "
                        "или параллельный коннект." + extra}
    return None


async def _transport_summary(pool, owner_id: int) -> dict:
    """Сколько аккаунтов имеют стабильный выход (прокси/релей/IPv6) vs прямой."""
    out = {"total": 0, "with_proxy": 0, "with_relay": 0, "direct_only": 0,
           "cf_relay_configured": False, "ipv6_configured": False, "global_proxy": False}
    try:
        from config import CF_RELAY_URL, TG_PROXY
        out["cf_relay_configured"] = bool(str(CF_RELAY_URL or "").strip())
        out["global_proxy"] = bool(str(TG_PROXY or "").strip())
    except Exception:
        pass
    try:
        import os as _os
        out["ipv6_configured"] = bool(_os.getenv("IPV6_SUBNET", "").strip())
    except Exception:
        pass
    # Считаем по колонкам защищённо — cf_relay_url может отсутствовать в старой схеме.
    for col, key in (("proxy_id IS NOT NULL", "with_proxy"),
                     ("cf_relay_url IS NOT NULL AND cf_relay_url <> ''", "with_relay")):
        try:
            out[key] = int(await pool.fetchval(
                f"SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active "
                f"AND {col}", owner_id) or 0)
        except Exception:
            out[key] = 0
    try:
        out["total"] = int(await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active", owner_id) or 0)
    except Exception:
        pass
    out["no_proxy"] = max(0, out["total"] - out["with_proxy"])
    out["direct_only"] = max(0, out["total"] - out["with_proxy"] - out["with_relay"])
    return out


async def diagnose(pool, owner_id: int) -> dict:
    """Полная диагностика: воронка + конфиг + залипшие op + последние ошибки."""
    out = {"funnel": compute_funnel([]), "verdict": {}, "config": {},
           "stuck": {}, "recent_errors": []}
    # Конфиг Telethon: без TG_API_ID/HASH не подключится НИ один аккаунт.
    try:
        from config import TG_API_ID, TG_API_HASH
        out["config"]["telethon_ready"] = bool(int(TG_API_ID or 0) and TG_API_HASH)
    except Exception:
        out["config"]["telethon_ready"] = None

    try:
        rows = await pool.fetch(
            "SELECT is_active, "
            "(session_str IS NOT NULL AND session_str <> '') AS has_session, "
            "COALESCE(acc_status,'active') AS acc_status, "
            "(cooldown_until IS NOT NULL AND cooldown_until > NOW()) AS cd_active, "
            "trust_score, COALESCE(in_operation, FALSE) AS in_operation "
            "FROM tg_accounts WHERE owner_id=$1", owner_id)
        f = compute_funnel([dict(r) for r in rows])
        out["funnel"] = f
        out["verdict"] = verdict(f)
    except Exception as e:
        log.warning("fleet_doctor.diagnose funnel failed owner=%s: %s", owner_id, e)
        out["verdict"] = {"key": "error", "ok": False, "text": "Не удалось собрать диагностику."}

    # Залипшие состояния, мешающие исполнению.
    try:
        out["stuck"]["running_ops"] = int(await pool.fetchval(
            "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='running'", owner_id) or 0)
        out["stuck"]["pending_ops"] = int(await pool.fetchval(
            "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='pending'", owner_id) or 0)
    except Exception:
        pass

    # Последние реальные ошибки операций — то, что видит пользователь как «не сработало».
    try:
        from services import op_status as _ost_reason

        # Причина по ОБЕИМ колонкам очереди. Раньше список брал только
        # error_msg и им же фильтровал, поэтому упавшая операция, чья причина
        # легла в last_error, из «последних реальных ошибок» просто исчезала:
        # владелец видел пустой список там, где сбои были.
        # Статус тоже по обоим недоведённым исходам: 'partial' — такая же
        # незакрытая работа, и её причина нужна в «последних реальных ошибках».
        _reason = _ost_reason.sql_error_reason()
        er = await pool.fetch(
            f"SELECT id, COALESCE(label, op_type) AS op, {_reason} AS error_msg, "
            "finished_at "
            "FROM operation_queue WHERE owner_id=$1 "
            f"AND status IN {_ost_reason.sql_unfinished_list()} "
            f"AND COALESCE({_reason}, '') <> '' "
            "ORDER BY finished_at DESC NULLS LAST LIMIT 5", owner_id)
        out["recent_errors"] = [
            {"id": r["id"], "op": r["op"], "error": (r["error_msg"] or "")[:200],
             "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None}
            for r in er]
    except Exception:
        pass

    # Транспорт + класс ошибок: воронка может быть зелёной, но операции падают
    # системно на ПОДКЛЮЧЕНИИ — тогда переопределяем «готов» на реальный диагноз.
    out["transport"] = await _transport_summary(pool, owner_id)
    out["error_class"] = classify_errors(out.get("recent_errors", []))
    try:
        if out["verdict"].get("ok"):
            tv = _transport_verdict(out["error_class"]["class"], out["transport"])
            if tv:
                out["verdict"] = tv
    except Exception:
        pass
    return out
