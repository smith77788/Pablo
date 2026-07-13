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
import { connect } from "cloudflare:sockets";

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


async def get_workers_subdomain(api_token: str, account_id: str) -> str:
    """Узнать workers.dev-поддомен аккаунта (напр. 'infragram').

    URL воркера = https://{name}.{subdomain}.workers.dev — поддомен НЕ равен
    account_id (прежний код строил `{name}.{account_id}.workers.dev` — такой адрес
    не резолвится). Берём из env CF_WORKERS_SUBDOMAIN, иначе спрашиваем у CF API.
    Возвращает '' если поддомен не настроен (тогда деплой бессмыслен)."""
    env_sub = (os.getenv("CF_WORKERS_SUBDOMAIN", "") or "").strip().replace(".workers.dev", "")
    if env_sub:
        return env_sub
    import aiohttp
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/subdomain"
    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
                return ((data.get("result") or {}).get("subdomain") or "").strip()
    except Exception as e:
        log.error("get_workers_subdomain failed: %s", e)
        return ""


async def _enable_workers_dev(name: str, api_token: str, account_id: str) -> tuple:
    """Включить *.workers.dev-роут для скрипта (иначе URL отдаёт 404 даже после
    успешного PUT скрипта — это отдельное действие в CF API).
    Возвращает (ok, error)."""
    import aiohttp
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
           f"/workers/scripts/{name}/subdomain")
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json={"enabled": True}) as resp:
                if resp.status in (200, 201):
                    return True, None
                body = (await resp.text())[:300]
                log.error("enable workers.dev %s: %s %s", name, resp.status, body)
                return False, f"workers.dev route HTTP {resp.status}: {body}"
    except Exception as e:
        log.error("enable workers.dev %s failed: %s", name, e)
        return False, str(e)


async def deploy_worker(name: str, api_token: str, account_id: str,
                        subdomain: str = "") -> tuple:
    """Deploy a single CF Worker, включить workers.dev-роут и вернуть РАБОЧИЙ URL.

    Возвращает (url, error): при успехе (url, None), при сбое ("", 'текст ошибки CF').
    Скрипт использует ES-модули (`export default` + `import ... from "cloudflare:sockets"`),
    поэтому заливается как module-воркер через multipart с metadata.main_module,
    а НЕ сырым PUT'ом application/javascript (иначе CF трактует его как
    service-worker формат → 'Uncaught SyntaxError: Unexpected token export')."""
    import aiohttp

    if not subdomain:
        subdomain = await get_workers_subdomain(api_token, account_id)
    if not subdomain:
        msg = "workers.dev subdomain не настроен"
        log.error("deploy_worker %s: %s", name, msg)
        return "", msg

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{name}"
    headers = {"Authorization": f"Bearer {api_token}"}
    metadata = {"main_module": "worker.js", "compatibility_date": "2024-11-01"}
    form = aiohttp.FormData()
    form.add_field("metadata", json.dumps(metadata), content_type="application/json")
    form.add_field("worker.js", WORKER_TEMPLATE, filename="worker.js",
                   content_type="application/javascript+module")

    async with aiohttp.ClientSession() as session:
        async with session.put(url, headers=headers, data=form) as resp:
            if resp.status not in (200, 201):
                body = (await resp.text())[:300]
                log.error("Failed to deploy %s: %s %s", name, resp.status, body)
                return "", f"HTTP {resp.status}: {body}"
    # включаем публичный *.workers.dev роут (без этого URL 404-ит)
    ok, route_err = await _enable_workers_dev(name, api_token, account_id)
    if not ok:
        return "", route_err or "не удалось включить workers.dev-роут"
    worker_url = f"https://{name}.{subdomain}.workers.dev"
    log.info("Deployed CF Worker: %s -> %s", name, worker_url)
    return worker_url, None


async def deploy_pool(count: int, name_prefix: str, api_token: str, account_id: str,
                      subdomain: str = "", concurrency: int = 8) -> dict:
    """Deploy multiple CF Workers параллельно (с ограничением одновременности).
    Последовательный деплой 100 воркеров занимал минуты и вешал HTTP-запрос →
    таймаут шлюза; поэтому конкурентно + вызывать в фоне.

    Возвращает {"urls": [...], "errors": [...до 5...], "ok": N, "count": count}.
    Ошибки НЕ глотаются — первые несколько отдаём наружу, чтобы пользователь
    в /status увидел реальную причину сбоя CF, а не пустой результат."""
    if not subdomain:
        subdomain = await get_workers_subdomain(api_token, account_id)
    if not subdomain:
        msg = ("workers.dev subdomain не настроен для аккаунта Cloudflare — "
               "включите Workers в дашборде CF или задайте поддомен")
        log.error("deploy_pool: %s — деплой отменён", msg)
        return {"urls": [], "errors": [msg], "ok": 0, "count": count}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(i: int) -> tuple:
        async with sem:
            return await deploy_worker(f"{name_prefix}-{i}", api_token, account_id,
                                       subdomain=subdomain)

    results = await asyncio.gather(*[_one(i) for i in range(1, count + 1)],
                                   return_exceptions=True)
    urls, errors = [], []
    for r in results:
        if isinstance(r, Exception):
            errors.append(str(r)[:300])
            continue
        u, err = r if isinstance(r, tuple) else (r, None)
        if u:
            urls.append(u)
        elif err:
            errors.append(err)
    log.info("deploy_pool: %d/%d воркеров успешно", len(urls), count)
    return {"urls": urls, "errors": errors[:5], "ok": len(urls), "count": count}


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
