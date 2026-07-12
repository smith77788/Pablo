#!/usr/bin/env python3
"""
Deploy multiple Cloudflare Workers for IP rotation.

Each Worker gives a unique Cloudflare edge IP to Telegram.
Deploy multiple Workers → each account gets a different IP.

Usage:
    python deploy_cf_workers.py --count 10 --name tg-relay
    
This will create 10 Workers named tg-relay-1, tg-relay-2, etc.
on Cloudflare, and store the URLs in the database.

Prerequisites:
    1. Cloudflare account with Workers enabled
    2. CF_API_TOKEN environment variable set
    3. CF_ACCOUNT_ID environment variable set
"""

import os
import sys
import json
import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# CF Worker template (from infra/cf_relay_worker.js)
WORKER_TEMPLATE = """
/**
 * CF Relay Worker — WebSocket → TCP proxy для Telegram DC
 * Auto-deployed by Infragram IP rotation system.
 */
const DC_IPS = {
  1: "149.154.175.53",
  2: "149.154.175.53",
  3: "149.154.175.53",
  4: "91.108.56.130",
  5: "91.108.56.130",
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const dcId = parseInt(url.pathname.split('/').pop());
    
    if (!DC_IPS[dcId]) {
      return new Response('Invalid DC ID', { status: 400 });
    }
    
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    
    ctx.waitUntil(this.handleConnection(server, DC_IPS[dcId], dcId));
    
    return new Response(null, {
      status: 101,
      webSocket: client,
    });
  },

  async handleConnection(ws, targetIp, dcId) {
    try {
      const tcpSocket = connect({ hostname: targetIp, port: 443 });
      const writer = tcpSocket.writable.getWriter();
      const reader = tcpSocket.readable.getReader();
      
      ws.accept();
      
      const pump = async () => {
        try {
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            ws.send(value);
          }
        } catch (e) {}
      };
      
      ws.addEventListener('message', async (event) => {
        await writer.write(event.data);
      });
      
      ws.addEventListener('close', () => {
        try { reader.cancel(); } catch(e) {}
        try { writer.close(); } catch(e) {}
      });
      
      await pump();
    } catch (e) {
      ws.close(1011, 'Connection error');
    }
  }
};
"""


async def deploy_worker(name: str, api_token: str, account_id: str) -> str:
    """Deploy a single CF Worker and return its URL."""
    import aiohttp
    
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{name}"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/javascript",
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.put(url, headers=headers, data=WORKER_TEMPLATE) as resp:
            if resp.status == 200:
                worker_url = f"https://{name}.{account_id}.workers.dev"
                log.info("Deployed CF Worker: %s -> %s", name, worker_url)
                return worker_url
            else:
                error = await resp.text()
                log.error("Failed to deploy %s: %s", name, error)
                return ""


async def deploy_pool(count: int, name_prefix: str, api_token: str, account_id: str) -> list:
    """Deploy multiple CF Workers and return their URLs."""
    urls = []
    for i in range(1, count + 1):
        name = f"{name_prefix}-{i}"
        url = await deploy_worker(name, api_token, account_id)
        if url:
            urls.append(url)
            log.info("Deployed %d/%d: %s", i, count, url)
        else:
            log.error("Failed to deploy %d/%d: %s", i, count, name)
    return urls


async def assign_urls_to_accounts(pool, owner_id: int, urls: list) -> dict:
    """Assign CF Worker URLs to accounts (round-robin)."""
    if not urls:
        return {"error": "No URLs to assign"}
    
    accounts = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
        owner_id)
    
    assigned = 0
    for i, acc in enumerate(accounts):
        url = urls[i % len(urls)]
        await pool.execute(
            "UPDATE tg_accounts SET cf_relay_url=$1 WHERE id=$2",
            url, acc['id'])
        assigned += 1
    
    # Store URLs in cf_worker_pool
    for url in urls:
        await pool.execute(
            """INSERT INTO cf_worker_pool (owner_id, worker_url, status)
               VALUES ($1, $2, 'active')
               ON CONFLICT (owner_id, worker_url) DO NOTHING""",
            owner_id, url)
    
    return {"assigned": assigned, "urls": urls}


async def get_pool_status(pool, owner_id: int) -> dict:
    """Get status of CF Worker pool."""
    workers = await pool.fetch(
        "SELECT * FROM cf_worker_pool WHERE owner_id=$1", owner_id)
    accounts = await pool.fetch(
        """SELECT cf_relay_url, COUNT(*) as cnt
           FROM tg_accounts WHERE owner_id=$1 AND cf_relay_url IS NOT NULL
           GROUP BY cf_relay_url""", owner_id)
    
    return {
        "workers": [dict(w) for w in workers],
        "accounts_by_url": [dict(a) for a in accounts],
        "total_workers": len(workers),
        "total_accounts_with_relay": sum(a['cnt'] for a in accounts),
    }
