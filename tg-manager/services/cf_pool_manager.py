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

# Сколько health-пингов подряд должен провалить воркер, прежде чем он помечается
# 'down' и аккаунты с него переезжают. Дебаунс от разовых сетевых блипов.
_DOWN_THRESHOLD = 2

# CF Worker template (from infra/cf_relay_worker.js)
WORKER_TEMPLATE = """
/**
 * CF Relay Worker — WebSocket → TCP proxy для Telegram DC
 * Auto-deployed by Infragram IP rotation system.
 */
import { connect } from "cloudflare:sockets";

// Продакшн-IP Telegram DC (те же, что в services/account_manager.py DC_IPS).
// Прежняя таблица роутила DC2/DC3 на IP DC1, а DC4 на IP DC5 — auth_key
// DC-специфичен, поэтому большинство аккаунтов (обычно DC2) молча не
// подключались. Значения обязаны совпадать с каноном в account_manager.
const DC_IPS = {
  1: "149.154.175.53",
  2: "149.154.167.51",
  3: "149.154.175.100",
  4: "149.154.167.91",
  5: "91.108.56.130",
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Health-probe: GET /health или / → 200 + colo + РЕАЛЬНЫЙ egress-IP воркера
    // (через subrequest к cdn-cgi/trace — ip= в ответе = исходящий IP воркера).
    // Так приложение видит фактические IP, а не только точку присутствия.
    if (url.pathname === '/' || url.pathname === '/health') {
      const colo = (request.cf && request.cf.colo) || null;
      let ip = null;
      try {
        const t = await fetch('https://www.cloudflare.com/cdn-cgi/trace');
        const txt = await t.text();
        const m = txt.match(/^ip=(.+)$/m);
        if (m) ip = m[1].trim();
      } catch (e) {}
      return new Response(JSON.stringify({ ok: true, colo, ip }), {
        headers: { 'content-type': 'application/json' },
      });
    }

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


async def count_relay_targets(pool, owner_id: int) -> dict:
    """Сколько аккаунтов реально нуждаются в релее (для авто-count деплоя).

    Аккаунт с собственным прокси (proxy_id задан) уже изолирован по IP — релей
    ему не нужен (_make_client всё равно предпочтёт прокси). Значит воркеры
    нужны только активным аккаунтам БЕЗ прокси. Возвращает {active, relay_needed}."""
    active = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
        owner_id) or 0
    relay_needed = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND proxy_id IS NULL",
        owner_id) or 0
    return {"active": int(active), "relay_needed": int(relay_needed)}


async def assign_urls_to_accounts(pool, owner_id: int, urls: list) -> dict:
    """Раздать URL воркеров аккаунтам БЕЗ собственного прокси (round-robin).

    Ключевое (anti-detection): аккаунты с proxy_id НЕ трогаем — у них своя
    IP-изоляция через прокси, релей им не нужен и только замусорил бы cf_relay_url.
    При числе воркеров ≥ числа таких аккаунтов раздача выходит 1:1 (уникальный IP)."""
    if not urls:
        return {"error": "No URLs to assign"}

    accounts = await pool.fetch(
        "SELECT id FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND proxy_id IS NULL ORDER BY id",
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

    unique = len(set(urls))
    return {"assigned": assigned, "urls": urls,
            "isolation_1to1": assigned <= unique, "unique_ips": unique}


async def check_pool(pool, owner_id: int, timeout: float = 8.0) -> dict:
    """Пинг каждого воркера (GET /health) — живость + colo (гео edge-точки CF).

    Обновляет cf_worker_pool.status ('active'/'down'). Разные colo → трафик
    аккаунтов расходится по разным точкам присутствия CF (косвенный индикатор
    разнообразия egress-IP; полной гарантии уникальности edge-IP CF не даёт)."""
    import aiohttp
    workers = await pool.fetch(
        "SELECT worker_url FROM cf_worker_pool WHERE owner_id=$1", owner_id)
    if not workers:
        return {"checked": 0, "alive": 0, "colos": [], "ips": [],
                "unique_ips": 0, "results": []}

    async def _ping(worker_url: str) -> dict:
        url = worker_url.rstrip("/") + "/health"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    if r.status == 200:
                        try:
                            j = await r.json()
                        except Exception:
                            j = {}
                        return {"url": worker_url, "alive": True,
                                "colo": (j or {}).get("colo"),
                                "ip": (j or {}).get("ip")}
                    return {"url": worker_url, "alive": False,
                            "error": f"HTTP {r.status}"}
        except Exception as e:
            return {"url": worker_url, "alive": False, "error": str(e)[:120]}

    results = await asyncio.gather(*[_ping(w["worker_url"]) for w in workers])
    for res in results:
        try:
            if res["alive"]:
                # живой → статус active, стрик сбрасываем
                await pool.execute(
                    "UPDATE cf_worker_pool SET status='active', fail_streak=0 "
                    "WHERE owner_id=$1 AND worker_url=$2", owner_id, res["url"])
            else:
                # дебаунс: помечаем down только когда стрик достиг порога — иначе
                # разовый блип дёргал бы аккаунты между воркерами каждый цикл.
                await pool.execute(
                    "UPDATE cf_worker_pool SET fail_streak=fail_streak+1, "
                    "status=CASE WHEN fail_streak+1 >= $3 THEN 'down' ELSE status END "
                    "WHERE owner_id=$1 AND worker_url=$2",
                    owner_id, res["url"], _DOWN_THRESHOLD)
        except Exception:
            # fail_streak-колонка ещё не примигрировала → без дебаунса
            await pool.execute(
                "UPDATE cf_worker_pool SET status=$1 WHERE owner_id=$2 AND worker_url=$3",
                "active" if res["alive"] else "down", owner_id, res["url"])
    alive = sum(1 for r in results if r["alive"])
    colos = sorted({r.get("colo") for r in results if r.get("colo")})
    ips = sorted({r.get("ip") for r in results if r.get("ip")})
    return {"checked": len(results), "alive": alive,
            "colos": colos, "ips": ips, "unique_ips": len(ips),
            "results": results}


async def get_pool_status(pool, owner_id: int) -> dict:
    """Get status of CF Worker pool."""
    workers = await pool.fetch(
        "SELECT * FROM cf_worker_pool WHERE owner_id=$1", owner_id)
    accounts = await pool.fetch(
        """SELECT cf_relay_url, COUNT(*) as cnt
           FROM tg_accounts WHERE owner_id=$1 AND cf_relay_url IS NOT NULL
           GROUP BY cf_relay_url""", owner_id)
    
    counts = await count_relay_targets(pool, owner_id)
    return {
        "workers": [dict(w) for w in workers],
        "accounts_by_url": [dict(a) for a in accounts],
        "total_workers": len(workers),
        "total_accounts_with_relay": sum(a['cnt'] for a in accounts),
        "active_accounts": counts["active"],
        "relay_needed": counts["relay_needed"],  # активные без своего прокси
    }


def _worker_name_from_url(worker_url: str) -> str:
    """https://{name}.{subdomain}.workers.dev → name (для DELETE через CF API)."""
    try:
        host = worker_url.split("//", 1)[-1].split("/", 1)[0]
        return host.split(".", 1)[0]
    except Exception:
        return ""


async def delete_worker(name: str, api_token: str, account_id: str) -> bool:
    """Удалить CF-воркер (скрипт) через CF API. Идемпотентно: 404 — тоже успех."""
    import aiohttp
    if not name:
        return False
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
           f"/workers/scripts/{name}")
    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.delete(url, headers=headers) as r:
                if r.status in (200, 204, 404):
                    return True
                log.error("delete_worker %s: %s %s", name, r.status,
                          (await r.text())[:200])
                return False
    except Exception as e:
        log.error("delete_worker %s failed: %s", name, e)
        return False


async def clear_pool(pool, owner_id: int, api_token: str = "",
                     account_id: str = "") -> dict:
    """Снести пул владельца: удалить воркеры в CF (если заданы доступы), очистить
    cf_worker_pool и снять cf_relay_url с аккаунтов. Прокси-аккаунты не затрагиваются
    (у них cf_relay_url и так пуст)."""
    workers = await pool.fetch(
        "SELECT worker_url FROM cf_worker_pool WHERE owner_id=$1", owner_id)
    deleted = 0
    if api_token and account_id:
        for w in workers:
            name = _worker_name_from_url(w["worker_url"])
            if name and await delete_worker(name, api_token, account_id):
                deleted += 1
    await pool.execute("DELETE FROM cf_worker_pool WHERE owner_id=$1", owner_id)
    cleared = await pool.fetchval(
        "WITH u AS (UPDATE tg_accounts SET cf_relay_url=NULL "
        "WHERE owner_id=$1 AND cf_relay_url IS NOT NULL RETURNING 1) "
        "SELECT COUNT(*) FROM u", owner_id)
    return {"workers_deleted": deleted, "db_workers_removed": len(workers),
            "accounts_cleared": int(cleared or 0)}


async def reconcile_pool(pool, owner_id: int, keep_urls: list,
                         api_token: str = "", account_id: str = "") -> int:
    """Удалить воркеры, которых больше нет в актуальном наборе (keep_urls): из CF
    и из cf_worker_pool. Возвращает число снятых. Без доступов — только чистит БД."""
    rows = await pool.fetch(
        "SELECT worker_url FROM cf_worker_pool WHERE owner_id=$1", owner_id)
    keep = set(keep_urls)
    stale = [r["worker_url"] for r in rows if r["worker_url"] not in keep]
    removed = 0
    for url in stale:
        if api_token and account_id:
            await delete_worker(_worker_name_from_url(url), api_token, account_id)
        await pool.execute(
            "DELETE FROM cf_worker_pool WHERE owner_id=$1 AND worker_url=$2",
            owner_id, url)
        removed += 1
    return removed


async def sync_relay_assignment(pool, owner_id: int) -> dict:
    """Раздать существующий пул воркеров аккаунтам, которым нужен релей, но которые
    его ещё не получили (напр. добавленные ПОСЛЕ деплоя). Прокси-аккаунты не трогаем.

    Живой аналог «органа»: держит покрытие релеем в тонусе без передеплоя.
    Возвращает {assigned, pool_size, naked_remaining}."""
    urls = [r["worker_url"] for r in await pool.fetch(
        "SELECT worker_url FROM cf_worker_pool WHERE owner_id=$1 "
        "AND status <> 'down' ORDER BY worker_url", owner_id)]
    if not urls:
        return {"assigned": 0, "pool_size": 0, "naked_remaining": None}
    naked = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
        "AND proxy_id IS NULL AND cf_relay_url IS NULL ORDER BY id", owner_id)
    # текущая загрузка воркеров (для равномерной раздачи новичкам)
    load_rows = await pool.fetch(
        "SELECT cf_relay_url, COUNT(*) c FROM tg_accounts WHERE owner_id=$1 "
        "AND cf_relay_url IS NOT NULL GROUP BY cf_relay_url", owner_id)
    load = {u: 0 for u in urls}
    for r in load_rows:
        if r["cf_relay_url"] in load:
            load[r["cf_relay_url"]] = r["c"]
    assigned = 0
    for acc in naked:
        url = min(urls, key=lambda u: load[u])  # наименее загруженный воркер
        await pool.execute("UPDATE tg_accounts SET cf_relay_url=$1 WHERE id=$2",
                           url, acc["id"])
        load[url] += 1
        assigned += 1
    return {"assigned": assigned, "pool_size": len(urls),
            "naked_remaining": 0 if assigned == len(naked) else len(naked) - assigned}


async def heal_dead_relays(pool, owner_id: int) -> dict:
    """Перевести аккаунты с МЁРТВОГО воркера на живой (аналог failover прокси).

    Мёртвые = cf_worker_pool.status='down'. Аккаунты, чей cf_relay_url указывает на
    down-воркер, переназначаются на наименее загруженный живой. Прокси-аккаунты не
    трогаем. Возвращает {reassigned, dead_workers, live_workers}."""
    live = [r["worker_url"] for r in await pool.fetch(
        "SELECT worker_url FROM cf_worker_pool WHERE owner_id=$1 "
        "AND status='active' ORDER BY worker_url", owner_id)]
    dead = [r["worker_url"] for r in await pool.fetch(
        "SELECT worker_url FROM cf_worker_pool WHERE owner_id=$1 "
        "AND status='down'", owner_id)]
    if not live or not dead:
        return {"reassigned": 0, "dead_workers": len(dead), "live_workers": len(live)}
    load_rows = await pool.fetch(
        "SELECT cf_relay_url, COUNT(*) c FROM tg_accounts WHERE owner_id=$1 "
        "AND cf_relay_url = ANY($2::text[]) GROUP BY cf_relay_url", owner_id, live)
    load = {u: 0 for u in live}
    for r in load_rows:
        load[r["cf_relay_url"]] = r["c"]
    stranded = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
        "AND proxy_id IS NULL AND cf_relay_url = ANY($2::text[]) ORDER BY id",
        owner_id, dead)
    reassigned = 0
    for acc in stranded:
        url = min(live, key=lambda u: load[u])
        await pool.execute("UPDATE tg_accounts SET cf_relay_url=$1 WHERE id=$2",
                           url, acc["id"])
        load[url] += 1
        reassigned += 1
    return {"reassigned": reassigned, "dead_workers": len(dead),
            "live_workers": len(live)}


async def run(pool, bot=None, *, interval_min: float = 30.0) -> None:
    """Фоновый монитор CF-пула (регистрируется в main.py как _resilient).

    Каждые interval_min: по каждому владельцу с воркерами — health-check, перевод
    аккаунтов с мёртвых воркеров на живые (heal_dead_relays), доназначение релея
    новым «голым» аккаунтам (sync_relay_assignment). Держит IP-изоляцию живой без
    ручного вмешательства — как орган, а не разовая кнопка.
    process-local таймер, не переживает рестарт (перезапустится сам с задержкой)."""
    log.info("cf_pool monitor: started (interval=%gmin)", interval_min)
    await asyncio.sleep(180)  # дать системе прогреться
    while True:
        try:
            owners = await pool.fetch(
                "SELECT DISTINCT owner_id FROM cf_worker_pool")
            for row in owners:
                oid = row["owner_id"]
                try:
                    chk = await check_pool(pool, oid)
                    healed = await heal_dead_relays(pool, oid)
                    synced = await sync_relay_assignment(pool, oid)
                    if healed.get("reassigned") or synced.get("assigned"):
                        log.info("cf_pool monitor owner=%s: alive=%d/%d healed=%d "
                                 "synced=%d", oid, chk.get("alive", 0),
                                 chk.get("checked", 0), healed["reassigned"],
                                 synced["assigned"])
                except Exception as e:
                    log.warning("cf_pool monitor owner=%s: %s", oid, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("cf_pool monitor loop error: %s", e, exc_info=True)
        await asyncio.sleep(interval_min * 60)
