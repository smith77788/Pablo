"""Авто-реабилитация ограниченных аккаунтов: спам-блок → тихий прогрев →
перепроверка → возврат в строй.

Проблема (по аудиту). Аккаунт под спам-блоком выпадал из ВСЕГО: операции его не
берут (ресурс-селектор фильтрует dead-статусы), и даже штатный прогрев его
пропускает (`run_warmup_session` гейтит `spamblock`). Итог — ограниченный аккаунт
замирал навсегда и ждал ручного вмешательства владельца. Восстановление было
чисто реактивным.

Решение — стейт-машина на аккаунт (`account_rehab_state`), которую крутит фоновый
цикл `run_rehab_loop`:

    appeal   — запросить снятие у @SpamBot (свой аккаунт, легитимная аппеляция);
    warming  — тихий ПАССИВНЫЙ прогрев: только чтение каналов/профилей/presence,
               без рассылок и инвайтов (при спам-блоке это разрешено и держит
               аккаунт «живым», человекоподобным);
    recheck  — перепроверка статуса через @SpamBot;
    freed    — снят: acc_status → 'active', аккаунт вернулся в строй;
    stuck    — вечный блок и аппеляции исчерпаны → нужен ручной разбор/перезаливка;
    gone     — забанен/удалён/сессия истекла → реабилитировать нечего.

Осторожность заложена в темп: трогаем не более `_MAX_PER_TICK` аккаунтов за тик и
разносим действия во времени (jitter), чтобы не создавать всплеск одинаковых системных
событий по всему флоту (тот самый фингерпринт, который ловит анти-спам).

Ничего не рассылает третьим лицам: только диалог с системным @SpamBot и пассивное
чтение публичных каналов. Fail-soft: любой сбой по аккаунту не роняет цикл.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Темп/пороги ───────────────────────────────────────────────────────────────
_TICK_SEC = 900              # период цикла (15 мин)
_MAX_PER_TICK = 3            # сколько аккаунтов трогаем за один тик (анти-всплеск)
_WARM_GAP_SEC = 40 * 60      # базовый разнос между действиями по аккаунту (jitter ниже)
_WARM_CYCLES_BEFORE_RECHECK = 2   # столько прогревов перед первой перепроверкой
_WARM_ACTIONS_MIN = 3        # пассивных действий за один прогрев
_WARM_ACTIONS_MAX = 6
_MAX_APPEALS = 2             # сколько раз просим снятия у @SpamBot
_MAX_RECHECKS = 8            # после стольких безуспешных перепроверок вечного — stuck
# Бэк-офф перепроверок по числу попыток (часы). Временный блок снимается по сроку —
# частить бессмысленно и подозрительно; растягиваем интервал.
_RECHECK_BACKOFF_H = (6, 12, 24, 24, 48, 48, 72, 72)

_DEAD_STATUSES = ("banned", "deactivated", "deleted", "session_expired")

# Заморозка — не «мёртвый» и не спам-блок. Аккаунт в режиме «только чтение»
# снимается ТОЛЬКО апелляцией, которую подаёт сам владелец: @SpamBot ведёт в
# анкету (ФИО, почта, причина) и капчу «подтвердите, что вы человек». Прогрев
# и автоаппеляция тут бессмысленны (а нажатие «This is a mistake» автоматикой
# бросило бы анкету на полпути), поэтому реабилитация передаёт такой аккаунт
# человеку — фаза 'stuck', но с честной причиной, а не «стоит заменить».
_FROZEN_NOTE = ("заморожен Telegram — апелляцию подаёт владелец сам в @SpamBot, "
                "иначе аккаунт удалят после даты из сообщения")


def _disabled() -> bool:
    return os.getenv("INFRAGRAM_DISABLE_REHAB", "").strip().lower() in ("1", "true", "yes")


def _jitter(base: int, frac: float = 0.35) -> int:
    """Разброс интервала ±frac — чтобы действия флота не выстраивались в ритм."""
    d = int(base * frac)
    return max(60, base + random.randint(-d, d))


def _backoff_sec(attempts: int, pace: str = "normal") -> int:
    idx = min(attempts, len(_RECHECK_BACKOFF_H) - 1)
    base = _RECHECK_BACKOFF_H[idx] * 3600 * PACE_MULT.get(pace, 1.0)
    # Даже «быстрый» темп не перепроверяет чаще пола: частые запросы к
    # @SpamBot сами по себе выглядят подозрительно и блок не снимают.
    return max(_MIN_RECHECK_GAP_SEC, _jitter(int(base), frac=0.25))


def _warm_gap(pace: str = "normal") -> int:
    return max(_MIN_WARM_GAP_SEC,
               _jitter(int(_WARM_GAP_SEC * PACE_MULT.get(pace, 1.0))))


# ── Настройки владельца ───────────────────────────────────────────────────────
# Раньше всё было зашито в код: темп, число перепроверок, аппеляция, отдых
# после снятия. Владелец не мог ни притормозить восстановление на время
# волны блокировок, ни запретить автоматике писать @SpamBot, ни выключить
# уведомления. Значения по умолчанию совпадают с прежним поведением.
PACE_MULT = {"careful": 1.6, "normal": 1.0, "fast": 0.6}
PACE_LABEL = {
    "careful": "Бережный — реже действия и проверки",
    "normal": "Обычный",
    "fast": "Быстрый — чаще, но не чаще безопасного предела",
}
_MIN_RECHECK_GAP_SEC = 3 * 3600
_MIN_WARM_GAP_SEC = 20 * 60
_MAX_RECHECKS_RANGE = (3, 16)
_REST_HOURS_RANGE = (0, 168)
# Предохранитель: даже если Telegram назвал дату снятия, бесконечно ходить по
# кругу нельзя — после стольких перепроверок аккаунт отдаётся человеку.
_HARD_MAX_RECHECKS = 40

DEFAULT_SETTINGS: dict[str, Any] = {
    "paused": False,         # всё восстановление на паузе
    "auto_appeal": True,     # автоматика сама пишет @SpamBot «это ошибка»
    "pace": "normal",
    "max_rechecks": _MAX_RECHECKS,
    "rest_hours": 24,        # отдых после снятия: операции аккаунт не берут
    "notify_freed": True,
    "notify_stuck": True,
}


def normalize_settings(raw: Optional[dict]) -> dict[str, Any]:
    """Привести настройки к допустимым значениям (с экрана приходит что угодно)."""
    out = dict(DEFAULT_SETTINGS)
    raw = dict(raw or {})
    for k in ("paused", "auto_appeal", "notify_freed", "notify_stuck"):
        if k in raw and raw[k] is not None:
            v = raw[k]
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "yes", "on", "да")
            out[k] = bool(v)
    pace = str(raw.get("pace") or out["pace"]).strip().lower()
    out["pace"] = pace if pace in PACE_MULT else "normal"
    for k, (lo, hi) in (("max_rechecks", _MAX_RECHECKS_RANGE),
                        ("rest_hours", _REST_HOURS_RANGE)):
        if raw.get(k) is None:
            continue
        try:
            out[k] = max(lo, min(hi, int(raw[k])))
        except (TypeError, ValueError):
            pass
    return out


async def get_settings(pool, owner_id: int) -> dict[str, Any]:
    """Настройки владельца; при любом сбое — значения по умолчанию."""
    try:
        row = await pool.fetchrow(
            """SELECT paused, auto_appeal, pace, max_rechecks, rest_hours,
                      notify_freed, notify_stuck
                 FROM rehab_settings WHERE owner_id=$1""", owner_id)
    except Exception:
        return dict(DEFAULT_SETTINGS)
    return normalize_settings(dict(row) if row else None)


async def save_settings(pool, owner_id: int, patch: dict) -> dict[str, Any]:
    """Сохранить изменения поверх текущих настроек; вернуть итог."""
    cur = await get_settings(pool, owner_id)
    cur.update({k: v for k, v in (patch or {}).items() if k in DEFAULT_SETTINGS})
    s = normalize_settings(cur)
    await pool.execute(
        """INSERT INTO rehab_settings(owner_id, paused, auto_appeal, pace,
                   max_rechecks, rest_hours, notify_freed, notify_stuck, updated_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now())
           ON CONFLICT (owner_id) DO UPDATE SET
               paused=EXCLUDED.paused, auto_appeal=EXCLUDED.auto_appeal,
               pace=EXCLUDED.pace, max_rechecks=EXCLUDED.max_rechecks,
               rest_hours=EXCLUDED.rest_hours, notify_freed=EXCLUDED.notify_freed,
               notify_stuck=EXCLUDED.notify_stuck, updated_at=now()""",
        owner_id, s["paused"], s["auto_appeal"], s["pace"], s["max_rechecks"],
        s["rest_hours"], s["notify_freed"], s["notify_stuck"])
    return s


# ── Дата снятия из ответа @SpamBot ────────────────────────────────────────────
# Временный блок называет дату: «Your account will be automatically released
# on 13 Nov 2026, 15:42 UTC». Раньше её не читали: перепроверки шли по
# слепому бэк-оффу (≈13 суток на восемь попыток), и блок на месяц уходил
# «на разбор» с советом заменить аккаунт, хотя Telegram сам назвал день,
# когда всё кончится. Теперь перепроверка ставится сразу после этой даты,
# а пока она не наступила, аккаунт не считается безнадёжным.
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "січ": 1, "лют": 2, "бер": 3, "кві": 4, "тра": 5, "чер": 6, "лип": 7,
    "сер": 8, "вер": 9, "жов": 10, "лис": 11, "гру": 12,
}
_RX_DMY_WORD = re.compile(
    r"(\d{1,2})\s+([A-Za-zА-Яа-яЁёІіЇїЄє]{3,})\.?\s+(\d{4})"
    r"(?:\s*(?:г\.?|р\.?)?,?\s*(?:at\s+|в\s+|о\s+)?(\d{1,2}):(\d{2}))?")
_RX_MDY_WORD = re.compile(
    r"([A-Za-z]{3,})\.?\s+(\d{1,2}),?\s+(\d{4})"
    r"(?:,?\s*(?:at\s+)?(\d{1,2}):(\d{2}))?")
_RX_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?")
_RX_DOTTED = re.compile(
    r"(\d{1,2})[./](\d{1,2})[./](\d{4})(?:,?\s*(\d{1,2}):(\d{2}))?")


def parse_release_at(text: Optional[str], now=None):
    """Дата автоматического снятия из ответа @SpamBot или None.

    Чистая функция. Время без пояса считается UTC (так пишет @SpamBot).
    Дата в далёком прошлом или дальше года вперёд — не дата снятия, а шум.
    """
    from datetime import datetime, timedelta, timezone

    if not text:
        return None
    now = now or datetime.now(timezone.utc)

    def _mk(y, mo, d, hh, mm):
        try:
            return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0),
                            tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    cands = []
    for m in _RX_DMY_WORD.finditer(text):
        mo = _MONTHS.get(m.group(2).lower()[:3])
        if mo:
            cands.append(_mk(m.group(3), mo, m.group(1), m.group(4), m.group(5)))
    for m in _RX_MDY_WORD.finditer(text):
        mo = _MONTHS.get(m.group(1).lower()[:3])
        if mo:
            cands.append(_mk(m.group(3), mo, m.group(2), m.group(4), m.group(5)))
    for m in _RX_ISO.finditer(text):
        cands.append(_mk(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)))
    for m in _RX_DOTTED.finditer(text):
        cands.append(_mk(m.group(3), m.group(2), m.group(1), m.group(4), m.group(5)))
    for dt in cands:
        if dt is None:
            continue
        if now - timedelta(days=2) <= dt <= now + timedelta(days=400):
            return dt
    return None


def _future(dt, now=None) -> bool:
    from datetime import datetime, timezone

    if dt is None:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > now
    except (AttributeError, TypeError):
        return False


def _secs_until(dt, now=None) -> int:
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((dt - now).total_seconds()))


# ── История шагов ─────────────────────────────────────────────────────────────
# Что автоматика делала с аккаунтом, видно по журналу, а не по одной строке
# «заметка», которую затирал каждый следующий шаг. Подписи — здесь, на сервере.
EVENT_LABEL = {
    "enrolled": "🩹 Взят на восстановление",
    "relapse": "🔁 Повторный спам-блок — восстановление начато заново",
    "appeal": "📨 Запрос снятия у @SpamBot",
    "appeal_skipped": "⏭ Запрос снятия пропущен (выключен в настройках)",
    "warming": "🌱 Тихий прогрев",
    "recheck": "🔍 Перепроверка у @SpamBot",
    "release_date": "📅 Telegram назвал дату снятия",
    "freed": "✅ Ограничение снято",
    "rest": "😴 Отдых перед возвратом в операции",
    "stuck": "🛠 Передан вам на разбор",
    "frozen": "🧊 Заморожен Telegram",
    "gone": "🚫 Восстанавливать нечего",
    "manual_now": "⏩ Вы ускорили следующий шаг",
    "manual_off": "⏹ Вы выключили восстановление",
    "manual_restart": "🔁 Вы перезапустили восстановление",
    "manual_appeal": "🛡 Вы отправили запрос снятия",
}


async def _log_event(pool, acc_id: int, owner_id, step: str,
                     outcome: str = "", detail: str = "") -> None:
    """Записать шаг в историю. Журнал не должен ронять сам шаг."""
    try:
        await pool.execute(
            """INSERT INTO account_rehab_events(acc_id, owner_id, step, outcome, detail)
               VALUES ($1, $2, $3, $4, $5)""",
            int(acc_id), int(owner_id or 0), step, (outcome or "")[:60],
            (detail or "")[:400])
    except Exception:
        log.debug("rehab: шаг истории не записан acc=%s step=%s", acc_id, step)


async def history(pool, owner_id: int, acc_id: int, limit: int = 100) -> list[dict]:
    """История восстановления одного аккаунта, новые сверху."""
    rows = await pool.fetch(
        """SELECT at, step, outcome, detail FROM account_rehab_events
            WHERE acc_id=$1 AND owner_id=$2
            ORDER BY at DESC, id DESC LIMIT $3""",
        acc_id, owner_id, max(1, min(500, int(limit))))
    out = []
    for r in rows:
        d = dict(r)
        d["label"] = EVENT_LABEL.get(d["step"], d["step"])
        d["at"] = d["at"].isoformat() if d.get("at") else None
        out.append(d)
    return out


async def _rest_after_release(pool, acc_id: int, owner_id, hours: int) -> None:
    """Мягкий возврат: после снятия аккаунт отдыхает, операции его не берут.

    Сообщение об итоге и раньше советовало «не нагружайте сразу», но совет
    ничего не делал: аккаунт уходил в рассылки в ту же минуту, и блок
    возвращался. Отдых ставится через `cooldown_until` — его уважает единая
    дверь выбора аккаунтов (`resource_selector`). Уже стоящий более долгий
    отдых не укорачивается.
    """
    if not hours or hours <= 0:
        return
    try:
        await pool.execute(
            """UPDATE tg_accounts
                  SET cooldown_until = GREATEST(COALESCE(cooldown_until, now()),
                                                now() + make_interval(hours => $2))
                WHERE id=$1""",
            acc_id, int(hours))
        await _log_event(pool, acc_id, owner_id, "rest", "",
                         f"{int(hours)} ч без операций")
    except Exception:
        log.debug("rehab: отдых после снятия не поставлен acc=%s", acc_id)


# ── Синхронизация набора реабилитируемых с реальностью ─────────────────────────
async def _sync_enrollment(pool) -> None:
    """Завести стейт для новых спам-блоков и закрыть тех, кто уже сменил статус."""
    # 1) Новые под спам-блоком, которых ещё нет в реабилитации.
    await pool.execute(
        """WITH ins AS (
               INSERT INTO account_rehab_state(acc_id, owner_id, phase, next_action_at)
               SELECT a.id, a.owner_id, 'appeal', now()
               FROM tg_accounts a
               WHERE COALESCE(a.acc_status, 'active') = 'spamblock'
                 AND a.session_str IS NOT NULL AND length(a.session_str) > 10
               ON CONFLICT (acc_id) DO NOTHING
               RETURNING acc_id, owner_id)
           INSERT INTO account_rehab_events(acc_id, owner_id, step)
           SELECT acc_id, owner_id, 'enrolled' FROM ins"""
    )
    # 2) Кто больше не под спам-блоком — закрыть реабилитацию соответствующим итогом.
    #    active/warming/cooldown → freed (сняли где-то ещё); dead → gone.
    await pool.execute(
        """UPDATE account_rehab_state r
           SET phase = CASE
                   WHEN COALESCE(a.acc_status,'active') = ANY($1::text[]) THEN 'gone'
                   -- Заморозка — не «снят»: без этой ветки замороженный аккаунт
                   -- записывался в восстановленные, и итог реабилитации врал.
                   WHEN COALESCE(a.acc_status,'active') = 'frozen' THEN 'stuck'
                   ELSE 'freed' END,
               note = CASE WHEN COALESCE(a.acc_status,'active') = 'frozen'
                   THEN $2 ELSE r.note END,
               freed_at = CASE WHEN r.freed_at IS NULL
                   AND NOT (COALESCE(a.acc_status,'active') = ANY($1::text[]))
                   AND COALESCE(a.acc_status,'active') <> 'frozen'
                   THEN now() ELSE r.freed_at END,
               updated_at = now()
           FROM tg_accounts a
           WHERE a.id = r.acc_id
             AND r.phase IN ('appeal','warming','recheck','off')
             AND COALESCE(a.acc_status,'active') <> 'spamblock'""",
        list(_DEAD_STATUSES), _FROZEN_NOTE,
    )
    # 'off' в списке намеренно: пока аккаунт под блоком, выключенное вручную
    # восстановление остаётся выключенным (условие выше это гарантирует).
    # Но как только ограничение снято, отметка снимается — и если аккаунт
    # заблокируют СНОВА через месяц, восстановление подхватит его как обычно,
    # а не промолчит из-за давно забытого выключения.

    # 3) Повторный блок. Строка аккаунта, которого уже восстанавливали, стоит
    #    в 'freed' (или 'gone', если сессию потом перезалили), и шаг 1 её не
    #    трогает (ON CONFLICT DO NOTHING), а цикл берёт только активные фазы.
    #    Обещание из комментария выше не выполнялось: повторно заблокированный
    #    аккаунт не подхватывался НИКОГДА и молча висел под спам-блоком.
    #    Заморозку, которую владелец обжаловал до простого спам-блока, тоже
    #    берём заново — автоматика ей больше не мешает.
    await pool.execute(
        """WITH re AS (
               UPDATE account_rehab_state r
                  SET phase='appeal', attempts=0, appeal_count=0, warm_cycles=0,
                      kind=NULL, release_at=NULL, freed_at=NULL,
                      notified_phase=NULL, relapses = r.relapses + 1,
                      note='повторный спам-блок — восстановление начато заново',
                      first_seen=now(), next_action_at=now(), updated_at=now()
                 FROM tg_accounts a
                WHERE a.id = r.acc_id
                  AND COALESCE(a.acc_status,'active') = 'spamblock'
                  AND a.session_str IS NOT NULL AND length(a.session_str) > 10
                  AND (r.phase IN ('freed','gone')
                       OR (r.phase = 'stuck' AND r.note = $1))
               RETURNING r.acc_id, r.owner_id)
           INSERT INTO account_rehab_events(acc_id, owner_id, step)
           SELECT acc_id, owner_id, 'relapse' FROM re""",
        _FROZEN_NOTE,
    )


async def _due_rows(pool, limit: int) -> list[dict]:
    rows = await pool.fetch(
        """SELECT r.*, a.session_str, a.owner_id AS a_owner
           FROM account_rehab_state r
           JOIN tg_accounts a ON a.id = r.acc_id
           WHERE r.phase IN ('appeal','warming','recheck')
             AND r.next_action_at <= now()
             AND COALESCE(a.acc_status,'active') = 'spamblock'
             AND a.session_str IS NOT NULL AND length(a.session_str) > 10
             -- Пауза владельца: строки стоят на месте, шаги не делаются.
             AND NOT EXISTS (SELECT 1 FROM rehab_settings s
                              WHERE s.owner_id = r.owner_id AND s.paused)
           ORDER BY r.next_action_at ASC
           LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


async def _advance(
    pool, acc_id: int, *, phase: str, note: str,
    delay_sec: Optional[int] = None,
    kind: Optional[str] = None,
    inc_attempts: int = 0, inc_appeals: int = 0,
    inc_warm_cycles: int = 0, inc_warm_actions: int = 0,
    freed: bool = False,
    release_at=None,
) -> None:
    await pool.execute(
        """UPDATE account_rehab_state SET
               release_at = COALESCE($11::timestamptz, release_at),
               phase = $2,
               note = $3,
               kind = COALESCE($4, kind),
               attempts = attempts + $5,
               appeal_count = appeal_count + $6,
               warm_cycles = warm_cycles + $7,
               warm_actions = warm_actions + $8,
               last_action_at = now(),
               next_action_at = CASE WHEN $9::int IS NULL
                   THEN next_action_at ELSE now() + make_interval(secs => $9) END,
               freed_at = CASE WHEN $10 AND freed_at IS NULL THEN now() ELSE freed_at END,
               updated_at = now()
           WHERE acc_id = $1""",
        acc_id, phase, note[:400], kind,
        inc_attempts, inc_appeals, inc_warm_cycles, inc_warm_actions,
        delay_sec, freed, release_at,
    )


async def _hand_over_terminal(pool, acc_id: int, status: str, *,
                              inc_attempts: int = 0, inc_appeals: int = 0) -> None:
    """@SpamBot ответил заморозкой или баном — реабилитация здесь не лечит.

    Статус пишется правдой (иначе операции продолжат брать аккаунт под
    'spamblock'), а реабилитация отдаёт аккаунт: заморозку — человеку
    ('stuck' с причиной, апелляцию подаёт он сам), бан — в 'gone'.
    """
    from services import account_status

    frozen = status == "frozen"
    await account_status.set_status(
        pool, acc_id, status,
        reason=("Реабилитация: @SpamBot сообщил о заморозке аккаунта" if frozen
                else "Реабилитация: @SpamBot сообщил о блокировке аккаунта"),
        source="rehab")
    await _advance(pool, acc_id,
                   phase="stuck" if frozen else "gone",
                   note=_FROZEN_NOTE if frozen else "заблокирован Telegram — восстанавливать нечего",
                   inc_attempts=inc_attempts, inc_appeals=inc_appeals)
    log.info("rehab: acc=%d передан из реабилитации — %s", acc_id, status)


# ── Тихий пассивный прогрев ────────────────────────────────────────────────────
async def _quiet_warmup(pool, acc: dict) -> int:
    """Несколько пассивных действий (чтение/просмотр/presence) для одного аккаунта.

    Возвращает число успешных действий. Захватывает аккаунт через op_worker, чтобы
    не столкнуться с операцией/штатным прогревом (AUTH_KEY_DUPLICATED), и обязательно
    освобождает его. Только чтение — ничего не рассылает.
    """
    from services import account_manager, account_warmer
    from services import op_worker as _opw

    acc_id = int(acc["acc_id"])
    if not await _opw.try_claim_account(acc_id):
        return 0

    client = None
    done = 0
    try:
        # Полная запись аккаунта для транспорта/устройства.
        from database import db as _db
        row = await _db.get_account_for_telethon(pool, acc_id)
        if not row or not row.get("session_str"):
            return 0

        client = await asyncio.wait_for(
            account_manager.connect_client(
                row["session_str"], dict(row), action_type="warmup", low_risk=True),
            timeout=45,
        )
        # Цели: своя инфраструктура + публичные каналы (только чтение).
        try:
            res = await account_warmer._get_warmup_resources(pool, int(acc["owner_id"]))
            targets = [f"@{c['username']}" for c in res.get("channels", []) if c.get("username")]
        except Exception:
            targets = []
        targets += list(account_warmer._WARMUP_PUBLIC_CHANNELS)
        random.shuffle(targets)

        # Пассивный репертуар: чтение канала, просмотр профиля, presence, диалоги.
        actions = [
            account_warmer._perform_read_channel,
            account_warmer._perform_view_profile,
            account_warmer._perform_open_chat,
        ]
        n = random.randint(_WARM_ACTIONS_MIN, _WARM_ACTIONS_MAX)
        # presence/dialogs не требуют цели
        try:
            if await account_warmer._perform_update_presence(client):
                done += 1
        except Exception:
            pass
        for i in range(n):
            if i >= len(targets):
                break
            fn = random.choice(actions)
            try:
                if await asyncio.wait_for(fn(client, targets[i]), timeout=40):
                    done += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("rehab warmup action failed acc=%d", acc_id)
            await asyncio.sleep(random.uniform(3, 9))
        return done
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.debug("rehab: quiet warmup acc=%d failed: %s", acc_id, e)
        return done
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        try:
            await _opw.release_accounts([acc_id])
        except Exception:
            pass


# ── Обработка одного аккаунта по его фазе ──────────────────────────────────────
async def _process(pool, r: dict) -> None:
    from services import account_manager, account_status

    acc_id = int(r["acc_id"])
    owner_id = r.get("owner_id")
    phase = r["phase"]
    sess = r.get("session_str")
    acc_for_client = {"acc_id": acc_id, "owner_id": owner_id, "session_str": sess}
    cfg = normalize_settings(r.get("_cfg"))
    pace = cfg["pace"]
    release_at = r.get("release_at")

    async def _freed(note: str, **inc) -> None:
        await _advance(pool, acc_id, phase="freed", note=note, freed=True, **inc)
        await _log_event(pool, acc_id, owner_id, "freed", "ok", note)
        await _rest_after_release(pool, acc_id, owner_id, cfg["rest_hours"])

    if phase == "appeal":
        if not cfg["auto_appeal"]:
            # Владелец запретил автоматике писать @SpamBot — сразу к тихому
            # прогреву; снятие узнаем на перепроверке.
            await _advance(pool, acc_id, phase="warming",
                           note="запрос снятия выключен в настройках",
                           delay_sec=_warm_gap(pace))
            await _log_event(pool, acc_id, owner_id, "appeal_skipped")
            return
        try:
            res = await asyncio.wait_for(
                account_manager.appeal_spamblock(sess, {"id": acc_id, "owner_id": owner_id}),
                timeout=90)
        except Exception as e:
            res = {"ok": False, "status": "error", "reply": str(e)[:120]}
        st = res.get("status")
        if st in ("frozen", "banned"):
            await _hand_over_terminal(pool, acc_id, st, inc_appeals=0)
            await _log_event(pool, acc_id, owner_id, "frozen" if st == "frozen" else "gone", st)
            return
        if st == "free":
            await account_status.set_status(
                pool, acc_id, "active", reason="Спам-блок снят (аппеляция)", source="rehab")
            await _freed("снят по аппеляции", inc_appeals=1)
            log.info("rehab: acc=%d освобождён по аппеляции", acc_id)
            return
        rel = parse_release_at(res.get("full_reply") or res.get("reply"))
        # Аппеляция отправлена (или блок ещё держится) → к тихому прогреву.
        await _advance(pool, acc_id, phase="warming",
                       note=f"аппеляция: {st}", inc_appeals=1,
                       kind="temp" if rel else None, release_at=rel,
                       delay_sec=_warm_gap(pace))
        await _log_event(pool, acc_id, owner_id, "appeal", st or "")
        if rel:
            await _log_event(pool, acc_id, owner_id, "release_date", "",
                             rel.strftime("%d.%m.%Y %H:%M UTC"))
        return

    if phase == "warming":
        done = await _quiet_warmup(pool, acc_for_client)
        cycles = int(r["warm_cycles"]) + 1
        await _log_event(pool, acc_id, owner_id, "warming", "",
                         f"{done} пассивных действий")
        if _future(release_at):
            # Telegram назвал дату. До неё перепроверять незачем: держим
            # аккаунт живым редким тихим прогревом (раз в сутки), а
            # перепроверку ставим сразу после даты снятия.
            left = _secs_until(release_at)
            if left > 30 * 3600:
                await _advance(pool, acc_id, phase="warming",
                               note=f"ждём даты снятия; тихий прогрев ({done} действ.)",
                               inc_warm_cycles=1, inc_warm_actions=done,
                               delay_sec=_jitter(20 * 3600, frac=0.2))
            else:
                await _advance(pool, acc_id, phase="recheck",
                               note="перепроверка сразу после даты снятия",
                               inc_warm_cycles=1, inc_warm_actions=done,
                               delay_sec=left + random.randint(20 * 60, 90 * 60))
            return
        if cycles >= _WARM_CYCLES_BEFORE_RECHECK:
            await _advance(pool, acc_id, phase="recheck",
                           note=f"прогрет ({done} действ.), к перепроверке",
                           inc_warm_cycles=1, inc_warm_actions=done,
                           delay_sec=_warm_gap(pace))
        else:
            await _advance(pool, acc_id, phase="warming",
                           note=f"тихий прогрев ({done} действ.)",
                           inc_warm_cycles=1, inc_warm_actions=done,
                           delay_sec=_warm_gap(pace))
        return

    if phase == "recheck":
        try:
            chk = await asyncio.wait_for(
                account_manager.check_account_status_full(
                    sess, {"id": acc_id, "owner_id": owner_id}, check_spambot=True),
                timeout=60)
        except Exception as e:
            chk = {"status": "unknown", "reason": str(e)[:120]}
        status = chk.get("status")
        attempts = int(r["attempts"]) + 1

        if status == "active":
            await account_status.set_status(
                pool, acc_id, "active",
                reason="Спам-блок снят (перепроверка после прогрева)", source="rehab")
            await _freed("снят по перепроверке", inc_attempts=1)
            log.info("rehab: acc=%d освобождён после прогрева (попытка %d)", acc_id, attempts)
            return

        if status == "frozen":
            await _hand_over_terminal(pool, acc_id, "frozen", inc_attempts=1)
            await _log_event(pool, acc_id, owner_id, "frozen", "frozen")
            return

        if status in _DEAD_STATUSES:
            await account_status.set_status(
                pool, acc_id, status, reason="Реабилитация: обнаружен терминальный статус",
                source="rehab")
            await _advance(pool, acc_id, phase="gone",
                           note=f"терминальный статус: {status}", inc_attempts=1)
            await _log_event(pool, acc_id, owner_id, "gone", status)
            return

        # Всё ещё спам-блок. Уточняем kind и дату снятия.
        kind = chk.get("spamblock_kind") or r.get("kind")
        rel = parse_release_at(chk.get("spambot_reply") or chk.get("reason"))
        if rel is not None:
            kind = "temp"
            if not release_at or rel != release_at:
                await _log_event(pool, acc_id, owner_id, "release_date", "",
                                 rel.strftime("%d.%m.%Y %H:%M UTC"))
        rel_eff = rel or release_at
        await _log_event(pool, acc_id, owner_id, "recheck",
                         status if status != "spamblock" else (kind or "spamblock"))
        waiting_release = _future(rel_eff)
        max_rechecks = cfg["max_rechecks"]
        exhausted = (
            attempts >= _HARD_MAX_RECHECKS
            or (not waiting_release and (
                attempts >= max_rechecks
                or (kind == "perm" and int(r["appeal_count"]) >= _MAX_APPEALS
                    and attempts >= max(3, max_rechecks // 2))))
        )
        if exhausted:
            await _advance(pool, acc_id, phase="stuck", kind=kind,
                           note=f"не снят за {attempts} перепроверок ({kind or '?'}) — нужен ручной разбор",
                           inc_attempts=1, release_at=rel)
            await _log_event(pool, acc_id, owner_id, "stuck", kind or "",
                             f"{attempts} перепроверок")
            log.info("rehab: acc=%d застрял (%s), уходит на ручной разбор", acc_id, kind)
            return

        # Ещё одна итерация прогрева с растущим бэк-оффом.
        delay = _backoff_sec(attempts, pace)
        if waiting_release:
            delay = min(delay, _jitter(_WARM_GAP_SEC))
        await _advance(pool, acc_id, phase="warming", kind=kind,
                       note=f"ещё под блоком ({kind or '?'}), попытка {attempts}",
                       inc_attempts=1, release_at=rel, delay_sec=delay)
        return


async def run_rehab_cycle(pool) -> int:
    """Один проход реабилитации. Возвращает число обработанных аккаунтов.

    Выделен отдельно от цикла ради тестируемости и ручного запуска.
    """
    if _disabled():
        return 0
    try:
        await _sync_enrollment(pool)
    except Exception:
        log.warning("rehab: sync enrollment failed", exc_info=True)
        return 0

    try:
        rows = await _due_rows(pool, _MAX_PER_TICK)
    except Exception:
        log.warning("rehab: due rows fetch failed", exc_info=True)
        return 0

    handled = 0
    cfg_cache: dict = {}
    for r in rows:
        try:
            oid = r.get("owner_id")
            if oid is not None and oid not in cfg_cache:
                cfg_cache[oid] = await get_settings(pool, oid)
            r["_cfg"] = cfg_cache.get(oid)
            await _process(pool, r)
            handled += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("rehab: process acc=%s failed", r.get("acc_id"), exc_info=True)
        # Разнос между аккаунтами в пределах тика — не бить залпом.
        await asyncio.sleep(random.uniform(2, 6))
    return handled


async def run_rehab_loop(pool, bot=None) -> None:
    """Фоновый цикл авто-реабилитации. Запускать один раз на процесс."""
    if _disabled():
        log.info("rehab: отключён через INFRAGRAM_DISABLE_REHAB")
        return
    log.info("rehab: цикл авто-реабилитации запущен (tick=%ds)", _TICK_SEC)
    # Небольшая стартовая задержка — дать флоту подняться.
    await asyncio.sleep(random.uniform(20, 60))
    while True:
        try:
            n = await run_rehab_cycle(pool)
            if n:
                log.info("rehab: обработано аккаунтов за тик: %d", n)
            # Итог восстановления — новость, которую человек обязан услышать:
            # вернувшийся аккаунт иначе будет списан, а застрявший так и
            # останется ждать разбора, которого никто не назначал.
            if bot is not None:
                try:
                    await notify_terminal(pool, bot)
                except Exception:
                    log.debug("rehab: уведомления об итогах не отправлены")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("rehab: тик упал", exc_info=True)
        await asyncio.sleep(_jitter(_TICK_SEC, frac=0.15))


# ── Человеческое описание фазы ────────────────────────────────────────────────
# Разрыв: реабилитация работала как чёрный ящик. Аккаунт ловил спам-блок,
# исчезал из операций — и всё. Фоновый цикл сутками пытался его вернуть,
# аккаунты уходили в `stuck` («нужен ручной разбор») и вставали в очередь,
# которую НЕКОМУ было увидеть: ни экрана, ни уведомления, а `rehab_overview`
# не вызывался ни из одного места продукта. Пользователь считал аккаунт
# потерянным, хотя тот восстанавливался, — или ждал вечно того, что уже
# требовало его рук.

PHASE_LABEL = {
    "appeal":  "📨 Запрос на снятие",
    "warming": "🌱 Тихий прогрев",
    "recheck": "🔍 Перепроверка",
    "freed":   "✅ Восстановлен",
    "stuck":   "🛠 Нужен ваш разбор",
    "gone":    "🚫 Восстанавливать нечего",
    "off":     "⏹ Восстановление выключено",
}
PHASE_HINT = {
    "appeal":  "Аккаунт просит @SpamBot снять ограничение — это штатная"
               " аппеляция от лица самого аккаунта.",
    "warming": "Идут только пассивные действия — чтение каналов и профилей."
               " Ничего никому не рассылается: при спам-блоке это единственное"
               " безопасное поведение, и именно оно возвращает доверие.",
    "recheck": "Проверяем у @SpamBot, снято ли ограничение. Интервал между"
               " проверками растёт намеренно: частые проверки выглядят"
               " подозрительно и делу не помогают.",
    "freed":   "Ограничение снято, аккаунт вернулся в строй и снова участвует"
               " в операциях.",
    "stuck":   "Автоматика исчерпала попытки — ограничение не снимается."
               " Обычно это постоянный блок: аккаунт стоит заменить."
               " Можно запустить восстановление заново, если хотите ещё попытку.",
    "gone":    "Аккаунт забанен, удалён или его сессия недействительна —"
               " восстанавливать нечего.",
    "off":     "Вы остановили автоматическое восстановление для этого аккаунта.",
}
ACTIVE_PHASES = ("appeal", "warming", "recheck")
# Фазы, в которых дальше без человека не сдвинется.
ATTENTION_PHASES = ("stuck",)


def describe(row: dict, now=None, settings: Optional[dict] = None) -> dict[str, Any]:
    """Что происходит с аккаунтом в реабилитации и когда следующий шаг.

    Чистая функция: строка состояния → то, что видит человек.
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    phase = (row.get("phase") or "appeal").lower()
    label = PHASE_LABEL.get(phase, phase)
    hint = PHASE_HINT.get(phase, "")
    # Заморозка лежит в той же фазе «нужен ваш разбор», но совет у неё другой:
    # общий текст «стоит заменить» для неё неверен — аккаунт возвращается
    # апелляцией, и её подаёт только сам владелец.
    frozen = phase == "stuck" and str(row.get("acc_status") or "").lower() == "frozen"
    if frozen:
        label = "🧊 Заморожен — нужна ваша апелляция"
        hint = ("Telegram заморозил аккаунт: режим «только чтение». Автоматика"
                " это не снимает — апелляцию подаёте вы. Откройте @SpamBot с этого аккаунта, нажмите"
                " «This is a mistake» и ответьте на вопросы — иначе Telegram"
                " удалит аккаунт после даты, указанной в сообщении.")
    nxt = row.get("next_action_at")
    wait_s = None
    if phase in ACTIVE_PHASES and nxt is not None:
        try:
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            wait_s = max(0, int((nxt - now).total_seconds()))
        except (AttributeError, TypeError, ValueError):
            wait_s = None
    def _aware(v):
        if v is None or not hasattr(v, "tzinfo"):
            return None
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)

    # Дата, которую назвал сам Telegram, — главное, что хочет знать человек
    # о временном блоке: «когда он вернётся».
    rel = _aware(row.get("release_at"))
    release_in = None
    if phase in ACTIVE_PHASES and rel is not None and rel > now:
        release_in = int((rel - now).total_seconds())
    rest_until = _aware(row.get("cooldown_until"))
    rest_in = None
    if phase == "freed" and rest_until is not None and rest_until > now:
        rest_in = int((rest_until - now).total_seconds())
        hint = ("Ограничение снято. Аккаунт отдыхает и пока не участвует в"
                " операциях — так блок не вернётся сразу. Потом он вернётся в"
                " строй сам.")
    max_att = int((settings or {}).get("max_rechecks") or _MAX_RECHECKS)
    return {
        "phase": phase,
        "label": label,
        "hint": hint,
        "note": row.get("note") or "",
        "attempts": int(row.get("attempts") or 0),
        "max_attempts": max_att,
        "release_at": rel.isoformat() if rel is not None else None,
        "release_in_seconds": release_in,
        "rest_in_seconds": rest_in,
        "relapses": int(row.get("relapses") or 0),
        "frozen": frozen,
        "next_in_seconds": wait_s,
        "active": phase in ACTIVE_PHASES,
        "attention": phase in ATTENTION_PHASES,
        # Перезапуск имеет смысл только там, где автоматика уже сдалась.
        # Заморозку перезапуск не снимет: её обжалует только человек.
        "restartable": phase in ("stuck", "off") and not frozen,
    }


def build_terminal_alert(name: str, phase: str, attempts: int,
                         frozen: bool = False) -> str:
    """Сообщение об итоге восстановления. Оба итога важны и оба молчали.

    `freed` — аккаунт вернулся в строй, и человек должен об этом знать, иначе
    он уже списал его и купил замену. `stuck` — автоматика сдалась и передаёт
    аккаунт человеку; без сообщения эта передача была в пустоту.
    """
    if phase == "freed":
        return (
            "✅ <b>Аккаунт восстановлен</b>\n\n"
            f"<b>{name}</b> — спам-блок снят, аккаунт снова участвует в операциях.\n\n"
            "Не нагружайте его сразу: дайте пару дней спокойного режима,"
            " иначе ограничение вернётся."
        )
    if frozen:
        return (
            "🧊 <b>Аккаунт заморожен Telegram</b>\n\n"
            f"<b>{name}</b> — режим «только чтение», из работы выведен.\n\n"
            "Автоматика это не снимает: апелляцию подаёте вы. Откройте"
            " @SpamBot с этого аккаунта, нажмите «This is a mistake» и ответьте"
            " на вопросы. Если не успеть до даты из сообщения, Telegram удалит"
            " аккаунт."
        )
    return (
        "🛠 <b>Аккаунт не удалось восстановить</b>\n\n"
        f"<b>{name}</b> — ограничение держится после {int(attempts or 0)} перепроверок.\n\n"
        "Похоже на постоянный блок: автоматика больше ничего сделать не может."
        " Замените аккаунт или запустите восстановление заново вручную,"
        " если хотите дать ему ещё шанс."
    )


class _SkipNotify(Exception):
    """Уведомление выключено владельцем."""


async def notify_terminal(pool, bot) -> int:
    """Сообщить владельцам об аккаунтах, чьё восстановление завершилось.

    Один раз на итог (метка `notified_phase`) — тот же приём, что у сторожа
    прокси и у прогрева: повторяющееся уведомление перестают читать.
    """
    try:
        rows = await pool.fetch(
            """SELECT r.acc_id, r.owner_id, r.phase, r.attempts,
                      a.phone, a.first_name, a.acc_status
                 FROM account_rehab_state r
                 JOIN tg_accounts a ON a.id = r.acc_id
                WHERE r.phase IN ('freed','stuck')
                  AND COALESCE(r.notified_phase,'') <> r.phase
                LIMIT 50""")
    except Exception as e:
        log.debug("rehab: выборка завершённых не удалась: %s", e)
        return 0

    sent = 0
    cfg_cache: dict = {}
    for r in rows:
        name = r["first_name"] or r["phone"] or f"аккаунт #{r['acc_id']}"
        oid = r["owner_id"]
        if oid not in cfg_cache:
            cfg_cache[oid] = await get_settings(pool, oid)
        cfg = cfg_cache[oid]
        # Выключенное в настройках уведомление не шлётся, но итог помечается
        # сообщённым — иначе при включении прилетела бы пачка старых новостей.
        want = cfg["notify_freed"] if r["phase"] == "freed" else cfg["notify_stuck"]
        try:
            if not want:
                raise _SkipNotify()
            from database import db as _db

            await _db.notify_if_enabled(
                pool, bot, r["owner_id"], "restriction",
                build_terminal_alert(
                    name, r["phase"], r["attempts"],
                    frozen=str(r.get("acc_status") or "").lower() == "frozen"),
                dedup_key=f"rehab:{r['acc_id']}:{r['phase']}")
            sent += 1
        except _SkipNotify:
            pass
        except Exception:
            log.debug("rehab: уведомление не отправлено acc=%s", r["acc_id"])
        try:
            await pool.execute(
                "UPDATE account_rehab_state SET notified_phase=$2 WHERE acc_id=$1",
                r["acc_id"], r["phase"])
        except Exception:
            log.debug("rehab: метка уведомления не записана acc=%s", r["acc_id"])
    return sent


_MANUAL_APPEAL_TIMEOUT_S = 60


async def run_manual_appeal(pool, owner_id: int, acc_id: int, acc: dict) -> dict:
    """Ручная аппеляция спам-блока — одна дверь для мини-аппа и бота.

    Диалог с @SpamBot под тайм-аутом, учёт в реабилитации
    (`record_manual_appeal`) и след в журнале действий человека. Возвращает
    ответ `appeal_spamblock`; при тайм-ауте или сбое — status 'timeout' /
    'error' без сырого текста исключения (он уходит в лог).
    """
    from services import account_manager
    from database.db import record_manual_action

    try:
        res = await asyncio.wait_for(
            account_manager.appeal_spamblock(acc.get("session_str"), dict(acc)),
            timeout=_MANUAL_APPEAL_TIMEOUT_S)
    except asyncio.TimeoutError:
        res = {"ok": False, "status": "timeout", "reply": "", "steps": 0}
    except Exception:
        log.exception("rehab: ручная аппеляция acc=%s", acc_id)
        res = {"ok": False, "status": "error", "reply": "", "steps": 0}
    st = res.get("status") or "error"
    await record_manual_appeal(
        pool, owner_id, acc_id, st,
        release_at=parse_release_at(res.get("full_reply") or res.get("reply")))
    await record_manual_action(
        pool, owner_id, "spamblock_appeal", target=f"account:{acc_id}",
        result="success" if st in ("free", "appeal_sent") else st)
    return res


async def record_manual_appeal(pool, owner_id: int, acc_id: int, status: str,
                               release_at=None) -> None:
    """Учесть аппеляцию, которую человек отправил сам (кнопка в карточке).

    Раньше ручная аппеляция шла к @SpamBot мимо реабилитации: счётчик
    `appeal_count` не рос (лимит `_MAX_APPEALS` не работал), фаза оставалась
    `appeal`, и через минуты автоматика писала @SpamBot снова — две
    одинаковые аппеляции подряд, ровно тот всплеск, который реабилитация
    старается не создавать. А ответ «блок снят» записывался статусом 'ok',
    которого нет в словаре статусов: аккаунт не возвращался в строй.

    Теперь:
      • `free` — аккаунт под спам-блоком возвращается в 'active' (с причиной
        и источником в журнале статусов), реабилитация закрывается `freed`;
      • `appeal_sent` / `still_blocked` — аккаунт помечается спам-блоком, если
        ещё не помечен, аппеляция засчитывается, фаза `appeal` сменяется тихим
        прогревом, а следующий шаг автоматики не раньше обычного разноса.
    Fail-soft: учёт не должен ронять ответ человеку.
    """
    from services import account_status

    try:
        cur = await pool.fetchval(
            "SELECT acc_status FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            acc_id, owner_id)
        cur = (cur or "active").lower()
        if status == "free":
            if cur in account_status.RESTRICTED_STATUSES:
                await account_status.set_status(
                    pool, acc_id, "active",
                    reason="Спам-блок снят (ручная аппеляция)", source="manual_appeal")
            await pool.execute(
                """UPDATE account_rehab_state
                      SET phase='freed', note='снят по ручной аппеляции',
                          appeal_count = appeal_count + 1,
                          last_action_at = now(),
                          freed_at = COALESCE(freed_at, now()),
                          updated_at = now()
                    WHERE acc_id=$1 AND owner_id=$2
                      AND phase IN ('appeal','warming','recheck','off','stuck')""",
                acc_id, owner_id)
            await _log_event(pool, acc_id, owner_id, "manual_appeal", "free")
            cfg = await get_settings(pool, owner_id)
            await _rest_after_release(pool, acc_id, owner_id, cfg["rest_hours"])
            return
        if status in ("frozen", "banned"):
            # @SpamBot ответил заморозкой/баном — ни одной кнопки не нажато
            # (см. appeal_spamblock). Статус пишется правдой, иначе операции
            # продолжат брать аккаунт; реабилитация его отдаёт.
            frozen = status == "frozen"
            if cur != status:
                await account_status.set_status(
                    pool, acc_id, status,
                    reason=("@SpamBot: аккаунт заморожен (ручная проверка)" if frozen
                            else "@SpamBot: аккаунт заблокирован (ручная проверка)"),
                    source="manual_appeal")
            await pool.execute(
                """UPDATE account_rehab_state
                      SET phase=$3, note=$4, last_action_at = now(), updated_at = now()
                    WHERE acc_id=$1 AND owner_id=$2
                      AND phase IN ('appeal','warming','recheck','off','stuck')""",
                acc_id, owner_id, "stuck" if frozen else "gone",
                _FROZEN_NOTE if frozen else "заблокирован Telegram — восстанавливать нечего")
            await _log_event(pool, acc_id, owner_id, "manual_appeal", status)
            return
        if status not in ("appeal_sent", "still_blocked"):
            return
        if cur != "spamblock" and cur not in account_status.LOST_STATUSES:
            # @SpamBot подтвердил ограничение — статус должен говорить правду,
            # иначе операции продолжат брать флагнутый аккаунт.
            await account_status.set_status(
                pool, acc_id, "spamblock",
                reason="Спам-блок подтверждён @SpamBot (ручная аппеляция)",
                source="manual_appeal")
        note = ("ручная аппеляция отправлена" if status == "appeal_sent"
                else "ручная проверка: блок держится")
        await pool.execute(
            """INSERT INTO account_rehab_state(
                   acc_id, owner_id, phase, appeal_count, note,
                   last_action_at, next_action_at, release_at)
               SELECT a.id, a.owner_id, 'warming', 1, $3, now(),
                      now() + make_interval(secs => $4), $5::timestamptz
                 FROM tg_accounts a
                WHERE a.id=$1 AND a.owner_id=$2
               ON CONFLICT (acc_id) DO UPDATE
                  SET appeal_count = account_rehab_state.appeal_count + 1,
                      release_at = COALESCE(EXCLUDED.release_at,
                                            account_rehab_state.release_at),
                      phase = CASE WHEN account_rehab_state.phase = 'appeal'
                                   THEN 'warming' ELSE account_rehab_state.phase END,
                      note = EXCLUDED.note,
                      last_action_at = now(),
                      next_action_at = GREATEST(account_rehab_state.next_action_at,
                                                EXCLUDED.next_action_at),
                      updated_at = now()
                WHERE account_rehab_state.owner_id = EXCLUDED.owner_id""",
            acc_id, owner_id, note, _jitter(_WARM_GAP_SEC), release_at)
        await _log_event(pool, acc_id, owner_id, "manual_appeal", status)
        if release_at is not None:
            await _log_event(pool, acc_id, owner_id, "release_date", "",
                             release_at.strftime("%d.%m.%Y %H:%M UTC"))
    except Exception as e:
        log.warning("rehab: ручная аппеляция acc=%s не учтена: %s", acc_id, e)


async def force_now(pool, owner_id: int, acc_id: int) -> bool:
    """Сделать следующий шаг восстановления немедленно.

    Бэк-офф перепроверок доходит до 72 часов — разумно для автоматики, но
    невыносимо, когда человек знает, что блок уже снят, и хочет вернуть
    аккаунт в строй сейчас. Раньше ждать приходилось молча и до конца.
    """
    try:
        row = await pool.fetchrow(
            """UPDATE account_rehab_state
                  SET next_action_at=now(), updated_at=now()
                WHERE acc_id=$1 AND owner_id=$2
                  AND phase IN ('appeal','warming','recheck')
             RETURNING acc_id""",
            acc_id, owner_id)
        if row is not None:
            await _log_event(pool, acc_id, owner_id, "manual_now")
        return row is not None
    except Exception as e:
        log.warning("rehab: ускорить шаг acc=%s не удалось: %s", acc_id, e)
        return False


async def set_enabled(pool, owner_id: int, acc_id: int, enabled: bool) -> bool:
    """Включить/выключить авто-восстановление для одного аккаунта.

    Выключение нужно, когда человек решил заменить аккаунт и не хочет, чтобы
    флот тратил на него действия. Включение — второй шанс для того, что
    автоматика уже отдала на ручной разбор.
    """
    phase = "appeal" if enabled else "off"
    note = ("перезапущено вручную" if enabled
            else "восстановление выключено вручную")
    try:
        row = await pool.fetchrow(
            """UPDATE account_rehab_state
                  SET phase=$3, note=$4, next_action_at=now(),
                      notified_phase=NULL, updated_at=now(),
                      attempts = CASE WHEN $3='appeal' THEN 0 ELSE attempts END,
                      appeal_count = CASE WHEN $3='appeal' THEN 0 ELSE appeal_count END,
                      warm_cycles = CASE WHEN $3='appeal' THEN 0 ELSE warm_cycles END
                WHERE acc_id=$1 AND owner_id=$2
                  -- Перезапускать имеет смысл только то, что автоматика уже
                  -- отдала человеку. Поднимать 'gone' (забанен/удалён) нельзя:
                  -- цикл его всё равно не возьмёт, а экран показывал бы
                  -- «идёт восстановление» там, где восстанавливать нечего.
                  AND ($3 <> 'appeal' OR phase IN ('stuck','off'))
             RETURNING acc_id""",
            acc_id, owner_id, phase, note)
        if row is not None:
            await _log_event(pool, acc_id, owner_id,
                             "manual_restart" if enabled else "manual_off")
        return row is not None
    except Exception as e:
        log.warning("rehab: смена режима acc=%s не удалась: %s", acc_id, e)
        return False


# Срезы списка на экране. Ключ приходит с экрана, SQL — только отсюда.
VIEWS = {
    "all": "TRUE",
    "active": "r.phase IN ('appeal','warming','recheck')",
    "attention": "r.phase = 'stuck'",
    "freed": "r.phase = 'freed'",
    "closed": "r.phase IN ('gone','off')",
}


async def list_for_owner(pool, owner_id: int, limit: int = 100,
                         offset: int = 0, view: str = "all",
                         settings: Optional[dict] = None) -> list[dict]:
    """Аккаунты владельца в реабилитации — с человеческим описанием фазы.

    Страница, а не весь список: при волне блокировок на флоте строк тут больше
    сотни, а кнопки ⏩/⏹/🔁 живут в строке — аккаунт за потолком нельзя было ни
    поторопить, ни остановить. Порядок кладёт наверх тех, кому нужен разбор,
    поэтому за потолком оставалось самое спокойное, но и его не было видно.
    `view` — срез из `VIEWS` (неизвестный — весь список).
    """
    cond = VIEWS.get(view or "all", "TRUE")
    try:
        rows = await pool.fetch(
            f"""SELECT r.acc_id, r.phase, r.kind, r.attempts, r.appeal_count,
                      r.warm_cycles, r.warm_actions, r.note, r.first_seen,
                      r.last_action_at, r.next_action_at, r.freed_at,
                      r.release_at, r.relapses,
                      a.phone, a.first_name, a.username, a.acc_status,
                      a.cooldown_until
                 FROM account_rehab_state r
                 JOIN tg_accounts a ON a.id = r.acc_id
                WHERE r.owner_id=$1 AND {cond}
                -- `updated_at` у двух строк совпадает запросто (один проход
                -- автоматики правит их вместе), а со сдвигом страницы без
                -- уникального хвоста теряют и повторяют аккаунты.
                ORDER BY (r.phase = 'stuck') DESC,
                         (r.phase IN ('appeal','warming','recheck')) DESC,
                         r.updated_at DESC, r.acc_id
                LIMIT $2 OFFSET $3""",
            owner_id, max(1, min(500, int(limit))), max(0, int(offset or 0)))
    except Exception as e:
        log.debug("rehab: список для владельца %s не получен: %s", owner_id, e)
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["state"] = describe(d, settings=settings)
        for k in ("first_seen", "last_action_at", "next_action_at", "freed_at",
                  "release_at", "cooldown_until"):
            d[k] = d[k].isoformat() if d.get(k) else None
        out.append(d)
    return out


async def restart_all_stuck(pool, owner_id: int) -> int:
    """Дать ещё одну попытку всем, кого автоматика отдала на разбор.

    Заморозку не трогает: её снимает только апелляция владельца. Шаги не
    делаются залпом — цикл всё равно берёт не больше трёх аккаунтов за тик,
    а старт каждого разнесён случайной задержкой.
    """
    rows = await pool.fetch(
        """UPDATE account_rehab_state r
              SET phase='appeal', note='перезапущено вручную (все застрявшие)',
                  attempts=0, appeal_count=0, warm_cycles=0, notified_phase=NULL,
                  next_action_at = now() + make_interval(secs => (random()*3600)::int),
                  updated_at=now()
             FROM tg_accounts a
            WHERE a.id = r.acc_id AND r.owner_id=$1 AND r.phase='stuck'
              AND COALESCE(a.acc_status,'active') <> 'frozen'
              AND COALESCE(r.note,'') <> $2
          RETURNING r.acc_id""",
        owner_id, _FROZEN_NOTE)
    for r in rows:
        await _log_event(pool, r["acc_id"], owner_id, "manual_restart")
    return len(rows)


# ── Сводка для UI/диагностики ──────────────────────────────────────────────────
async def rehab_overview(pool, owner_id: int) -> dict[str, Any]:
    """Короткая сводка по реабилитации для владельца (для пульса/здоровья)."""
    rows = await pool.fetch(
        """SELECT phase, count(*) AS n FROM account_rehab_state
           WHERE owner_id = $1 GROUP BY phase""",
        owner_id,
    )
    by_phase = {r["phase"]: int(r["n"]) for r in rows}
    active = sum(by_phase.get(p, 0) for p in ("appeal", "warming", "recheck"))
    freed = by_phase.get("freed", 0)
    stuck = by_phase.get("stuck", 0)
    gone = by_phase.get("gone", 0)
    finished = freed + stuck + gone
    out = {
        "in_progress": active,
        "freed": freed,
        "stuck": stuck,
        "gone": gone,
        "off": by_phase.get("off", 0),
        "by_phase": by_phase,
        # Доля возвращённых среди завершённых — честный ответ на «работает ли».
        "success_pct": round(100 * freed / finished) if finished else None,
        "avg_hours_to_free": None,
        "frozen": 0,
        "relapses": 0,
        "waiting_release": 0,
    }
    try:
        st = await pool.fetchrow(
            """SELECT
                 avg(EXTRACT(EPOCH FROM (r.freed_at - r.first_seen)) / 3600.0)
                   FILTER (WHERE r.phase='freed' AND r.freed_at IS NOT NULL) AS avg_h,
                 count(*) FILTER (WHERE r.phase='stuck'
                                  AND a.acc_status='frozen') AS frozen,
                 COALESCE(sum(r.relapses), 0) AS relapses,
                 count(*) FILTER (WHERE r.phase IN ('appeal','warming','recheck')
                                  AND r.release_at > now()) AS waiting_release
               FROM account_rehab_state r
               JOIN tg_accounts a ON a.id = r.acc_id
              WHERE r.owner_id=$1""", owner_id)
        if st:
            out["avg_hours_to_free"] = (round(float(st["avg_h"]), 1)
                                        if st["avg_h"] is not None else None)
            out["frozen"] = int(st["frozen"] or 0)
            out["relapses"] = int(st["relapses"] or 0)
            out["waiting_release"] = int(st["waiting_release"] or 0)
    except Exception:
        log.debug("rehab: расширенная сводка не получена owner=%s", owner_id)
    return out
