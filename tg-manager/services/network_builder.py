"""Mass Network Builder — templates and topology for channel/bot networks.

Manages network templates, topology visualization, and batch creation
of interconnected channels, groups, and bots.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


async def init_network_tables(pool: asyncpg.Pool) -> None:
    """Create network builder tables."""
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS network_templates (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            template_type TEXT NOT NULL DEFAULT 'channel_group',
            nodes JSONB NOT NULL DEFAULT '[]',
            edges JSONB NOT NULL DEFAULT '[]',
            settings JSONB DEFAULT '{}',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    ''')
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS network_instances (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL,
            template_id INTEGER REFERENCES network_templates(id),
            name TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            nodes JSONB NOT NULL DEFAULT '[]',
            edges JSONB NOT NULL DEFAULT '[]',
            stats JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            launched_at TIMESTAMPTZ
        );
    ''')
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS network_nodes (
            id SERIAL PRIMARY KEY,
            instance_id INTEGER REFERENCES network_instances(id) ON DELETE CASCADE,
            node_type TEXT NOT NULL,
            ref_id BIGINT,
            label TEXT,
            config JSONB DEFAULT '{}',
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    ''')
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS network_edges (
            id SERIAL PRIMARY KEY,
            instance_id INTEGER REFERENCES network_instances(id) ON DELETE CASCADE,
            source_node_id INTEGER REFERENCES network_nodes(id),
            target_node_id INTEGER REFERENCES network_nodes(id),
            edge_type TEXT NOT NULL DEFAULT 'admin',
            config JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    ''')
    log.info("Network builder tables initialized")


@dataclass
class NetworkNode:
    node_type: str  # channel, group, bot
    label: str
    ref_id: Optional[int] = None
    config: dict = field(default_factory=dict)


@dataclass
class NetworkEdge:
    source_idx: int
    target_idx: int
    edge_type: str = "admin"
    config: dict = field(default_factory=dict)


async def create_template(pool: asyncpg.Pool, owner_id: int, name: str,
                          description: str = "", template_type: str = "channel_group",
                          nodes: list = None, edges: list = None) -> dict:
    """Create a network template."""
    try:
        row = await pool.fetchrow(
            '''INSERT INTO network_templates (owner_id, name, description, template_type, nodes, edges)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
               RETURNING id''',
            owner_id, name, description, template_type,
            json.dumps(nodes or []), json.dumps(edges or []))
        return {'ok': True, 'id': row['id']}
    except Exception as e:
        log.warning("create_template error: %s", e)
        return {'ok': False, 'error': str(e)}


async def get_templates(pool: asyncpg.Pool, owner_id: int) -> list:
    """Get all network templates."""
    try:
        rows = await pool.fetch(
            'SELECT * FROM network_templates WHERE owner_id = $1 ORDER BY name',
            owner_id)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_templates error: %s", e)
        return []


async def delete_template(pool: asyncpg.Pool, owner_id: int, template_id: int) -> dict:
    """Delete a network template."""
    try:
        await pool.execute(
            'DELETE FROM network_templates WHERE id = $1 AND owner_id = $2',
            template_id, owner_id)
        return {'ok': True}
    except Exception as e:
        log.warning("delete_template error: %s", e)
        return {'ok': False, 'error': str(e)}


async def create_instance(pool: asyncpg.Pool, owner_id: int, template_id: int,
                          name: str) -> dict:
    """Create a network instance from template."""
    try:
        template = await pool.fetchrow(
            'SELECT * FROM network_templates WHERE id = $1 AND owner_id = $2',
            template_id, owner_id)
        if not template:
            return {'ok': False, 'error': 'Template not found'}

        row = await pool.fetchrow(
            '''INSERT INTO network_instances (owner_id, template_id, name, nodes, edges)
               VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
               RETURNING id''',
            owner_id, template_id, name,
            template['nodes'], template['edges'])
        return {'ok': True, 'id': row['id']}
    except Exception as e:
        log.warning("create_instance error: %s", e)
        return {'ok': False, 'error': str(e)}


async def get_instances(pool: asyncpg.Pool, owner_id: int) -> list:
    """Get all network instances."""
    try:
        rows = await pool.fetch(
            '''SELECT ni.*, nt.name as template_name
               FROM network_instances ni
               LEFT JOIN network_templates nt ON nt.id = ni.template_id
               WHERE ni.owner_id = $1
               ORDER BY ni.created_at DESC''',
            owner_id)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_instances error: %s", e)
        return []


async def get_instance_detail(pool: asyncpg.Pool, owner_id: int,
                              instance_id: int) -> Optional[dict]:
    """Get network instance with nodes and edges."""
    try:
        instance = await pool.fetchrow(
            'SELECT * FROM network_instances WHERE id = $1 AND owner_id = $2',
            instance_id, owner_id)
        if not instance:
            return None
        nodes = await pool.fetch(
            'SELECT * FROM network_nodes WHERE instance_id = $1 ORDER BY created_at',
            instance_id)
        edges = await pool.fetch(
            'SELECT * FROM network_edges WHERE instance_id = $1',
            instance_id)
        return {
            'instance': dict(instance),
            'nodes': [dict(n) for n in nodes],
            'edges': [dict(e) for e in edges],
        }
    except Exception as e:
        log.warning("get_instance_detail error: %s", e)
        return None


async def add_node(pool: asyncpg.Pool, instance_id: int, node_type: str,
                   label: str, ref_id: Optional[int] = None,
                   config: dict = None) -> dict:
    """Add a node to a network instance."""
    try:
        row = await pool.fetchrow(
            '''INSERT INTO network_nodes (instance_id, node_type, label, ref_id, config)
               VALUES ($1, $2, $3, $4, $5::jsonb)
               RETURNING id''',
            instance_id, node_type, label, ref_id, json.dumps(config or {}))
        return {'ok': True, 'id': row['id']}
    except Exception as e:
        log.warning("add_node error: %s", e)
        return {'ok': False, 'error': str(e)}


async def add_edge(pool: asyncpg.Pool, instance_id: int, source_node_id: int,
                   target_node_id: int, edge_type: str = "admin",
                   config: dict = None) -> dict:
    """Add an edge between nodes."""
    try:
        row = await pool.fetchrow(
            '''INSERT INTO network_edges (instance_id, source_node_id, target_node_id, edge_type, config)
               VALUES ($1, $2, $3, $4, $5::jsonb)
               RETURNING id''',
            instance_id, source_node_id, target_node_id, edge_type,
            json.dumps(config or {}))
        return {'ok': True, 'id': row['id']}
    except Exception as e:
        log.warning("add_edge error: %s", e)
        return {'ok': False, 'error': str(e)}


async def update_node_status(pool: asyncpg.Pool, node_id: int, status: str,
                             ref_id: Optional[int] = None) -> dict:
    """Update node status after creation."""
    try:
        if ref_id:
            await pool.execute(
                'UPDATE network_nodes SET status = $1, ref_id = $2 WHERE id = $3',
                status, ref_id, node_id)
        else:
            await pool.execute(
                'UPDATE network_nodes SET status = $1 WHERE id = $2',
                status, node_id)
        return {'ok': True}
    except Exception as e:
        log.warning("update_node_status error: %s", e)
        return {'ok': False, 'error': str(e)}


async def get_network_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Get network statistics."""
    try:
        templates = await pool.fetchval(
            'SELECT COUNT(*) FROM network_templates WHERE owner_id = $1', owner_id)
        instances = await pool.fetchval(
            'SELECT COUNT(*) FROM network_instances WHERE owner_id = $1', owner_id)
        nodes = await pool.fetchval(
            '''SELECT COUNT(*) FROM network_nodes ni
               JOIN network_instances ni2 ON ni2.id = ni.instance_id
               WHERE ni2.owner_id = $1''', owner_id)
        edges = await pool.fetchval(
            '''SELECT COUNT(*) FROM network_edges ne
               JOIN network_instances ni ON ni.id = ne.instance_id
               WHERE ni.owner_id = $1''', owner_id)
        return {
            'templates': templates or 0,
            'instances': instances or 0,
            'total_nodes': nodes or 0,
            'total_edges': edges or 0,
        }
    except Exception as e:
        log.warning("get_network_stats error: %s", e)
        return {'templates': 0, 'instances': 0, 'total_nodes': 0, 'total_edges': 0}


async def get_graph_data(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Get graph data for visualization across all user instances."""
    try:
        instances = await pool.fetch(
            '''SELECT ni.id, ni.name, ni.status, ni.nodes, ni.edges
               FROM network_instances ni
               WHERE ni.owner_id = $1
               ORDER BY ni.created_at DESC''',
            owner_id)
        if not instances:
            return {'ok': True, 'nodes': [], 'edges': [], 'instances': []}

        all_nodes: list[dict] = []
        all_edges: list[dict] = []
        instance_summaries: list[dict] = []

        for inst in instances:
            inst_id = inst['id']
            inst_name = inst['name']
            inst_status = inst['status']
            inst_nodes = await pool.fetch(
                'SELECT * FROM network_nodes WHERE instance_id = $1', inst_id)
            inst_edges = await pool.fetch(
                'SELECT * FROM network_edges WHERE instance_id = $1', inst_id)

            node_map: dict[int, int] = {}
            for n in inst_nodes:
                global_idx = len(all_nodes)
                node_map[n['id']] = global_idx
                all_nodes.append({
                    'id': global_idx,
                    'instance_id': inst_id,
                    'instance_name': inst_name,
                    'db_id': n['id'],
                    'label': n['label'] or f"Node {n['id']}",
                    'type': n['node_type'],
                    'status': n['status'],
                })

            for e in inst_edges:
                src = node_map.get(e['source_node_id'])
                tgt = node_map.get(e['target_node_id'])
                if src is not None and tgt is not None:
                    all_edges.append({
                        'source': src,
                        'target': tgt,
                        'type': e['edge_type'],
                    })

            instance_summaries.append({
                'id': inst_id,
                'name': inst_name,
                'status': inst_status,
                'node_count': len(inst_nodes),
                'edge_count': len(inst_edges),
            })

        return {
            'ok': True,
            'nodes': all_nodes,
            'edges': all_edges,
            'instances': instance_summaries,
        }
    except Exception as e:
        log.warning("get_graph_data error: %s", e)
        return {'ok': False, 'error': str(e), 'nodes': [], 'edges': [], 'instances': []}


async def get_graph_clusters(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Cluster nodes by connectivity using union-find."""
    try:
        graph_data = await get_graph_data(pool, owner_id)
        if not graph_data.get('ok'):
            return {'ok': False, 'error': graph_data.get('error', 'Failed to get graph data')}

        nodes = graph_data['nodes']
        edges = graph_data['edges']
        n = len(nodes)
        if n == 0:
            return {'ok': True, 'clusters': [], 'node_cluster_map': {}}

        parent = list(range(n))
        rank = [0] * n

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra == rb:
                return
            if rank[ra] < rank[rb]:
                ra, rb = rb, ra
            parent[rb] = ra
            if rank[ra] == rank[rb]:
                rank[ra] += 1

        for edge in edges:
            union(edge['source'], edge['target'])

        cluster_map: dict[int, list[int]] = {}
        node_cluster_map: dict[int, int] = {}
        for i in range(n):
            root = find(i)
            if root not in cluster_map:
                cluster_map[root] = []
            cluster_map[root].append(i)
            node_cluster_map[i] = root

        clusters = []
        for idx, (_, member_ids) in enumerate(cluster_map.items()):
            cluster_nodes = [nodes[mid] for mid in member_ids]
            type_counts: dict[str, int] = {}
            for cn in cluster_nodes:
                t = cn['type']
                type_counts[t] = type_counts.get(t, 0) + 1
            clusters.append({
                'cluster_id': idx,
                'size': len(member_ids),
                'nodes': member_ids,
                'type_distribution': type_counts,
            })

        clusters.sort(key=lambda c: c['size'], reverse=True)
        return {'ok': True, 'clusters': clusters, 'node_cluster_map': node_cluster_map}
    except Exception as e:
        log.warning("get_graph_clusters error: %s", e)
        return {'ok': False, 'error': str(e)}


async def find_shortest_path(pool: asyncpg.Pool, owner_id: int,
                             source_id: int, target_id: int) -> dict:
    """Find shortest path between two nodes via BFS."""
    try:
        graph_data = await get_graph_data(pool, owner_id)
        if not graph_data.get('ok'):
            return {'ok': False, 'error': graph_data.get('error', 'Failed to get graph data')}

        nodes = graph_data['nodes']
        edges = graph_data['edges']

        db_to_global: dict[int, int] = {}
        for node in nodes:
            if node['db_id'] == source_id and source_id not in db_to_global:
                db_to_global[source_id] = node['id']
            if node['db_id'] == target_id and target_id not in db_to_global:
                db_to_global[target_id] = node['id']

        src_global = db_to_global.get(source_id)
        tgt_global = db_to_global.get(target_id)

        if src_global is None or tgt_global is None:
            return {'ok': False, 'error': 'Source or target node not found'}

        adj: dict[int, list[int]] = {}
        for edge in edges:
            adj.setdefault(edge['source'], []).append(edge['target'])
            adj.setdefault(edge['target'], []).append(edge['source'])

        from collections import deque
        queue: deque[int] = deque([src_global])
        prev: dict[int, int | None] = {src_global: None}

        while queue:
            current = queue.popleft()
            if current == tgt_global:
                break
            for neighbor in adj.get(current, []):
                if neighbor not in prev:
                    prev[neighbor] = current
                    queue.append(neighbor)

        if tgt_global not in prev:
            return {'ok': True, 'found': False, 'path': [], 'length': -1}

        path_ids: list[int] = []
        cur: int | None = tgt_global
        while cur is not None:
            path_ids.append(cur)
            cur = prev.get(cur)
        path_ids.reverse()

        path_nodes = [nodes[pid] for pid in path_ids]
        return {
            'ok': True,
            'found': True,
            'path': path_nodes,
            'length': len(path_ids) - 1,
        }
    except Exception as e:
        log.warning("find_shortest_path error: %s", e)
        return {'ok': False, 'error': str(e)}


async def get_community_detection(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Detect communities using label propagation algorithm."""
    try:
        graph_data = await get_graph_data(pool, owner_id)
        if not graph_data.get('ok'):
            return {'ok': False, 'error': graph_data.get('error', 'Failed to get graph data')}

        nodes = graph_data['nodes']
        edges = graph_data['edges']
        n = len(nodes)
        if n == 0:
            return {'ok': True, 'communities': [], 'node_community_map': {}}

        import random
        adj: dict[int, list[int]] = {}
        for edge in edges:
            adj.setdefault(edge['source'], []).append(edge['target'])
            adj.setdefault(edge['target'], []).append(edge['source'])

        label = list(range(n))
        max_iterations = n * 10
        order = list(range(n))

        for _ in range(max_iterations):
            random.shuffle(order)
            changed = False
            for node_idx in order:
                neighbors = adj.get(node_idx, [])
                if not neighbors:
                    continue
                freq: dict[int, int] = {}
                for nb in neighbors:
                    nb_label = label[nb]
                    freq[nb_label] = freq.get(nb_label, 0) + 1
                best_label = max(freq, key=freq.get)  # type: ignore[arg-type]
                if label[node_idx] != best_label:
                    label[node_idx] = best_label
                    changed = True
            if not changed:
                break

        community_map: dict[int, list[int]] = {}
        node_community_map: dict[int, int] = {}
        for i in range(n):
            lbl = label[i]
            community_map.setdefault(lbl, []).append(i)
            node_community_map[i] = lbl

        communities = []
        for idx, (_, member_ids) in enumerate(community_map.items()):
            community_nodes = [nodes[mid] for mid in member_ids]
            type_counts: dict[str, int] = {}
            for cn in community_nodes:
                t = cn['type']
                type_counts[t] = type_counts.get(t, 0) + 1
            internal_edges = sum(
                1 for e in edges
                if e['source'] in member_ids and e['target'] in member_ids
            )
            total_possible = len(member_ids) * (len(member_ids) - 1) // 2
            density = internal_edges / total_possible if total_possible > 0 else 0.0
            communities.append({
                'community_id': idx,
                'size': len(member_ids),
                'nodes': member_ids,
                'type_distribution': type_counts,
                'internal_edges': internal_edges,
                'density': round(density, 4),
            })

        communities.sort(key=lambda c: c['size'], reverse=True)
        return {
            'ok': True,
            'communities': communities,
            'node_community_map': node_community_map,
            'num_communities': len(communities),
        }
    except Exception as e:
        log.warning("get_community_detection error: %s", e)
        return {'ok': False, 'error': str(e)}
