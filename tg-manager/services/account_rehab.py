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
             AND r.phase IN ('appeal','warming','recheck','off')
             AND COALESCE(a.acc_status,'active') <> 'spamblock'""",
        list(_DEAD_STATUSES),
    )
    # 'off' в списке намеренно: пока аккаунт под блоком, выключенное вручную
    # восстановление остаётся выключенным (условие выше это гарантирует).
    # Но как только ограничение снято, отметка снимается — и если аккаунт
    # заблокируют СНОВА через месяц, восстановление подхватит его как обычно,
    # а не промолчит из-за давно забытого выключения.


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


def describe(row: dict, now=None) -> dict[str, Any]:
    """Что происходит с аккаунтом в реабилитации и когда следующий шаг.

    Чистая функция: строка состояния → то, что видит человек.
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    phase = (row.get("phase") or "appeal").lower()
    label = PHASE_LABEL.get(phase, phase)
    hint = PHASE_HINT.get(phase, "")
    nxt = row.get("next_action_at")
    wait_s = None
    if phase in ACTIVE_PHASES and nxt is not None:
        try:
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            wait_s = max(0, int((nxt - now).total_seconds()))
        except (AttributeError, TypeError, ValueError):
            wait_s = None
    return {
        "phase": phase,
        "label": label,
        "hint": hint,
        "note": row.get("note") or "",
        "attempts": int(row.get("attempts") or 0),
        "max_attempts": _MAX_RECHECKS,
        "next_in_seconds": wait_s,
        "active": phase in ACTIVE_PHASES,
        "attention": phase in ATTENTION_PHASES,
        # Перезапуск имеет смысл только там, где автоматика уже сдалась.
        "restartable": phase in ("stuck", "off"),
    }


def build_terminal_alert(name: str, phase: str, attempts: int) -> str:
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
    return (
        "🛠 <b>Аккаунт не удалось восстановить</b>\n\n"
        f"<b>{name}</b> — ограничение держится после {int(attempts or 0)} перепроверок.\n\n"
        "Похоже на постоянный блок: автоматика больше ничего сделать не может."
        " Замените аккаунт или запустите восстановление заново вручную,"
        " если хотите дать ему ещё шанс."
    )


async def notify_terminal(pool, bot) -> int:
    """Сообщить владельцам об аккаунтах, чьё восстановление завершилось.

    Один раз на итог (метка `notified_phase`) — тот же приём, что у сторожа
    прокси и у прогрева: повторяющееся уведомление перестают читать.
    """
    try:
        rows = await pool.fetch(
            """SELECT r.acc_id, r.owner_id, r.phase, r.attempts,
                      a.phone, a.first_name
                 FROM account_rehab_state r
                 JOIN tg_accounts a ON a.id = r.acc_id
                WHERE r.phase IN ('freed','stuck')
                  AND COALESCE(r.notified_phase,'') <> r.phase
                LIMIT 50""")
    except Exception as e:
        log.debug("rehab: выборка завершённых не удалась: %s", e)
        return 0

    sent = 0
    for r in rows:
        name = r["first_name"] or r["phone"] or f"аккаунт #{r['acc_id']}"
        try:
            from database import db as _db

            await _db.notify_if_enabled(
                pool, bot, r["owner_id"], "restriction",
                build_terminal_alert(name, r["phase"], r["attempts"]),
                dedup_key=f"rehab:{r['acc_id']}:{r['phase']}")
            sent += 1
        except Exception:
            log.debug("rehab: уведомление не отправлено acc=%s", r["acc_id"])
        try:
            await pool.execute(
                "UPDATE account_rehab_state SET notified_phase=$2 WHERE acc_id=$1",
                r["acc_id"], r["phase"])
        except Exception:
            log.debug("rehab: метка уведомления не записана acc=%s", r["acc_id"])
    return sent


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
        return row is not None
    except Exception as e:
        log.warning("rehab: смена режима acc=%s не удалась: %s", acc_id, e)
        return False


async def list_for_owner(pool, owner_id: int, limit: int = 100) -> list[dict]:
    """Аккаунты владельца в реабилитации — с человеческим описанием фазы."""
    try:
        rows = await pool.fetch(
            """SELECT r.acc_id, r.phase, r.kind, r.attempts, r.appeal_count,
                      r.warm_cycles, r.note, r.first_seen, r.last_action_at,
                      r.next_action_at, r.freed_at,
                      a.phone, a.first_name, a.username, a.acc_status
                 FROM account_rehab_state r
                 JOIN tg_accounts a ON a.id = r.acc_id
                WHERE r.owner_id=$1
                ORDER BY (r.phase = 'stuck') DESC,
                         (r.phase IN ('appeal','warming','recheck')) DESC,
                         r.updated_at DESC
                LIMIT $2""",
            owner_id, limit)
    except Exception as e:
        log.debug("rehab: список для владельца %s не получен: %s", owner_id, e)
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["state"] = describe(d)
        for k in ("first_seen", "last_action_at", "next_action_at", "freed_at"):
            d[k] = d[k].isoformat() if d.get(k) else None
        out.append(d)
    return out


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
