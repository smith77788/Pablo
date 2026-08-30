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

_DEAD_STATUSES = ("banned", "deactivated", "session_expired")


def _disabled() -> bool:
    return os.getenv("INFRAGRAM_DISABLE_REHAB", "").strip().lower() in ("1", "true", "yes")


def _jitter(base: int, frac: float = 0.35) -> int:
    """Разброс интервала ±frac — чтобы действия флота не выстраивались в ритм."""
    d = int(base * frac)
    return max(60, base + random.randint(-d, d))


def _backoff_sec(attempts: int) -> int:
    idx = min(attempts, len(_RECHECK_BACKOFF_H) - 1)
    return _jitter(_RECHECK_BACKOFF_H[idx] * 3600, frac=0.25)


# ── Синхронизация набора реабилитируемых с реальностью ─────────────────────────
async def _sync_enrollment(pool) -> None:
    """Завести стейт для новых спам-блоков и закрыть тех, кто уже сменил статус."""
    # 1) Новые под спам-блоком, которых ещё нет в реабилитации.
    await pool.execute(
        """INSERT INTO account_rehab_state(acc_id, owner_id, phase, next_action_at)
           SELECT a.id, a.owner_id, 'appeal', now()
           FROM tg_accounts a
           WHERE COALESCE(a.acc_status, 'active') = 'spamblock'
             AND a.session_str IS NOT NULL AND length(a.session_str) > 10
           ON CONFLICT (acc_id) DO NOTHING"""
    )
    # 2) Кто больше не под спам-блоком — закрыть реабилитацию соответствующим итогом.
    #    active/warming/cooldown → freed (сняли где-то ещё); dead → gone.
    await pool.execute(
        """UPDATE account_rehab_state r
           SET phase = CASE
                   WHEN COALESCE(a.acc_status,'active') = ANY($1::text[]) THEN 'gone'
                   ELSE 'freed' END,
               freed_at = CASE WHEN r.freed_at IS NULL
                   AND COALESCE(a.acc_status,'active') <> ANY($1::text[])
                   THEN now() ELSE r.freed_at END,
               updated_at = now()
           FROM tg_accounts a
           WHERE a.id = r.acc_id
             AND r.phase IN ('appeal','warming','recheck')
             AND COALESCE(a.acc_status,'active') <> 'spamblock'""",
        list(_DEAD_STATUSES),
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
) -> None:
    await pool.execute(
        """UPDATE account_rehab_state SET
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
        delay_sec, freed,
    )


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
    phase = r["phase"]
    sess = r.get("session_str")
    acc_for_client = {"acc_id": acc_id, "owner_id": r["owner_id"], "session_str": sess}

    if phase == "appeal":
        try:
            res = await asyncio.wait_for(
                account_manager.appeal_spamblock(sess, {"id": acc_id, "owner_id": r["owner_id"]}),
                timeout=90)
        except Exception as e:
            res = {"ok": False, "status": "error", "reply": str(e)[:120]}
        st = res.get("status")
        if st == "free":
            await account_status.set_status(
                pool, acc_id, "active", reason="Спам-блок снят (аппеляция)", source="rehab")
            await _advance(pool, acc_id, phase="freed",
                           note="снят по аппеляции", inc_appeals=1, freed=True)
            log.info("rehab: acc=%d освобождён по аппеляции", acc_id)
            return
        # Аппеляция отправлена (или блок ещё держится) → к тихому прогреву.
        await _advance(pool, acc_id, phase="warming",
                       note=f"аппеляция: {st}", inc_appeals=1,
                       delay_sec=_jitter(_WARM_GAP_SEC))
        return

    if phase == "warming":
        done = await _quiet_warmup(pool, acc_for_client)
        cycles = int(r["warm_cycles"]) + 1
        if cycles >= _WARM_CYCLES_BEFORE_RECHECK:
            await _advance(pool, acc_id, phase="recheck",
                           note=f"прогрет ({done} действ.), к перепроверке",
                           inc_warm_cycles=1, inc_warm_actions=done,
                           delay_sec=_jitter(_WARM_GAP_SEC))
        else:
            await _advance(pool, acc_id, phase="warming",
                           note=f"тихий прогрев ({done} действ.)",
                           inc_warm_cycles=1, inc_warm_actions=done,
                           delay_sec=_jitter(_WARM_GAP_SEC))
        return

    if phase == "recheck":
        try:
            chk = await asyncio.wait_for(
                account_manager.check_account_status_full(
                    sess, {"id": acc_id, "owner_id": r["owner_id"]}, check_spambot=True),
                timeout=60)
        except Exception as e:
            chk = {"status": "unknown", "reason": str(e)[:120]}
        status = chk.get("status")
        attempts = int(r["attempts"]) + 1

        if status == "active":
            await account_status.set_status(
                pool, acc_id, "active",
                reason="Спам-блок снят (перепроверка после прогрева)", source="rehab")
            await _advance(pool, acc_id, phase="freed",
                           note="снят по перепроверке", inc_attempts=1, freed=True)
            log.info("rehab: acc=%d освобождён после прогрева (попытка %d)", acc_id, attempts)
            return

        if status in _DEAD_STATUSES:
            await account_status.set_status(
                pool, acc_id, status, reason="Реабилитация: обнаружен терминальный статус",
                source="rehab")
            await _advance(pool, acc_id, phase="gone",
                           note=f"терминальный статус: {status}", inc_attempts=1)
            return

        # Всё ещё спам-блок. Уточняем kind; при вечном и исчерпанных попытках — stuck.
        kind = chk.get("spamblock_kind") or r.get("kind")
        exhausted = (
            attempts >= _MAX_RECHECKS
            or (kind == "perm" and int(r["appeal_count"]) >= _MAX_APPEALS
                and attempts >= max(3, _MAX_RECHECKS // 2))
        )
        if exhausted:
            await _advance(pool, acc_id, phase="stuck", kind=kind,
                           note=f"не снят за {attempts} перепроверок ({kind or '?'}) — нужен ручной разбор",
                           inc_attempts=1)
            log.info("rehab: acc=%d застрял (%s), уходит на ручной разбор", acc_id, kind)
            return

        # Ещё одна итерация прогрева с растущим бэк-оффом.
        await _advance(pool, acc_id, phase="warming", kind=kind,
                       note=f"ещё под блоком ({kind or '?'}), попытка {attempts}",
                       inc_attempts=1, delay_sec=_backoff_sec(attempts))
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
    for r in rows:
        try:
            await _process(pool, r)
            handled += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("rehab: process acc=%s failed", r.get("acc_id"), exc_info=True)
        # Разнос между аккаунтами в пределах тика — не бить залпом.
        await asyncio.sleep(random.uniform(2, 6))
    return handled


async def run_rehab_loop(pool) -> None:
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
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("rehab: тик упал", exc_info=True)
        await asyncio.sleep(_jitter(_TICK_SEC, frac=0.15))


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
    return {
        "in_progress": active,
        "freed": by_phase.get("freed", 0),
        "stuck": by_phase.get("stuck", 0),
        "gone": by_phase.get("gone", 0),
        "by_phase": by_phase,
    }
