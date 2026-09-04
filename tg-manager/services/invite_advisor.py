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

import asyncio
import logging

log = logging.getLogger(__name__)

# Сколько проверок аккаунтов делаем одновременно. Разбор запрашивается из
# интерактивного экрана, а проверок ровно две на аккаунт (карантин + остаток
# лимита) — последовательно это 400 round-trip'ов на флоте в 200 аккаунтов, то
# есть секунды ожидания на ровном месте. Потолок держим ниже размера пула
# соединений: разбор — вспомогательный экран и не вправе занять пул целиком.
_PROBE_CONCURRENCY = 8


async def _map_bounded(items, fn):
    """Применить асинхронную fn ко всем items с ограничением параллелизма.

    Порядок результатов совпадает с порядком items — рекомендации перечисляют
    аккаунты, и переставленный порядок сбивал бы оператора при сравнении с
    предыдущим открытием экрана.
    """
    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _one(it):
        async with sem:
            return await fn(it)

    return await asyncio.gather(*[_one(i) for i in items])

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

    # ── 0. Операции вообще запускаются? ──────────────────────────────────────
    # Предохранитель op_worker останавливает ВСЕ операции владельца на 30 минут
    # после серии сбоев. Состояние отдавалось эндпойнтом `/circuit_breaker`, но фронт
    # его никогда не спрашивал: операция уходила в «ожидает» и не стартовала без
    # единого объяснения — снаружи это и есть «ничего не работает». Здесь пауза
    # становится видимой первым же пунктом разбора.
    checked.append("пауза после сбоев")
    try:
        # ОБЩЕЕ состояние (БД): разбор запрашивается из мини-аппа, а он может
        # обслуживаться процессом, который операций не запускал — в его памяти
        # предохранитель всегда «закрыт», и пауза осталась бы невидимой.
        from services.op_worker import circuit_breaker_status
        cb = await circuit_breaker_status(int(owner_id))
        if cb.get("status") == "open":
            mins = max(1, int(cb.get("cooldown_remaining_s") or 0) // 60)
            advice.append({
                "severity": "danger",
                "title": f"Операции на паузе ещё ~{mins} мин — новые запуски ждут",
                "detail": (
                    f"После {cb.get('failures')} сбоев подряд система приостановила "
                    f"запуск операций, чтобы не жечь аккаунты вслепую. Запущенное "
                    f"сейчас не пропадёт — оно стартует само, когда пауза кончится. "
                    f"Если сбои были из-за настроек (пустая аудитория, не указана "
                    f"группа), просто дождитесь окончания паузы."
                ),
            })
    except Exception:
        log.debug("invite_advisor: circuit breaker state unavailable", exc_info=True)

    week_rows = await _fetch(
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
    week = week_rows or []
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
    weak_rows = await _fetch(
        pool,
        """SELECT id, COALESCE(phone, first_name, '#' || id::text) AS label,
                  first_name, username, has_photo, warmup_level
           FROM tg_accounts
           WHERE owner_id = $1 AND is_active = TRUE AND session_str IS NOT NULL""",
        owner_id,
    )
    weak = weak_rows or []
    # Ни одного пригодного аккаунта — самая частая причина «инвайт не работает»:
    # операция честно падает с «Нет активных аккаунтов-инвайтеров с сессией», но
    # до этого момента ничто на экране об этом не предупреждало.
    if weak_rows is not None and not weak:
        advice.append({
            "severity": "danger",
            "title": "Нет аккаунтов, которыми можно приглашать",
            "detail": (
                "Инвайту нужен активный аккаунт с рабочей сессией. Сейчас таких "
                "нет: либо аккаунты не добавлены, либо их сессии не сохранились "
                "или истекли. Операция запустится и сразу завершится отказом — "
                "добавьте аккаунт и проверьте его в разделе «Аккаунты»."
            ),
        })

    # Карантин: аккаунты есть, но исполнитель их пропустит (риск-пульс). Если
    # пропустит ВСЕХ — инвайт снова завершится отказом, и снова без предупреждения.
    checked.append("карантин аккаунтов")
    if weak:
        try:
            from services import infra_memory as _im

            _flags = await _map_bounded(
                weak, lambda r: _im.is_account_quarantined(pool, r["id"]))
            quarantined = [r for r, flag in zip(weak, _flags) if flag]
            if quarantined and len(quarantined) == len(weak):
                advice.append({
                    "severity": "danger",
                    "title": "Все аккаунты на паузе риск-пульса — приглашать нечем",
                    "detail": (
                        "Каждый аккаунт помечен как рискованный (флуды, ограничения "
                        "или низкий trust) и будет пропущен для защиты от бана. "
                        "Инвайт завершится отказом, пока не появится хотя бы один "
                        "здоровый аккаунт — дайте текущим отдохнуть или добавьте новые."
                    ),
                })
            elif quarantined:
                advice.append({
                    "severity": "warn",
                    "title": f"{len(quarantined)} из {len(weak)} аккаунтов на паузе риск-пульса",
                    "detail": (
                        "Их пропустят для защиты от бана — реальная ёмкость инвайта "
                        "ниже, чем кажется по числу аккаунтов."
                    ),
                    "accounts": [{"id": r["id"], "label": _label({**dict(r), "account_id": r["id"]})}
                                 for r in quarantined[:5]],
                })
        except Exception:
            log.debug("invite_advisor: quarantine check unavailable", exc_info=True)

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

        _infos = await _map_bounded(
            weak, lambda r: recommended_daily_limit(pool, int(r["id"])))
        for r, info in zip(weak, _infos):
            rem = int((info or {}).get("remaining") or 0)
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


async def _fetch(pool, q: str, *args) -> list[dict] | None:
    """Запрос, который не имеет права уронить аналитика.

    Возвращает None, если запрос НЕ УДАЛСЯ. Это принципиально не то же самое,
    что пустой список: «аккаунтов нет» и «мы не смогли их спросить» — разные
    факты, и путать их нельзя. Ранняя версия возвращала [] в обоих случаях, и
    при сбое БД разбор уверенно сообщал «нет аккаунтов, которыми можно
    приглашать» — врал с видом точности ровно там, где пользователь принимает
    решение.
    """
    try:
        rows = await pool.fetch(q, *args)
        return [dict(r) for r in (rows or [])]
    except Exception:
        log.debug("invite_advisor: query failed", exc_info=True)
        return None
