require('dotenv').config();
const express = require('express');
const path = require('path');
const cors = require('cors');
let compression;
try { compression = require('compression'); } catch {}
const { initDatabase, get: dbGet, closeDatabase } = require('./database');
const { initBot } = require('./bot');
const apiRouter = require('./routes/api');
const { WebSocketServer } = require('ws');

if (!process.env.JWT_SECRET || process.env.JWT_SECRET.length < 32) {
  console.error('FATAL: JWT_SECRET must be set to a strong value (>= 32 chars).');
  process.exit(1);
}

let botInstance = null;

const app = express();
const PORT = process.env.PORT || 3000;

// ─── Logging ─────────────────────────────────────────────────────────────────
const LOG_JSON = process.env.NODE_ENV === 'production' || process.env.LOG_JSON === '1';

if (LOG_JSON) {
  // JSON structured logs for production
  app.use((req, res, next) => {
    const start = Date.now();
    res.on('finish', () => {
      if (req.path === '/api/health' || req.path === '/health') return; // Skip health checks
      const log = {
        ts: new Date().toISOString(),
        method: req.method,
        path: req.path,
        status: res.statusCode,
        ms: Date.now() - start,
        ip: req.ip || req.connection.remoteAddress,
        ua: req.get('user-agent')?.slice(0, 80),
      };
      if (res.statusCode >= 400) {
        log.level = 'warn';
      } else {
        log.level = 'info';
      }
      console.log(JSON.stringify(log));
    });
    next();
  });
} else {
  // Development: use morgan
  try {
    const morgan = require('morgan');
    app.use(morgan('dev'));
  } catch {}
}

// ─── Security headers ─────────────────────────────────────────────────────────
try {
  const helmet = require('helmet');
  app.use(helmet({
    contentSecurityPolicy: {
      directives: {
        defaultSrc: ["'self'"],
        scriptSrc: ["'self'", "'unsafe-inline'", "cdn.tailwindcss.com", "cdnjs.cloudflare.com"],
        styleSrc: ["'self'", "'unsafe-inline'", "cdnjs.cloudflare.com"],
        imgSrc: ["'self'", "data:", "https:"],
        connectSrc: ["'self'", "ws:", "wss:"],
        fontSrc: ["'self'", "cdnjs.cloudflare.com"],
      }
    },
    crossOriginEmbedderPolicy: false,
    referrerPolicy: { policy: 'strict-origin-when-cross-origin' },
  }));
  app.use(helmet.noSniff());
  app.use(helmet.hidePoweredBy());
  app.use(helmet.frameguard({ action: 'sameorigin' }));
} catch {}

// ─── Admin route security headers ────────────────────────────────────────────
app.use('/admin', (req, res, next) => {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Frame-Options', 'DENY');
  next();
});

// ─── Rate limiting ────────────────────────────────────────────────────────────
try {
  const rateLimit = require('express-rate-limit');
  const apiLimiter = rateLimit({
    windowMs: 15 * 60 * 1000, // 15 minutes
    max: 100,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Слишком много запросов. Попробуйте позже.' }
  });
  const authLimiter = rateLimit({
    windowMs: 15 * 60 * 1000,
    max: 10,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Слишком много попыток входа. Попробуйте через 15 минут.' }
  });
  const ordersLimiter = rateLimit({
    windowMs: 15 * 60 * 1000,
    max: 20,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Слишком много заявок. Попробуйте позже.' }
  });
  app.use('/api/', apiLimiter);
  app.use('/api/admin/login', authLimiter);
  app.use('/api/orders', ordersLimiter);
  app.use('/api/quick-booking', ordersLimiter); // same 20/15m limit for quick bookings
} catch {}

// ─── CORS ─────────────────────────────────────────────────────────────────────
const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || '').split(',').map(s => s.trim()).filter(Boolean);
app.use(cors(ALLOWED_ORIGINS.length ? {
  origin: ALLOWED_ORIGINS,
  credentials: true
} : {}));

app.use(express.json({ limit: '2mb' }));
app.use(express.urlencoded({ extended: true, limit: '2mb' }));

// ─── Compression ──────────────────────────────────────────────────────────────
if (compression) app.use(compression());

// ─── Response-time header (useful for monitoring / APM) ───────────────────────
app.use((req, res, next) => {
  const start = process.hrtime.bigint();
  res.on('finish', () => {
    const ms = Number(process.hrtime.bigint() - start) / 1e6;
    // header may already be sent for streamed responses — ignore the throw
    try { res.setHeader('X-Response-Time', `${ms.toFixed(2)}ms`); } catch (_) {}
  });
  next();
});

// ─── SEO: Dynamic sitemap.xml ─────────────────────────────────────────────────
app.get('/sitemap.xml', async (req, res) => {
  try {
    const { query: dbQuery } = require('./database');
    const models = await dbQuery('SELECT id, name, updated_at FROM models WHERE available=1 ORDER BY featured DESC, id ASC');
    const baseUrl = process.env.SITE_URL || 'https://nevesty-models.ru';

    const modelUrls = models.map(m => `
  <url>
    <loc>${baseUrl}/model/${m.id}</loc>
    <lastmod>${m.updated_at ? m.updated_at.split('T')[0] : new Date().toISOString().split('T')[0]}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>`).join('');

    const today = new Date().toISOString().split('T')[0];
    const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>${baseUrl}/</loc><lastmod>${today}</lastmod><changefreq>daily</changefreq><priority>1.0</priority></url>
  <url><loc>${baseUrl}/catalog.html</loc><lastmod>${today}</lastmod><changefreq>daily</changefreq><priority>0.9</priority></url>
  <url><loc>${baseUrl}/booking.html</loc><lastmod>${today}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>
  <url><loc>${baseUrl}/about.html</loc><lastmod>${today}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>${baseUrl}/contact.html</loc><lastmod>${today}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>${baseUrl}/pricing.html</loc><lastmod>${today}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>${baseUrl}/cases.html</loc><lastmod>${today}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>${baseUrl}/faq.html</loc><lastmod>${today}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>${baseUrl}/search.html</loc><lastmod>${today}</lastmod><changefreq>weekly</changefreq><priority>0.6</priority></url>
  <url><loc>${baseUrl}/favorites.html</loc><lastmod>${today}</lastmod><changefreq>weekly</changefreq><priority>0.5</priority></url>
  <url><loc>${baseUrl}/cabinet.html</loc><lastmod>${today}</lastmod><changefreq>monthly</changefreq><priority>0.5</priority></url>
  <url><loc>${baseUrl}/privacy.html</loc><lastmod>${today}</lastmod><changefreq>monthly</changefreq><priority>0.3</priority></url>${modelUrls}
</urlset>`;

    res.header('Content-Type', 'application/xml');
    res.send(xml);
  } catch (e) {
    console.error('[sitemap]', e.message);
    res.status(500).send('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>');
  }
});

// ─── SEO: Dynamic robots.txt ──────────────────────────────────────────────────
app.get('/robots.txt', (req, res) => {
  const baseUrl = process.env.SITE_URL || 'https://nevesty-models.ru';
  res.type('text/plain');
  res.send(
    `User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin/\nDisallow: /uploads/\nDisallow: /data/\n\nSitemap: ${baseUrl}/sitemap.xml`
  );
});

// ─── SEO: Server-side rendered model page with OG/Schema.org meta tags ────────
app.get('/model/:id', async (req, res) => {
  try {
    const { get: dbGetModel } = require('./database');
    const modelId = parseInt(req.params.id, 10);
    if (!Number.isInteger(modelId) || modelId <= 0) return res.redirect('/catalog.html');

    const model = await dbGetModel('SELECT * FROM models WHERE id=? AND available=1', [modelId]);
    if (!model) return res.redirect('/catalog.html');

    const siteUrl = process.env.SITE_URL || 'https://nevesty-models.ru';
    let photoUrl = `${siteUrl}/img/og-default.jpg`;
    if (model.photo_main) {
      photoUrl = `${siteUrl}/uploads/${model.photo_main}`;
    } else if (model.photos) {
      try {
        const photos = JSON.parse(model.photos);
        if (Array.isArray(photos) && photos.length > 0) {
          photoUrl = `${siteUrl}/uploads/${photos[0]}`;
        }
      } catch (_) {}
    }

    const modelName = (model.name || 'Модель').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const title = `${modelName} — Nevesty Models Agency`;
    const rawDesc = model.bio || `Модель ${model.name || ''}${model.city ? ', ' + model.city : ''}`;
    const desc = rawDesc.slice(0, 160).replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const descSchema = rawDesc.slice(0, 160).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    const canonicalUrl = `${siteUrl}/model/${model.id}`;

    const ogTags = `
  <!-- Open Graph / Twitter Card (dynamic, server-injected) -->
  <meta property="og:type" content="profile" />
  <meta property="og:title" content="${title.replace(/"/g, '&quot;')}" />
  <meta property="og:description" content="${desc}" />
  <meta property="og:image" content="${photoUrl}" />
  <meta property="og:url" content="${canonicalUrl}" />
  <meta property="og:site_name" content="Nevesty Models" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="${title.replace(/"/g, '&quot;')}" />
  <meta name="twitter:description" content="${desc}" />
  <meta name="twitter:image" content="${photoUrl}" />
  <link rel="canonical" href="${canonicalUrl}" />
  <!-- Schema.org Person -->
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "Person",
    "name": "${(model.name || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"')}",
    "description": "${descSchema}",
    "image": "${photoUrl}",
    "url": "${canonicalUrl}",
    "worksFor": {
      "@type": "Organization",
      "name": "Nevesty Models Agency",
      "url": "${siteUrl}"
    }
  }
  <\/script>
  <!-- Schema.org BreadcrumbList -->
  <script type="application/ld+json">
  ${JSON.stringify({
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "itemListElement": [
      { "@type": "ListItem", "position": 1, "name": "Главная", "item": siteUrl },
      { "@type": "ListItem", "position": 2, "name": "Каталог моделей", "item": `${siteUrl}/catalog.html` },
      { "@type": "ListItem", "position": 3, "name": model.name || 'Модель', "item": canonicalUrl }
    ]
  })}
  <\/script>`;

    const fs = require('fs');
    let html = fs.readFileSync(path.join(__dirname, 'public', 'model.html'), 'utf8');

    // Replace static OG tags in <head> with dynamic ones
    html = html.replace(/<meta property="og:title"[^>]*>/i, '');
    html = html.replace(/<meta property="og:description"[^>]*>/i, '');
    html = html.replace(/<meta property="og:type"[^>]*>/i, '');
    html = html.replace(/<meta property="og:image"[^>]*>/i, '');
    html = html.replace(/<meta property="og:url"[^>]*>/i, '');
    html = html.replace(/<meta property="og:site_name"[^>]*>/i, '');
    html = html.replace(/<meta name="twitter:card"[^>]*>/i, '');
    html = html.replace(/<link rel="canonical"[^>]*>/i, '');

    // Inject dynamic tags and update title
    html = html.replace('</head>', ogTags + '\n</head>');
    html = html.replace(/<title>[^<]*<\/title>/i, `<title>${title.replace(/</g, '&lt;')}</title>`);
    // Add data attribute for JS to auto-load the model
    html = html.replace('<body', `<body data-model-id="${model.id}"`);

    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.send(html);
  } catch (e) {
    console.error('[model-page]', e.message);
    res.redirect('/catalog.html');
  }
});

// ─── Static files ─────────────────────────────────────────────────────────────
app.use('/uploads', express.static(path.join(__dirname, 'uploads'), {
  maxAge: '7d',
  etag: true
}));
app.use(express.static(path.join(__dirname, 'public'), {
  maxAge: process.env.NODE_ENV === 'production' ? '1d' : 0,
  etag: true,
  lastModified: true,
}));

// ─── API ──────────────────────────────────────────────────────────────────────
app.use('/api', apiRouter);

// ─── Health check ─────────────────────────────────────────────────────────────
async function buildHealthResponse() {
  const os = require('os');
  const pkg = require('./package.json');
  const mem = process.memoryUsage();
  const memMb = Math.round(mem.heapUsed / 1024 / 1024 * 10) / 10;
  const loadAvg = os.loadavg();
  const freeMemMb = Math.round(os.freemem() / 1024 / 1024);
  const totalMemMb = Math.round(os.totalmem() / 1024 / 1024);
  const memUsedMb = Math.round(mem.rss / 1024 / 1024);
  const memAlertMb = parseInt(process.env.MEMORY_ALERT_MB || '500');
  let dbStatus = 'ok';
  let factoryLastCycle = null;
  let ordersToday = 0;
  let activeOrders = 0;
  let modelsCount = 0;

  let walStatus = null;
  try {
    await dbGet('SELECT 1 as ok');
    // Fetch DB metrics in parallel
    const today = new Date().toISOString().slice(0, 10);
    const [todayRow, activeRow, modelsRow] = await Promise.all([
      dbGet('SELECT COUNT(*) as n FROM orders WHERE date(created_at)=?', [today]),
      dbGet("SELECT COUNT(*) as n FROM orders WHERE status IN ('new','confirmed','in_progress')"),
      dbGet('SELECT COUNT(*) as n FROM models WHERE available=1'),
    ]);
    ordersToday = todayRow?.n || 0;
    activeOrders = activeRow?.n || 0;
    modelsCount = modelsRow?.n || 0;
  } catch (e) {
    dbStatus = 'error: ' + e.message;
  }

  try {
    // WAL checkpoint in passive mode (doesn't block reads/writes)
    const walRow = await dbGet('PRAGMA wal_checkpoint(PASSIVE)');
    walStatus = {
      mode: 'WAL',
      total_pages: walRow ? walRow.log : 0,
      moved_pages: walRow ? walRow.ckpt : 0,
    };
  } catch (e) {
    walStatus = { error: e.message };
  }

  try {
    const row = await dbGet("SELECT value FROM bot_settings WHERE key = 'factory_last_cycle'");
    if (row) factoryLastCycle = row.value;
  } catch (_) { /* table may not have this key yet */ }

  let factoryStatus = 'unknown';
  try {
    const { execSync } = require('child_process');
    const out = execSync('python3 /home/user/Pablo/factory_main.py --status 2>/dev/null', { timeout: 5000 }).toString();
    factoryStatus = out.includes('Last cycle:') ? 'ok' : 'no_cycles';
  } catch (_) { factoryStatus = 'unavailable'; }

  // Cache stats
  let cacheStats = {};
  try {
    const { cache } = require('./services/cache');
    const stats = cache.stats();
    cacheStats = { keys: stats.keys, hit_rate: stats.hit_rate };
  } catch (_) {}

  const uptime = Math.floor(process.uptime());
  const overallStatus = dbStatus === 'ok' ? 'ok' : 'degraded';
  return {
    status: overallStatus,
    uptime_sec: uptime,
    timestamp: new Date().toISOString(),
    uptime,
    uptimeHuman: `${Math.floor(uptime / 3600)}h ${Math.floor((uptime % 3600) / 60)}m`,
    version: pkg.version || '1.0.0',
    memory: {
      rss_mb: memUsedMb,
      heap_used_mb: Math.round(mem.heapUsed / 1024 / 1024),
      heap_total_mb: Math.round(mem.heapTotal / 1024 / 1024),
      free_mb: freeMemMb,
      total_mb: totalMemMb,
      alert: memUsedMb > memAlertMb,
    },
    cpu: {
      load_1m: Math.round(loadAvg[0] * 100) / 100,
      load_5m: Math.round(loadAvg[1] * 100) / 100,
      cores: os.cpus().length,
    },
    db: { status: dbStatus, wal: walStatus },
    components: {
      database: dbStatus,
      bot: botInstance ? 'ok' : 'not_initialized',
      factory: factoryStatus,
      factory_last_cycle: factoryLastCycle,
      cache: cacheStats,
    },
    metrics: {
      memory_mb: memMb,
      memory_rss_mb: memUsedMb,
      orders_today: ordersToday,
      active_orders: activeOrders,
      models_count: modelsCount,
    },
    // Legacy fields kept for compatibility
    database: dbStatus,
    bot: botInstance ? 'connected' : 'not_initialized',
    memory_mb: memUsedMb,
    ts: new Date().toISOString(),
  };
}

app.get('/health', async (req, res) => {
  try {
    const health = await buildHealthResponse();
    res.status(health.status === 'ok' ? 200 : 503).json(health);
  } catch (e) {
    res.status(503).json({ status: 'down', error: e.message });
  }
});

// /api/health — same payload, referenced by Docker healthcheck
app.get('/api/health', async (req, res) => {
  try {
    const health = await buildHealthResponse();
    res.status(health.status === 'ok' ? 200 : 503).json(health);
  } catch (e) {
    res.status(503).json({ status: 'down', error: e.message });
  }
});

// ─── Frontend routing ─────────────────────────────────────────────────────────
app.get('*', (req, res) => {
  let filePath = req.path.startsWith('/') ? req.path.slice(1) : req.path;
  if (!filePath || filePath.endsWith('/')) filePath += 'index.html';
  if (!path.extname(filePath)) filePath += '.html';
  const fullPath = path.join(__dirname, 'public', filePath);
  res.sendFile(fullPath, err => {
    if (err) {
      res.status(404).sendFile(path.join(__dirname, 'public', '404.html'), e2 => {
        if (e2) res.status(404).send('Not found');
      });
    }
  });
});

// ─── Explicit 404 handler (catches unmatched routes after all middleware) ──────
app.use((req, res) => {
  res.status(404).sendFile(path.join(__dirname, 'public', '404.html'), err => {
    if (err) res.status(404).send('Not found');
  });
});

// ─── Global error handler ─────────────────────────────────────────────────────
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  console.error('[ERROR]', err.message, err.stack);
  if (err.code === 'LIMIT_FILE_SIZE') return res.status(413).json({ error: 'Файл слишком большой (макс. 10 МБ)' });
  if (err.message?.includes('Only images')) return res.status(400).json({ error: err.message });
  res.status(500).json({ error: process.env.NODE_ENV === 'production' ? 'Внутренняя ошибка сервера' : err.message });
});

// ─── Startup ──────────────────────────────────────────────────────────────────
async function start() {
  await initDatabase();

  botInstance = initBot(app);
  if (botInstance) apiRouter.setBot(botInstance);

  // ─── Task scheduler ───────────────────────────────────────────────────────────
  const scheduler = require('./services/scheduler');
  const { get: dbGetSched } = require('./database');
  scheduler.init({
    db: { run: require('./database').run },
    bot: botInstance?.instance,
    adminIds: process.env.ADMIN_TELEGRAM_IDS || ''
  });
  scheduler.start();

  const server = app.listen(PORT, () => {
    console.log(`\n🌐 Nevesty Models  →  http://localhost:${PORT}`);
    console.log(`🔐 Admin panel     →  http://localhost:${PORT}/admin/login.html`);
    console.log(`   Login: ${process.env.ADMIN_USERNAME || 'admin'} / [password from ADMIN_PASSWORD env var]`);
    console.log(`❤  Health check   →  http://localhost:${PORT}/health\n`);
  });

  // ─── WebSocket для real-time обновлений заявок ────────────────────────────────
  const wss = new WebSocketServer({ server, path: '/' });

  // Map: orderId → Set<ws>  and  phone → Set<ws>
  const wsByOrder = new Map();
  const wsByPhone = new Map();

  function wsSubscribeOrder(ws, orderId) {
    if (!wsByOrder.has(orderId)) wsByOrder.set(orderId, new Set());
    wsByOrder.get(orderId).add(ws);
    if (!ws._orderIds) ws._orderIds = new Set();
    ws._orderIds.add(orderId);
  }

  function wsSubscribePhone(ws, phone) {
    if (!wsByPhone.has(phone)) wsByPhone.set(phone, new Set());
    wsByPhone.get(phone).add(ws);
    ws._phone = phone;
  }

  function wsCleanup(ws) {
    if (ws._orderIds) {
      for (const id of ws._orderIds) {
        const set = wsByOrder.get(id);
        if (set) { set.delete(ws); if (!set.size) wsByOrder.delete(id); }
      }
    }
    if (ws._phone) {
      const set = wsByPhone.get(ws._phone);
      if (set) { set.delete(ws); if (!set.size) wsByPhone.delete(ws._phone); }
    }
  }

  function notifyOrderUpdate(orderId, status, phone) {
    const msg = JSON.stringify({ type: 'order_update', order_id: orderId, status });
    // Notify by orderId
    const byId = wsByOrder.get(orderId);
    if (byId) byId.forEach(ws => { try { if (ws.readyState === ws.OPEN) ws.send(msg); } catch (_) {} });
    // Notify by phone (cabinet)
    if (phone) {
      const byPhone = wsByPhone.get(phone);
      if (byPhone) byPhone.forEach(ws => { try { if (ws.readyState === ws.OPEN) ws.send(msg); } catch (_) {} });
    }
  }

  wss.on('connection', (ws) => {
    ws.isAlive = true;
    ws.on('pong', () => { ws.isAlive = true; });
    ws.on('close', () => wsCleanup(ws));
    ws.on('error', () => wsCleanup(ws));

    ws.on('message', async (rawMsg) => {
      try {
        const msg = JSON.parse(rawMsg);
        if (msg.type === 'subscribe') {
          if (msg.order_id) {
            const id = parseInt(msg.order_id);
            if (Number.isInteger(id) && id > 0) {
              wsSubscribeOrder(ws, id);
              try {
                const { get: dbGetLocal } = require('./database');
                const order = await dbGetLocal('SELECT status FROM orders WHERE id=?', [id]);
                if (order) {
                  ws.send(JSON.stringify({ type: 'subscribed', order_id: id, status: order.status }));
                }
              } catch (_) {}
            }
          } else if (msg.phone) {
            const phone = String(msg.phone).replace(/\D/g, '').slice(-10);
            if (phone.length === 10) {
              wsSubscribePhone(ws, phone);
              ws.send(JSON.stringify({ type: 'subscribed', phone }));
            }
          }
        } else if (msg.type === 'pong') {
          ws.isAlive = true;
        }
      } catch (_) {}
    });
  });

  // Heartbeat — ping every 30s, terminate dead connections
  const wsPingInterval = setInterval(() => {
    wss.clients.forEach(ws => {
      if (!ws.isAlive) { wsCleanup(ws); return ws.terminate(); }
      ws.isAlive = false;
      ws.ping();
    });
  }, 30000);

  wss.on('close', () => clearInterval(wsPingInterval));

  // ─── Memory alert (every 5 minutes) ──────────────────────────────────────────
  const memAlertInterval = setInterval(async () => {
    try {
      const memMb = Math.round(process.memoryUsage().heapUsed / 1024 / 1024);
      if (memMb > 500) {
        const adminIds = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').filter(Boolean);
        for (const id of adminIds) {
          botInstance?.instance?.sendMessage(id, `⚠️ Внимание: потребление памяти ${memMb} MB`).catch(() => {});
        }
      }
    } catch (_) {}
  }, 5 * 60 * 1000);

  // ─── Factory health check (every 30 minutes) ──────────────────────────────────
  const factoryCheckInterval = setInterval(async () => {
    try {
      const { get: dbGetLocal } = require('./database');
      const row = await dbGetLocal("SELECT value FROM bot_settings WHERE key = 'factory_last_cycle'");
      if (!row?.value) return;
      const lastCycleMs = new Date(row.value).getTime();
      const hoursAgo = (Date.now() - lastCycleMs) / (1000 * 60 * 60);
      if (hoursAgo >= 12) {
        const adminIds = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').filter(Boolean);
        const h = Math.round(hoursAgo);
        for (const id of adminIds) {
          botInstance?.instance?.sendMessage(id,
            `⚠️ AI Factory не запускался ${h} ч. Последний цикл: ${row.value.slice(0, 16).replace('T', ' ')}`
          ).catch(() => {});
        }
      }
    } catch (_) {}
  }, 30 * 60 * 1000);

  // Attach to app so api routes can use it
  app.set('wsServer', { notifyOrderUpdate });
  console.log('🔌 WebSocket server attached to HTTP server');

  // ─── Graceful shutdown ───────────────────────────────────────────────────
  const shutdown = async (signal) => {
    console.log(`\n${signal} received — shutting down gracefully…`);
    const forceTimer = setTimeout(() => { console.error('Forced shutdown after timeout.'); process.exit(1); }, 10000);
    try {
      if (botInstance?.instance?.stopPolling) {
        try { await botInstance.instance.stopPolling({ cancel: true }); console.log('Bot polling stopped.'); } catch (e) { console.warn('stopPolling:', e.message); }
      }
      clearInterval(wsPingInterval);
      clearInterval(memAlertInterval);
      clearInterval(factoryCheckInterval);
      try { require('./services/scheduler').stop(); } catch (_) {}
      await new Promise(resolve => wss.close(resolve));
      console.log('WebSocket server closed.');
      await new Promise(resolve => server.close(resolve));
      console.log('HTTP server closed.');
      if (closeDatabase) await closeDatabase();
      console.log('Database closed.');
      clearTimeout(forceTimer);
      process.exit(0);
    } catch (e) {
      console.error('Shutdown error:', e);
      process.exit(1);
    }
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('uncaughtException', err => { console.error('[UNCAUGHT]', err); });
  process.on('unhandledRejection', (reason) => { console.error('[UNHANDLED REJECTION]', reason); });
}

start().catch(err => { console.error('Startup error:', err); process.exit(1); });
