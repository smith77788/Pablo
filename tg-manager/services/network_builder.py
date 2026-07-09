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
