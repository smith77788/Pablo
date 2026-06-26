"""Social Graph Engine — audience overlap mapping from operational data.

Builds a graph of channels/groups seen during operations.
Estimates audience overlap using co-presence of users in seen_entities.
Edges from content_meshes represent content flow relationships.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

_LOOP_INTERVAL = 21600  # recompute every 6 hours
_MIN_SHARED    = 5      # minimum shared users to record overlap


# ─── Node upsert ─────────────────────────────────────────────────────────────


async def upsert_node(
    pool: asyncpg.Pool,
    entity_id: str,
    entity_type: str = "channel",
    title: str | None = None,
    username: str | None = None,
    member_count: int = 0,
) -> int | None:
    """Upsert a graph node. Returns node id or None on error."""
    try:
        row = await pool.fetchrow(
            """INSERT INTO graph_nodes (entity_id, entity_type, title, username, member_count, last_seen)
               VALUES ($1,$2,$3,$4,$5,NOW())
               ON CONFLICT (entity_id) DO UPDATE
                   SET entity_type  = EXCLUDED.entity_type,
                       title        = COALESCE(EXCLUDED.title, graph_nodes.title),
                       username     = COALESCE(EXCLUDED.username, graph_nodes.username),
                       member_count = GREATEST(EXCLUDED.member_count, graph_nodes.member_count),
                       last_seen    = NOW()
               RETURNING id""",
            str(entity_id),
            entity_type,
            title,
            username,
            max(0, member_count or 0),
        )
        return row["id"] if row else None
    except Exception as e:
        log.debug("graph_engine.upsert_node: %s", e)
        return None


async def upsert_edge(
    pool: asyncpg.Pool,
    from_node_id: int,
    to_node_id: int,
    edge_type: str,
    weight: float = 1.0,
) -> None:
    """Upsert a directed edge. Never raises."""
    try:
        await pool.execute(
            """INSERT INTO graph_edges (from_node, to_node, edge_type, weight, last_seen)
               VALUES ($1,$2,$3,$4,NOW())
               ON CONFLICT (from_node, to_node, edge_type) DO UPDATE
                   SET weight    = GREATEST(EXCLUDED.weight, graph_edges.weight),
                       last_seen = NOW()""",
            from_node_id,
            to_node_id,
            edge_type,
            weight,
        )
    except Exception as e:
        log.debug("graph_engine.upsert_edge: %s", e)


# ─── Graph queries ────────────────────────────────────────────────────────────


async def get_user_nodes(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    """Returns graph nodes for channels operated by this user."""
    try:
        rows = await pool.fetch(
            """SELECT gn.id, gn.entity_id, gn.title, gn.username,
                      gn.member_count, gn.entity_type
               FROM graph_nodes gn
               WHERE gn.entity_id IN (
                   SELECT DISTINCT source_channel FROM content_meshes WHERE owner_id=$1
                     AND source_channel IS NOT NULL
                   UNION
                   SELECT DISTINCT target_channel FROM mesh_targets mt
                   JOIN content_meshes cm ON cm.id = mt.mesh_id
                   WHERE cm.owner_id=$1
               )
               ORDER BY gn.member_count DESC
               LIMIT 50""",
            owner_id,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("graph_engine.get_user_nodes: %s", e)
        return []


async def get_top_overlaps(
    pool: asyncpg.Pool,
    limit: int = 10,
    min_pct: float = 0.05,
) -> list[dict]:
    """Returns top audience overlaps globally."""
    try:
        rows = await pool.fetch(
            """SELECT
                   na.title AS title_a, na.entity_id AS id_a,
                   nb.title AS title_b, nb.entity_id AS id_b,
                   ao.overlap_pct, ao.shared_users
               FROM audience_overlaps ao
               JOIN graph_nodes na ON na.id = ao.node_a
               JOIN graph_nodes nb ON nb.id = ao.node_b
               WHERE ao.overlap_pct >= $1
               ORDER BY ao.overlap_pct DESC
               LIMIT $2""",
            min_pct,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("graph_engine.get_top_overlaps: %s", e)
        return []


async def get_node_stats(pool: asyncpg.Pool) -> dict:
    """Returns graph-wide stats."""
    try:
        row = await pool.fetchrow(
            """SELECT
               (SELECT COUNT(*) FROM graph_nodes) AS nodes,
               (SELECT COUNT(*) FROM graph_edges) AS edges,
               (SELECT COUNT(*) FROM audience_overlaps WHERE overlap_pct > 0.1) AS strong_overlaps"""
        )
        if row:
            return {
                "nodes": int(row["nodes"] or 0),
                "edges": int(row["edges"] or 0),
                "strong_overlaps": int(row["strong_overlaps"] or 0),
            }
    except Exception as e:
        log.debug("graph_engine.get_node_stats: %s", e)
    return {"nodes": 0, "edges": 0, "strong_overlaps": 0}


# ─── Graph building ───────────────────────────────────────────────────────────


async def _ingest_seen_entities(pool: asyncpg.Pool) -> int:
    """Pull channels/groups from seen_entities into graph_nodes."""
    count = 0
    try:
        rows = await pool.fetch(
            """SELECT DISTINCT ON (entity_id)
                   entity_id, entity_type
               FROM seen_entities
               WHERE entity_type IN ('channel', 'group', 'supergroup')
                 AND last_seen_at > NOW() - INTERVAL '30 days'
               LIMIT 5000"""
        )
        for r in rows:
            node_id = await upsert_node(
                pool,
                entity_id=str(r["entity_id"]),
                entity_type=r["entity_type"],
            )
            if node_id:
                count += 1
    except Exception as e:
        log.debug("graph_engine._ingest_seen_entities: %s", e)
    return count


async def _ingest_content_meshes(pool: asyncpg.Pool) -> int:
    """Build content-flow edges from active content meshes."""
    count = 0
    try:
        meshes = await pool.fetch(
            """SELECT cm.source_channel, mt.target_channel
               FROM content_meshes cm
               JOIN mesh_targets mt ON mt.mesh_id = cm.id AND mt.enabled = TRUE
               WHERE cm.enabled = TRUE
                 AND cm.source_channel IS NOT NULL
                 AND mt.target_channel IS NOT NULL"""
        )
        for m in meshes:
            src_id = await upsert_node(
                pool, str(m["source_channel"]), "channel"
            )
            tgt_id = await upsert_node(
                pool, str(m["target_channel"]), "channel"
            )
            if src_id and tgt_id:
                await upsert_edge(pool, src_id, tgt_id, "content_flow", 1.0)
                count += 1
    except Exception as e:
        log.debug("graph_engine._ingest_content_meshes: %s", e)
    return count


async def _compute_overlaps(pool: asyncpg.Pool) -> int:
    """Estimate audience overlap between channels using seen_entities co-presence.

    Overlap = Dice coefficient:
      2 * |A ∩ B| / (|A| + |B|)
    where |A ∩ B| = users seen in both chat A and chat B.
    """
    count = 0
    try:
        pairs = await pool.fetch(
            """
            SELECT
                se1.chat_id  AS chat_a,
                se2.chat_id  AS chat_b,
                COUNT(*)     AS shared,
                (SELECT COUNT(*) FROM seen_entities
                 WHERE chat_id=se1.chat_id AND entity_type='user') AS size_a,
                (SELECT COUNT(*) FROM seen_entities
                 WHERE chat_id=se2.chat_id AND entity_type='user') AS size_b
            FROM seen_entities se1
            JOIN seen_entities se2
              ON  se1.entity_id = se2.entity_id
              AND se1.chat_id   < se2.chat_id
              AND se1.entity_type = 'user'
              AND se2.entity_type = 'user'
            WHERE se1.last_seen_at > NOW() - INTERVAL '30 days'
              AND se2.last_seen_at > NOW() - INTERVAL '30 days'
            GROUP BY se1.chat_id, se2.chat_id
            HAVING COUNT(*) >= $1
            LIMIT 500
            """,
            _MIN_SHARED,
        )
        for p in pairs:
            shared = int(p["shared"])
            size_a = int(p["size_a"] or 0)
            size_b = int(p["size_b"] or 0)
            denominator = size_a + size_b
            if denominator == 0:
                continue
            dice = 2.0 * shared / denominator

            # Resolve graph node ids for these chat_ids
            na_id = await upsert_node(pool, str(p["chat_a"]), "channel")
            nb_id = await upsert_node(pool, str(p["chat_b"]), "channel")
            if not na_id or not nb_id:
                continue
            node_a = min(na_id, nb_id)
            node_b = max(na_id, nb_id)

            try:
                await pool.execute(
                    """INSERT INTO audience_overlaps
                           (node_a, node_b, overlap_pct, shared_users, computed_at)
                       VALUES ($1,$2,$3,$4,NOW())
                       ON CONFLICT (node_a, node_b) DO UPDATE
                           SET overlap_pct  = EXCLUDED.overlap_pct,
                               shared_users = EXCLUDED.shared_users,
                               computed_at  = NOW()""",
                    node_a,
                    node_b,
                    round(dice, 4),
                    shared,
                )
                count += 1
            except Exception as e:
                log.debug("graph_engine: overlap upsert error: %s", e)
    except Exception as e:
        log.debug("graph_engine._compute_overlaps: %s", e)
    return count


# ─── Background worker ────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot) -> None:
    log.info("Social Graph Engine started")
    while True:
        try:
            n_nodes   = await _ingest_seen_entities(pool)
            n_edges   = await _ingest_content_meshes(pool)
            n_overlaps = await _compute_overlaps(pool)
            log.info(
                "Graph Engine: +%d nodes, +%d edges, %d overlaps computed",
                n_nodes, n_edges, n_overlaps,
            )
        except Exception as e:
            log.error("Graph Engine loop error: %s", e)
        await asyncio.sleep(_LOOP_INTERVAL)
