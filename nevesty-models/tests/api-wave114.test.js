'use strict';
// Wave 114: Static files, robots.txt, catalog sorting, security headers, settings public leak

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-wave114-minimum-32chars!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.BOT_TOKEN = '123:test';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');
const path = require('path');

let app;

beforeAll(async () => {
  const { initDatabase, run } = require('../database');
  await initDatabase();

  // Seed a few models for sorting tests
  await run(
    `INSERT INTO models (name, age, height, category, available, featured, archived, order_count)
     VALUES (?,?,?,?,?,?,?,?)`,
    ['Zara Smith', 24, 175, 'fashion', 1, 0, 0, 5]
  );
  await run(
    `INSERT INTO models (name, age, height, category, available, featured, archived, order_count)
     VALUES (?,?,?,?,?,?,?,?)`,
    ['Anna Bell', 26, 168, 'events', 1, 1, 0, 12]
  );
  await run(
    `INSERT INTO models (name, age, height, category, available, featured, archived, order_count)
     VALUES (?,?,?,?,?,?,?,?)`,
    ['Maria Canova', 22, 180, 'fashion', 1, 0, 0, 3]
  );

  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');

  const a = express();

  // Add helmet-like security headers (mirrors server.js)
  try {
    const helmet = require('helmet');
    a.use(
      helmet({
        contentSecurityPolicy: false,
        crossOriginEmbedderPolicy: false,
      })
    );
    a.use(helmet.noSniff());
    a.use(helmet.frameguard({ action: 'sameorigin' }));
  } catch (_) {}

  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());

  // robots.txt (mirrors server.js)
  a.get('/robots.txt', (req, res) => {
    res.type('text/plain');
    res.send('User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: https://example.com/sitemap.xml');
  });

  // sitemap.xml (mirrors server.js)
  a.get('/sitemap.xml', async (req, res) => {
    try {
      const { query: dbQuery } = require('../database');
      await dbQuery('SELECT id FROM models WHERE available=1 AND archived=0 LIMIT 1');
      res.header('Content-Type', 'application/xml');
      res.send(
        '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
      );
    } catch (e) {
      res.status(500).send('<?xml version="1.0"?><urlset/>');
    }
  });

  // Static files from /public (mirrors server.js)
  a.use(
    express.static(path.join(__dirname, '..', 'public'), {
      maxAge: 0,
      etag: true,
    })
  );

  // Health endpoint (mirrors server.js /api/health)
  a.get('/api/health', async (req, res) => {
    try {
      const { get: dbGet } = require('../database');
      let dbStatus = 'ok';
      try {
        await dbGet('SELECT 1');
      } catch (_) {
        dbStatus = 'error';
      }
      res.json({ status: 'ok', db: dbStatus });
    } catch (e) {
      res.status(503).json({ status: 'down', error: e.message });
    }
  });

  const bot = initBot(a);
  if (bot && apiRouter.setBot) apiRouter.setBot(bot);

  a.use('/api', apiRouter);

  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;
}, 20000);

// ── 1. Static files serving ───────────────────────────────────────────────────

describe('Static files — GET /', () => {
  it('GET / returns 200 (index.html)', async () => {
    const res = await request(app).get('/');
    expect(res.status).toBe(200);
  });

  it('GET / returns HTML content', async () => {
    const res = await request(app).get('/');
    expect(res.headers['content-type']).toMatch(/html/i);
  });

  it('GET /catalog.html returns 200', async () => {
    const res = await request(app).get('/catalog.html');
    expect(res.status).toBe(200);
  });

  it('GET /catalog.html returns HTML content', async () => {
    const res = await request(app).get('/catalog.html');
    expect(res.headers['content-type']).toMatch(/html/i);
  });

  it('GET /404.html returns 200 (static file exists)', async () => {
    const res = await request(app).get('/404.html');
    expect(res.status).toBe(200);
  });
});

// ── 2. robots.txt ─────────────────────────────────────────────────────────────

describe('robots.txt — GET /robots.txt', () => {
  it('returns 200', async () => {
    const res = await request(app).get('/robots.txt');
    expect(res.status).toBe(200);
  });

  it('Content-Type is text/plain', async () => {
    const res = await request(app).get('/robots.txt');
    expect(res.headers['content-type']).toMatch(/text\/plain/i);
  });

  it("body contains 'User-agent'", async () => {
    const res = await request(app).get('/robots.txt');
    expect(res.text).toMatch(/User-agent/i);
  });

  it("body contains 'Disallow: /api/'", async () => {
    const res = await request(app).get('/robots.txt');
    expect(res.text).toMatch(/Disallow:\s*\/api\//i);
  });
});

// ── 3. sitemap.xml ────────────────────────────────────────────────────────────

describe('sitemap.xml — GET /sitemap.xml', () => {
  it('returns 200', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.status).toBe(200);
  });

  it('Content-Type is xml', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.headers['content-type']).toMatch(/xml/i);
  });

  it('body contains urlset element', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.text).toMatch(/urlset/i);
  });
});

// ── 4. Catalog API with sorting ───────────────────────────────────────────────

describe('Catalog API — GET /api/models with sorting', () => {
  it('GET /api/models?sort=name returns 200 with array', async () => {
    const res = await request(app).get('/api/models?sort=name');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('sort=name returns models in alphabetical order', async () => {
    const res = await request(app).get('/api/models?sort=name');
    expect(res.status).toBe(200);
    for (let i = 1; i < res.body.length; i++) {
      expect(res.body[i].name.localeCompare(res.body[i - 1].name)).toBeGreaterThanOrEqual(0);
    }
  });

  it('GET /api/models?sort=featured returns 200', async () => {
    const res = await request(app).get('/api/models?sort=featured');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('GET /api/models?sort=orders returns 200', async () => {
    const res = await request(app).get('/api/models?sort=orders');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('GET /api/models?sort=invalid_sort returns 200 (not 500)', async () => {
    const res = await request(app).get('/api/models?sort=invalid_sort');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('each model in sorted response has required fields', async () => {
    const res = await request(app).get('/api/models?sort=name');
    expect(res.status).toBe(200);
    res.body.forEach(m => {
      expect(m).toHaveProperty('id');
      expect(m).toHaveProperty('name');
    });
  });
});

// ── 5. Security headers ───────────────────────────────────────────────────────

describe('Security headers', () => {
  it('GET /api/health has X-Content-Type-Options: nosniff', async () => {
    const res = await request(app).get('/api/health');
    expect(res.headers['x-content-type-options']).toBe('nosniff');
  });

  it('GET / has X-Frame-Options header (clickjacking protection)', async () => {
    const res = await request(app).get('/');
    expect(res.headers['x-frame-options']).toBeDefined();
  });

  it('GET /api/models has X-Content-Type-Options: nosniff', async () => {
    const res = await request(app).get('/api/models');
    expect(res.headers['x-content-type-options']).toBe('nosniff');
  });
});

// ── 6. Settings public — no secret leak ───────────────────────────────────────

describe('Settings public — GET /api/settings/public', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
  });

  it('does not return BOT_TOKEN in body', async () => {
    const res = await request(app).get('/api/settings/public');
    const body = JSON.stringify(res.body);
    expect(body).not.toMatch(/BOT_TOKEN/i);
    // also check value itself is not present
    expect(body).not.toContain('123:test');
  });

  it('does not return JWT_SECRET in body', async () => {
    const res = await request(app).get('/api/settings/public');
    const body = JSON.stringify(res.body);
    expect(body).not.toMatch(/JWT_SECRET/i);
    expect(body).not.toContain('test-secret-wave114');
  });

  it('does not return ADMIN_PASSWORD in body', async () => {
    const res = await request(app).get('/api/settings/public');
    const body = JSON.stringify(res.body);
    expect(body).not.toMatch(/ADMIN_PASSWORD/i);
    expect(body).not.toContain('admin123');
  });
});

// ── 7. Health endpoint ────────────────────────────────────────────────────────

describe('Health endpoint — GET /api/health', () => {
  it('returns 200', async () => {
    const res = await request(app).get('/api/health');
    expect(res.status).toBe(200);
  });

  it('response contains status field', async () => {
    const res = await request(app).get('/api/health');
    expect(res.body).toHaveProperty('status');
  });

  it('response contains db field', async () => {
    const res = await request(app).get('/api/health');
    expect(res.body).toHaveProperty('db');
  });

  it('status field is a string', async () => {
    const res = await request(app).get('/api/health');
    expect(typeof res.body.status).toBe('string');
  });
});
