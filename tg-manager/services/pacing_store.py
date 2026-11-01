"""Общий темп флота в Postgres: исходы операций и обучаемое состояние аккаунтов.

ЗАЧЕМ. Темп флота (`pacing_engine`: множитель задержки по последним исходам)
и выученная осторожность по аккаунту (`flood_engine`: серия флудов, риск,
паузы по типам действий) жили только в памяти исполнителя. Отсюда две беды:

* **рестарт** — а это каждый деплой — обнулял осторожность: первые действия
  после выпуска шли базовым темпом по флоту, который минуту назад ловил
  флуды;
* **второй процесс** считал темп по своим исходам: флуды, пойманные соседом,
  его не замедляли, и суммарный темп по флоту рос в N раз.

КАК. Цикл раз в `_SYNC_INTERVAL_S`:

1. пишет накопленные исходы в `fleet_pacing_events` и изменённые состояния
   аккаунтов в `account_pacing_state`;
2. читает последнее окно исходов ВСЕХ процессов и отдаёт его движку темпа.

Горячий путь (`recommended_delay`, `get_multiplier`) остаётся синхронным и
читает память: база в нём не участвует, поэтому её недоступность замедляет
только обмен, а не операции. Свои исходы, ещё не записанные в базу, движок
учитывает сразу (см. `PacingEngine._window`).

Состояние аккаунта читается из базы при старте каждой операции
(`flood_engine.hydrate_states`, не чаще раза в минуту на аккаунт) — аккаунт
одновременно ведёт только одна операция (аренда аккаунта в БД), поэтому
свежим оно должно быть именно в момент передачи.

Пауза аккаунта (`tg_accounts.cooldown_until`) сюда не входит: она пишется в
базу сразу, в момент флуда.
"""
from __future__ import annotations

import asyncio
import json
import logging

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_SYNC_INTERVAL_S = 5.0


def _metric(name: str, value: float = 1.0) -> None:
    try:
        from services import metrics as _m

        _m.inc(name, None, float(value))
    except Exception:
        pass


async def flush_events(pool, worker_id: str = "") -> int:
    """Записать накопленные исходы движка темпа. Сбой — вернуть в очередь."""
    from services.pacing_engine import get_pacing_engine

    eng = get_pacing_engine()
    recs = eng.drain_pending()
    if not recs:
        return 0
    try:
        await pool.execute(
            """INSERT INTO fleet_pacing_events
                   (created_at, action_type, ok, flood, ban, worker_id)
               SELECT to_timestamp(t.ts), t.typ, t.ok, t.fl, t.bn, $6
                 FROM unnest($1::float8[], $2::text[], $3::bool[], $4::bool[],
                             $5::bool[]) AS t(ts, typ, ok, fl, bn)""",
            [float(r["ts"]) for r in recs],
            [str(r.get("type") or "") for r in recs],
            [bool(r.get("ok")) for r in recs],
            [bool(r.get("flood")) for r in recs],
            [bool(r.get("ban")) for r in recs],
            worker_id or None,
        )
        return len(recs)
    except asyncio.CancelledError:
        eng.requeue(recs)
        raise
    except Exception:
        eng.requeue(recs)
        _metric("infragram_pacing_sync_errors_total")
        log_exc_swallow(log, "pacing_store.flush_events")
        return 0


async def pull_window(pool) -> int:
    """Прочитать общее окно исходов (всех процессов) и отдать движку темпа."""
    from services.pacing_engine import SHARED_WINDOW, get_pacing_engine

    try:
        rows = await pool.fetch(
            """SELECT EXTRACT(EPOCH FROM created_at) AS ts, action_type, ok, flood, ban
                 FROM fleet_pacing_events
                ORDER BY created_at DESC, id DESC
                LIMIT $1""",
            SHARED_WINDOW)
    except asyncio.CancelledError:
        raise
    except Exception:
        _metric("infragram_pacing_sync_errors_total")
        log_exc_swallow(log, "pacing_store.pull_window")
        return -1
    recs = [{"ts": float(r["ts"]), "type": r["action_type"] or "", "ok": bool(r["ok"]),
             "flood": bool(r["flood"]), "ban": bool(r["ban"])}
            for r in reversed(rows)]
    get_pacing_engine().set_shared(recs)
    return len(recs)


async def flush_account_states(pool) -> int:
    """Записать изменённые состояния аккаунтов. Сбой — вернуть отметки."""
    from services import flood_engine

    states = flood_engine.drain_dirty_states()
    if not states:
        return 0
    try:
        await pool.execute(
            """INSERT INTO account_pacing_state
                   (account_id, consecutive_floods, risk_score, action_delays,
                    last_flood_at, updated_at)
               SELECT t.aid, t.cf, t.rs, t.ad::jsonb,
                      CASE WHEN t.ago IS NULL THEN NULL
                           ELSE NOW() - make_interval(secs => t.ago) END,
                      NOW()
                 FROM unnest($1::bigint[], $2::int[], $3::real[], $4::text[],
                             $5::float8[]) AS t(aid, cf, rs, ad, ago)
               ON CONFLICT (account_id) DO UPDATE SET
                   consecutive_floods = EXCLUDED.consecutive_floods,
                   risk_score = EXCLUDED.risk_score,
                   action_delays = EXCLUDED.action_delays,
                   -- время флуда только вперёд: процесс, не видевший
                   -- свежего флуда соседа, не должен его «состарить»
                   last_flood_at = GREATEST(account_pacing_state.last_flood_at,
                                            EXCLUDED.last_flood_at),
                   updated_at = NOW()""",
            [s["account_id"] for s in states],
            [s["consecutive_floods"] for s in states],
            [s["risk_score"] for s in states],
            [json.dumps(s["action_delays"]) for s in states],
            [s["flood_ago_s"] for s in states],
        )
        return len(states)
    except asyncio.CancelledError:
        flood_engine.requeue_dirty([s["account_id"] for s in states])
        raise
    except Exception:
        flood_engine.requeue_dirty([s["account_id"] for s in states])
        _metric("infragram_pacing_sync_errors_total")
        log_exc_swallow(log, "pacing_store.flush_account_states")
        return 0


async def sync_once(pool, worker_id: str = "") -> None:
    """Один обмен: сначала запись (чтобы окно включало свои исходы), потом чтение."""
    await flush_events(pool, worker_id)
    await flush_account_states(pool)
    await pull_window(pool)


async def run_sync_loop(pool, worker_id: str = "") -> None:
    """Фоновый цикл обмена. На остановке — последняя запись, чтобы деплой не
    терял исходы и состояние, накопленные за последние секунды."""
    log.info("pacing_store: обмен темпом с базой запущен (раз в %.0f с)",
             _SYNC_INTERVAL_S)
    await sync_once(pool, worker_id)
    while True:
        try:
            await asyncio.sleep(_SYNC_INTERVAL_S)
            await sync_once(pool, worker_id)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(_final_flush(pool, worker_id))
            except BaseException:
                pass
            raise
        except Exception:
            log_exc_swallow(log, "pacing_store.run_sync_loop")


async def _final_flush(pool, worker_id: str) -> None:
    await flush_events(pool, worker_id)
    await flush_account_states(pool)
