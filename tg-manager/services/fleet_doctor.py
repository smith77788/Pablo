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

log = logging.getLogger(__name__)

_DEAD = {"banned", "spamblock", "deactivated", "session_expired"}
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


def verdict(f: dict) -> dict:
    """Назвать первый шаг, где флот обнуляется, + человекочитаемое действие."""
    if f["total"] == 0:
        return {"key": "no_accounts", "ok": False,
                "text": "Нет аккаунтов — добавьте флот в разделе «Аккаунты»."}
    if f["active"] == 0:
        return {"key": "all_inactive", "ok": False,
                "text": "Все аккаунты выключены (is_active=FALSE). Нажмите «🚀 Поднять флот»."}
    if f["with_session"] == 0:
        return {"key": "no_session", "ok": False,
                "text": "Ни у одного аккаунта нет сессии — переимпортируйте сессии."}
    if f["alive"] == 0:
        return {"key": "all_dead", "ok": False,
                "text": "Все сессии мертвы (бан/деактивация/session_expired) — переподключите аккаунты."}
    if f["not_cooldown"] == 0:
        return {"key": "all_cooldown", "ok": False,
                "text": "Все аккаунты на кулдауне (флуд/ограничения). Подождите или «🚀 Поднять флот» (сброс кулдауна)."}
    if f["operable_join"] == 0:
        return {"key": "low_trust", "ok": False,
                "text": "Trust всех аккаунтов ниже порога операций. «🚀 Поднять флот» вернёт траст к рабочему уровню."}
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
        er = await pool.fetch(
            "SELECT COALESCE(label, op_type) AS op, error_msg, finished_at "
            "FROM operation_queue WHERE owner_id=$1 AND status='failed' "
            "AND error_msg IS NOT NULL AND error_msg <> '' "
            "ORDER BY finished_at DESC NULLS LAST LIMIT 5", owner_id)
        out["recent_errors"] = [
            {"op": r["op"], "error": (r["error_msg"] or "")[:200]} for r in er]
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
