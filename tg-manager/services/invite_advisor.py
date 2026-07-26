"""Аналитик инвайтинга: разбор фактов и конкретные рекомендации.

Аналитика (`/invite/analytics`) отвечает на вопрос «что произошло»: столько-то
успешных, столько-то флудов, вот график за неделю. Она не отвечает на вопрос, с
которым пользователь к ней и приходит, — «и что мне теперь делать». Цифры сами
по себе не подсказывают, что три конкретных аккаунта пора вывести из ротации, что
у половины флота пустой профиль и он поэтому режет себе лимит, или что аккаунтов
физически не хватает под заявленную аудиторию.

Почему разбор ДЕТЕРМИНИРОВАННЫЙ, а не через языковую модель
────────────────────────────────────────────────────────────
Рекомендации здесь оперируют числами, по которым пользователь принимает решения о
живых аккаунтах: сколько осталось лимита, сколько было флудов, скольких аккаунтов
не хватает. Языковая модель эти числа уверенно переформулирует — и так же
уверенно ошибётся в них, а проверить их пользователю неоткуда. Поэтому и факты, и
выводы считаются по БД, и каждая рекомендация несёт числа, на которых построена:
её можно перепроверить. Модель тут добавила бы интонацию ценой достоверности —
плохой обмен для слоя, где ошибка стоит аккаунтов.

Каждая рекомендация: {severity, title, detail, accounts?}. `severity` —
"danger" | "warn" | "info" | "good". Пустой список значит «поводов вмешаться
нет», и это тоже честный ответ; отсутствие ДАННЫХ сообщается отдельно.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Ниже этой конверсии за неделю аудиторию есть смысл пересобрать, а не долбить.
_POOR_CONVERSION = 0.5
# Сколько дней держать «проблемным» аккаунт, словивший флуд.
_FLOOD_REST_DAYS = 3


def _label(row) -> str:
    return str(row.get("label") or f"#{row.get('account_id') or row.get('id')}")


async def build_advice(pool, owner_id: int, *, audience_size: int = 0) -> dict:
    """Собрать рекомендации по инвайтингу для владельца.

    `audience_size` — размер аудитории, которую пользователь СОБИРАЕТСЯ
    приглашать (0 = не спрашивали). Нужен для единственной рекомендации, которую
    нельзя дать по прошлому: хватит ли флота на задуманное.

    Никогда не бросает: аналитик, роняющий экран, хуже отсутствующего.
    """
    out: dict = {"has_data": False, "advice": [], "checked": []}
    if not pool or not owner_id:
        return out

    advice: list[dict] = []
    checked: list[str] = []

    week = await _fetch(
        pool,
        """SELECT s.account_id,
                  COALESCE(a.phone, a.first_name, '#' || a.id::text) AS label,
                  COALESCE(SUM(s.invites_ok), 0)   AS ok,
                  COALESCE(SUM(s.actions_fail), 0) AS failed,
                  COALESCE(SUM(s.flood_events), 0) AS floods
           FROM account_daily_stats s
           JOIN tg_accounts a ON a.id = s.account_id
           WHERE a.owner_id = $1 AND s.stat_date >= CURRENT_DATE - INTERVAL '6 days'
           GROUP BY s.account_id, a.phone, a.first_name, a.id""",
        owner_id,
    )
    out["has_data"] = bool(week)

    # ── 1. Кто ловит флуды ───────────────────────────────────────────────────
    checked.append("флуды за неделю")
    flooded = sorted([r for r in week if int(r["floods"] or 0) > 0],
                     key=lambda r: -int(r["floods"]))
    if flooded:
        total_floods = sum(int(r["floods"]) for r in flooded)
        advice.append({
            "severity": "danger",
            "title": f"{len(flooded)} аккаунт(ов) ловят флуд — выведите их из ротации",
            "detail": (
                f"За неделю {total_floods} флуд-событий. Аккаунт, уже помеченный "
                f"Telegram, при следующей операции добивается быстрее всего: дайте "
                f"ему отдохнуть {_FLOOD_REST_DAYS}+ дн. и не включайте в инвайт."
            ),
            "accounts": [{"id": r["account_id"], "label": _label(r),
                          "floods": int(r["floods"])} for r in flooded[:5]],
        })

    # ── 2. Конверсия: аудитория или темп ─────────────────────────────────────
    checked.append("конверсия аудитории")
    ok = sum(int(r["ok"] or 0) for r in week)
    failed = sum(int(r["failed"] or 0) for r in week)
    attempts = ok + failed
    if attempts >= 20:
        conv = ok / attempts
        if conv < _POOR_CONVERSION:
            advice.append({
                "severity": "warn",
                "title": f"Конверсия {round(conv * 100)}% — проблема в аудитории, не в темпе",
                "detail": (
                    f"Из {attempts} попыток прошло {ok}. Отказы на инвайте — это "
                    f"почти всегда закрытые настройки приватности у целей, а не "
                    f"скорость. Замедление тут не поможет: пересоберите аудиторию "
                    f"(активные за последние дни, без ботов) — иначе флот тратит "
                    f"суточные лимиты на заведомо недостижимых людей."
                ),
            })
        elif conv >= 0.8 and not flooded:
            advice.append({
                "severity": "good",
                "title": f"Неделя чистая: конверсия {round(conv * 100)}%, флудов нет",
                "detail": (
                    "Лимиты можно наращивать — движок сделает это сам, если оставить "
                    "темп «Авто». Ручное ускорение поверх чистой недели смысла не "
                    "имеет: рекомендованный суточный лимит и так растёт."
                ),
            })

    # ── 3. Профиль аккаунтов сам режет лимит ─────────────────────────────────
    checked.append("оформление профилей")
    weak = await _fetch(
        pool,
        """SELECT id, COALESCE(phone, first_name, '#' || id::text) AS label,
                  first_name, username, has_photo, warmup_level
           FROM tg_accounts
           WHERE owner_id = $1 AND is_active = TRUE AND session_str IS NOT NULL""",
        owner_id,
    )
    empty = [r for r in weak
             if not (r["first_name"] or "").strip() or not (r["username"] or "").strip()
             or r["has_photo"] is False]
    if empty:
        advice.append({
            "severity": "warn",
            "title": f"{len(empty)} аккаунт(ов) с незаполненным профилем",
            "detail": (
                "Пустое имя, отсутствие username или аватара — прямой спам-признак, "
                "и риск-движок за него режет суточный лимит именно этим аккаунтам. "
                "Оформление разовое, а лимит поднимет навсегда."
            ),
            "accounts": [{"id": r["id"], "label": _label({**dict(r), "account_id": r["id"]})}
                         for r in empty[:5]],
        })

    cold = [r for r in weak if int(r["warmup_level"] or 0) <= 0]
    if cold:
        advice.append({
            "severity": "warn",
            "title": f"{len(cold)} аккаунт(ов) не прогреты",
            "detail": (
                "Аккаунт без истории обычных действий, который сразу начинает "
                "приглашать людей, — самый частый кандидат на ограничение. "
                "Прогрев запускается один раз и снимает срез лимита."
            ),
            "accounts": [{"id": r["id"], "label": _label({**dict(r), "account_id": r["id"]})}
                         for r in cold[:5]],
        })

    # ── 4. Хватит ли флота на задуманное ─────────────────────────────────────
    checked.append("запас суточных лимитов")
    capacity, per_acc = 0, []
    try:
        from services.flood_engine import recommended_daily_limit
        for r in weak:
            info = await recommended_daily_limit(pool, int(r["id"]))
            rem = int(info.get("remaining") or 0)
            capacity += rem
            per_acc.append((r, rem))
    except Exception:
        log.debug("invite_advisor: limits unavailable owner=%s", owner_id, exc_info=True)
        per_acc = []

    exhausted = [r for r, rem in per_acc if rem <= 0]
    if exhausted:
        advice.append({
            "severity": "info",
            "title": f"{len(exhausted)} аккаунт(ов) исчерпали суточный лимит",
            "detail": (
                "Сегодня они будут пропущены — это не ошибка, а защита. Лимит "
                "обнуляется в начале следующих суток."
            ),
            "accounts": [{"id": r["id"], "label": _label({**dict(r), "account_id": r["id"]})}
                         for r in exhausted[:5]],
        })

    if audience_size and per_acc:
        if capacity < audience_size:
            need = audience_size - capacity
            advice.append({
                "severity": "warn",
                "title": f"Флота не хватит: {capacity} из {audience_size} за сегодня",
                "detail": (
                    f"Остаток суточных лимитов по всем аккаунтам — {capacity}. "
                    f"Оставшиеся {need} целей уедут на следующие дни (они не "
                    f"потеряются, очередь их сохранит). Чтобы закрыть сегодня, "
                    f"нужны ещё аккаунты — поднимать лимит вручную опаснее, чем "
                    f"разнести по дням."
                ),
            })

    # ── 5. Изоляция: несколько аккаунтов с одного IP ─────────────────────────
    checked.append("изоляция по IP")
    try:
        from services.proxy_selector import audit_proxy_isolation
        iso = await audit_proxy_isolation(pool, owner_id)
        shared = iso.get("shared_ip_groups") or []
        naked = iso.get("accounts_without_proxy") or []
        if shared:
            worst = max(int(g.get("count") or 0) for g in shared)
            advice.append({
                "severity": "danger",
                "title": f"{len(shared)} IP используются несколькими аккаунтами",
                "detail": (
                    f"На одном адресе до {worst} аккаунтов. Для инвайта это худший "
                    f"из рисков: связка вскрывается не по одному аккаунту, а сразу "
                    f"по всей группе с общего IP — и ограничение прилетает пачкой."
                ),
            })
        if naked:
            advice.append({
                "severity": "danger",
                "title": f"{len(naked)} аккаунт(ов) работают без прокси",
                "detail": (
                    "Без прокси аккаунт ходит с общего адреса сервера вместе с "
                    "остальными — изоляции нет вообще. Назначьте прокси до запуска "
                    "массовой операции."
                ),
            })
    except Exception:
        log.debug("invite_advisor: isolation audit unavailable owner=%s", owner_id,
                  exc_info=True)

    out["advice"] = advice
    out["checked"] = checked
    out["capacity_today"] = capacity
    return out


async def _fetch(pool, q: str, *args) -> list[dict]:
    """Запрос, который не имеет права уронить аналитика."""
    try:
        rows = await pool.fetch(q, *args)
        return [dict(r) for r in (rows or [])]
    except Exception:
        log.debug("invite_advisor: query failed", exc_info=True)
        return []
