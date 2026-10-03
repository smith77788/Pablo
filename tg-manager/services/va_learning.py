"""Comparable observations and one-time, transactional editorial learning."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from statistics import median

MIN_POSTS = 3
MAX_POSTS = 10


async def capture_sample(pool, owner_id: int, channel_id: int, post_id: int,
                         stats: dict, observed_at: datetime) -> None:
    counters = [stats.get(key) for key in ("views", "reactions", "forwards")]
    if any(type(value) is not int or value < 0 for value in counters):
        return
    # Never backfill an old post with today's cumulative counters.
    await pool.execute(
        "UPDATE va_channel_posts SET learning_views=$4, learning_reactions=$5, "
        "learning_forwards=$6, learning_sampled_at=$7 "
        "WHERE id=$1 AND owner_id=$2 AND channel_key=$3 "
        "AND learning_sampled_at IS NULL AND published_at <= $7 - interval '24 hours' "
        "AND published_at >= $7 - interval '30 hours'",
        int(post_id), int(owner_id), str(channel_id), *counters, observed_at,
    )


def decide(weights: dict[str, float], samples: list[dict]) -> dict | None:
    groups = defaultdict(list)
    for sample in samples:
        pillar = sample["pillar"]
        if pillar in weights:
            groups[pillar].append(sample)
    eligible = {p: rows for p, rows in groups.items() if len(rows) >= MIN_POSTS}
    if len(eligible) < 2:
        return None
    # Equal-size batches and medians reduce volume bias and single-post outliers.
    size = min(MAX_POSTS, *(len(rows) for rows in eligible.values()))
    batches = {p: rows[:size] for p, rows in eligible.items()}
    scores = {
        p: median(row["learning_views"] + 3 * row["learning_reactions"]
                  + 5 * row["learning_forwards"] for row in rows)
        for p, rows in batches.items()
    }
    baseline = sum(scores.values()) / len(scores)
    updated = dict(weights)
    for pillar, score in scores.items():
        if baseline > 0:
            ratio = score / baseline
            step = 1 if ratio >= 1.25 else -1 if ratio <= 0.8 else 0
            updated[pillar] = max(1.0, min(10.0, weights[pillar] + step))
    details = "; ".join(
        f"{p}: {weights[p]:g} → {updated[p]:g}, медианный отклик {scores[p]:g}"
        for p in batches
    )
    outcome = "Доли рубрик изменены" if updated != weights else "Доли рубрик оставлены без изменений"
    explanation = (
        f"{outcome}. Сравнено по {size} новых поста каждой рубрики через 24–30 часов "
        f"после публикации. {details}. Отклик = просмотры + 3 × реакции + 5 × пересылки; "
        "это не заявки и не продажи. Изменение веса не больше одного шага. "
        "Эти посты повторно в обучении не участвуют."
    )
    return {"weights": updated, "post_ids": [r["id"] for rows in batches.values() for r in rows],
            "explanation": explanation}


def _decoded(value):
    return json.loads(value) if isinstance(value, str) else value


async def autotune(pool, owner_id: int, channel_id: int) -> dict | None:
    owner_id, channel_key = int(owner_id), str(channel_id)
    async with pool.acquire() as conn, conn.transaction():
        # Serialize learners and respect a concurrent owner disabling auto-tune.
        admin = await conn.fetchrow(
            "SELECT auto_tune FROM va_channel_admin WHERE owner_id=$1 AND channel_id=$2 FOR UPDATE",
            owner_id, int(channel_id),
        )
        if not admin or not admin["auto_tune"]:
            return None
        brain = await conn.fetchrow(
            "SELECT pillars, mix_weights FROM va_channel_brain "
            "WHERE owner_id=$1 AND channel_key=$2 FOR UPDATE", owner_id, channel_key,
        )
        if not brain:
            return None
        pillars = _decoded(brain["pillars"]) or []
        stored = _decoded(brain["mix_weights"]) or {}
        weights = {p: float(stored.get(p, 1)) for p in pillars}
        if len(weights) < 2:
            return None
        rows = await conn.fetch(
            "SELECT id, pillar, learning_views, learning_reactions, learning_forwards FROM ("
            "SELECT id, pillar, learning_views, learning_reactions, learning_forwards, "
            "row_number() OVER (PARTITION BY pillar ORDER BY published_at DESC, id DESC) AS n "
            "FROM va_channel_posts WHERE owner_id=$1 AND channel_key=$2 "
            "AND pillar=ANY($3::text[]) AND learned_at IS NULL "
            "AND learning_sampled_at BETWEEN published_at + interval '24 hours' "
            "AND published_at + interval '30 hours' "
            "AND learning_sampled_at <= now() "
            "AND published_at > now() - interval '30 days' "
            "AND learning_views >= 0 AND learning_reactions >= 0 AND learning_forwards >= 0"
            ") samples WHERE n <= $4 ORDER BY pillar, n",
            owner_id, channel_key, pillars, MAX_POSTS,
        )
        decision = decide(weights, rows)
        if not decision:
            return None
        new = decision["weights"]
        if new != weights:
            # Only the weights change: never overwrite the owner's other rules.
            await conn.execute(
                "UPDATE va_channel_brain SET mix_weights=$3::jsonb, updated_at=now() "
                "WHERE owner_id=$1 AND channel_key=$2",
                owner_id, channel_key, json.dumps(new, ensure_ascii=False),
            )
            await conn.execute(
                "INSERT INTO va_learning_decisions(owner_id,channel_id,old_weights,new_weights,"
                "post_ids,explanation) VALUES($1,$2,$3::jsonb,$4::jsonb,$5::bigint[],$6)",
                owner_id, int(channel_id), json.dumps(weights, ensure_ascii=False),
                json.dumps(new, ensure_ascii=False), decision["post_ids"], decision["explanation"],
            )
        await conn.execute(
            "UPDATE va_channel_posts SET learned_at=now() "
            "WHERE owner_id=$1 AND channel_key=$2 AND id=ANY($3::bigint[])",
            owner_id, channel_key, decision["post_ids"],
        )
        await conn.execute(
            "INSERT INTO va_admin_events(owner_id, channel_id, kind, text) VALUES($1,$2,'tune',$3)",
            owner_id, int(channel_id), decision["explanation"],
        )
        return new if new != weights else None


class LearningConflict(ValueError):
    """A safe rollback is no longer possible without overwriting newer settings."""


async def history(pool, owner_id: int, channel_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, explanation, created_at, reverted_at FROM va_learning_decisions "
        "WHERE owner_id=$1 AND channel_id=$2 ORDER BY id DESC LIMIT 10",
        int(owner_id), int(channel_id),
    )
    return [{"id": r["id"], "explanation": r["explanation"],
             "created_at": r["created_at"].isoformat(),
             "can_revert": i == 0 and r["reverted_at"] is None}
            for i, r in enumerate(rows)]


async def rollback(pool, owner_id: int, channel_id: int, decision_id: int) -> None:
    owner_id, channel_id = int(owner_id), int(channel_id)
    async with pool.acquire() as conn, conn.transaction():
        admin = await conn.fetchrow(
            "SELECT id FROM va_channel_admin WHERE owner_id=$1 AND channel_id=$2 FOR UPDATE",
            owner_id, channel_id,
        )
        if not admin:
            raise LearningConflict("Администратор канала не найден")
        brain = await conn.fetchrow(
            "SELECT pillars, mix_weights FROM va_channel_brain "
            "WHERE owner_id=$1 AND channel_key=$2 FOR UPDATE", owner_id, str(channel_id),
        )
        decision = await conn.fetchrow(
            "SELECT id,old_weights,new_weights,reverted_at FROM va_learning_decisions "
            "WHERE owner_id=$1 AND channel_id=$2 ORDER BY id DESC LIMIT 1 FOR UPDATE",
            owner_id, channel_id,
        )
        if not brain or not decision or decision["id"] != int(decision_id) or decision["reverted_at"]:
            raise LearningConflict("Можно отменить только последнее ещё не отменённое решение")
        old, new = _decoded(decision["old_weights"]), _decoded(decision["new_weights"])
        if _decoded(brain["mix_weights"]) != new or set(_decoded(brain["pillars"])) != set(new):
            raise LearningConflict("Рубрики уже изменились. Отмена затёрла бы более новые настройки")
        await conn.execute(
            "UPDATE va_channel_brain SET mix_weights=$3::jsonb, updated_at=now() "
            "WHERE owner_id=$1 AND channel_key=$2", owner_id, str(channel_id), json.dumps(old),
        )
        await conn.execute(
            "UPDATE va_learning_decisions SET reverted_at=now() WHERE id=$1 AND owner_id=$2",
            int(decision_id), owner_id,
        )
        # Keep the samples consumed: undo must not immediately reproduce the decision.
        await conn.execute(
            "INSERT INTO va_admin_events(owner_id,channel_id,kind,text) VALUES($1,$2,'tune',$3)",
            owner_id, channel_id, "Владелец отменил последнее обучение. Предыдущие веса восстановлены; "
            "для следующего решения нужны новые публикации.",
        )
