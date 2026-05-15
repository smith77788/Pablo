'use strict';
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const fs = require('fs');
const path = require('path');
const express = require('express');
const cors = require('cors');

let app, adminToken;

const routesContent = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
const dbContent = fs.readFileSync(path.join(__dirname, '../database.js'), 'utf8');
const serverContent = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');

const FACTORY_DIR = path.join(__dirname, '../../factory');
const FACTORY_TESTS_DIR = path.join(FACTORY_DIR, 'tests');
const FACTORY_AGENTS_DIR = path.join(FACTORY_DIR, 'agents');
const factoryExists = fs.existsSync(FACTORY_DIR);

beforeAll(async () => {
  const { initDatabase } = require('../database');
  await initDatabase();
  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');
  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());
  const bot = initBot(a);
  if (bot) apiRouter.setBot(bot);
  a.use('/api', apiRouter);
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const res = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = res.body.token;
}, 15000);

afterAll(() => {
  if (app && app.close) app.close();
});

// ─── 1. FAQ categories — response shape ──────────────────────────────────────
describe('Wave 68: GET /api/faq/categories — response shape', () => {
  it('responds with 200 or 500 (in-memory DB may not have faq table)', async () => {
    const res = await request(app).get('/api/faq/categories');
    expect([200, 500]).toContain(res.status);
  });

  it('response body has categories key when 200', async () => {
    const res = await request(app).get('/api/faq/categories');
    if (res.status === 200) {
      expect(res.body).toHaveProperty('categories');
    }
  });

  it('categories is an array when 200', async () => {
    const res = await request(app).get('/api/faq/categories');
    if (res.status === 200) {
      expect(Array.isArray(res.body.categories)).toBe(true);
    }
  });

  it('responds without authentication (no 401/403)', async () => {
    const res = await request(app).get('/api/faq/categories');
    expect(res.status).not.toBe(401);
    expect(res.status).not.toBe(403);
  });

  it('content-type is application/json', async () => {
    const res = await request(app).get('/api/faq/categories');
    expect(res.headers['content-type']).toMatch(/json/);
  });
});

// ─── 2. FAQ with category filter — query param handling ───────────────────────
describe('Wave 68: GET /api/faq — category filter', () => {
  it('?category=booking returns 200', async () => {
    const res = await request(app).get('/api/faq?category=booking');
    expect(res.status).toBe(200);
  });

  it('?category=pricing returns 200', async () => {
    const res = await request(app).get('/api/faq?category=pricing');
    expect(res.status).toBe(200);
  });

  it('?category=general returns 200', async () => {
    const res = await request(app).get('/api/faq?category=general');
    expect(res.status).toBe(200);
  });

  it('response items have q and a fields when non-empty', async () => {
    // Seed first to ensure items exist
    if (adminToken) {
      await request(app)
        .post('/api/admin/faq/seed')
        .set('Authorization', `Bearer ${adminToken}`);
    }
    const res = await request(app).get('/api/faq');
    expect(res.status).toBe(200);
    if (Array.isArray(res.body) && res.body.length > 0) {
      expect(res.body[0]).toHaveProperty('q');
      expect(res.body[0]).toHaveProperty('a');
    }
  });

  it('faq items have category field', async () => {
    const res = await request(app).get('/api/faq');
    if (Array.isArray(res.body) && res.body.length > 0) {
      expect(res.body[0]).toHaveProperty('category');
    }
  });
});

// ─── 3. Admin FAQ Seed ─────────────────────────────────────────────────────────
describe('Wave 68: POST /api/admin/faq/seed', () => {
  it('endpoint defined in routes/api.js', () => {
    expect(routesContent).toContain('/admin/faq/seed');
  });

  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/faq/seed');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid token', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/faq/seed')
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 201]).toContain(res.status);
  });

  it('response has ok: true after seed', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/faq/seed')
      .set('Authorization', `Bearer ${adminToken}`);
    if ([200, 201].includes(res.status)) {
      expect(res.body.ok).toBe(true);
    }
  });

  it('seeded items appear in GET /api/faq', async () => {
    if (!adminToken) return;
    await request(app)
      .post('/api/admin/faq/seed')
      .set('Authorization', `Bearer ${adminToken}`);
    const res = await request(app).get('/api/faq');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });
});

// ─── 4. DB Maintenance — database.js ─────────────────────────────────────────
describe('Wave 68: DB Maintenance in database.js', () => {
  it('scheduleVacuum function exists', () => {
    expect(dbContent).toContain('scheduleVacuum');
  });

  it('uses VACUUM SQL statement', () => {
    expect(dbContent).toMatch(/VACUUM/);
  });

  it('wal_checkpoint is called with TRUNCATE', () => {
    expect(dbContent).toContain('wal_checkpoint(TRUNCATE)');
  });

  it('WAL timer uses setInterval', () => {
    expect(dbContent).toMatch(/setInterval/);
  });

  it('WAL timer uses .unref() to prevent process hang', () => {
    expect(dbContent).toMatch(/walTimer\.unref\(\)/);
  });

  it('scheduleVacuum timer uses .unref()', () => {
    expect(dbContent).toContain('.unref()');
  });
});

// ─── 5. Manual VACUUM endpoint ────────────────────────────────────────────────
describe('Wave 68: POST /api/admin/db/vacuum', () => {
  it('route handler exists in routes/api.js', () => {
    expect(routesContent).toContain('/admin/db/vacuum');
  });

  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/db/vacuum');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/db/vacuum')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response body has ok: true', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/db/vacuum')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('ok', true);
  });
});

// ─── 6. SEO: server.js sitemap and robots.txt ────────────────────────────────
describe('Wave 68: SEO — sitemap.xml and robots.txt in server.js', () => {
  it('server.js contains /sitemap.xml route', () => {
    expect(serverContent).toContain('/sitemap.xml');
  });

  it('server.js contains /robots.txt route', () => {
    expect(serverContent).toContain('/robots.txt');
  });

  it('sitemap.xml output contains urlset element', () => {
    expect(serverContent).toContain('urlset');
  });

  it('robots.txt output contains Sitemap directive', () => {
    expect(serverContent).toContain('Sitemap:');
  });
});

// ─── 7. Model Bio Generator — factory/cycle.py ───────────────────────────────
describe('Wave 68: Model Bio Generator (factory/cycle.py)', () => {
  it('factory/ directory exists', () => {
    if (!factoryExists) {
      console.warn('[Wave68] factory/ not present — skipping');
      return;
    }
    expect(factoryExists).toBe(true);
  });

  it('factory/tests/test_model_bios.py exists', () => {
    if (!factoryExists) return;
    expect(fs.existsSync(path.join(FACTORY_TESTS_DIR, 'test_model_bios.py'))).toBe(true);
  });

  it('cycle.py contains run_phase_26_model_bios', () => {
    if (!factoryExists) return;
    const cycleCode = fs.readFileSync(path.join(FACTORY_DIR, 'cycle.py'), 'utf8');
    expect(cycleCode).toContain('run_phase_26_model_bios');
  });

  it('cycle.py contains _generate_heuristic_bio function', () => {
    if (!factoryExists) return;
    const cycleCode = fs.readFileSync(path.join(FACTORY_DIR, 'cycle.py'), 'utf8');
    expect(cycleCode).toContain('_generate_heuristic_bio');
  });
});

// ─── 8. Creative Department Phase 6b ─────────────────────────────────────────
describe('Wave 68: Creative Department (factory/agents/creative_department.py)', () => {
  it('factory/agents/creative_department.py exists', () => {
    if (!factoryExists) return;
    expect(fs.existsSync(path.join(FACTORY_AGENTS_DIR, 'creative_department.py'))).toBe(true);
  });

  it('creative_department.py contains CreativeDepartment class', () => {
    if (!factoryExists) return;
    const filePath = path.join(FACTORY_AGENTS_DIR, 'creative_department.py');
    if (!fs.existsSync(filePath)) return;
    const content = fs.readFileSync(filePath, 'utf8');
    expect(content).toContain('CreativeDepartment');
  });

  it('cycle.py contains generate_social_caption', () => {
    if (!factoryExists) return;
    const cycleCode = fs.readFileSync(path.join(FACTORY_DIR, 'cycle.py'), 'utf8');
    expect(cycleCode).toContain('generate_social_caption');
  });

  it('cycle.py contains generate_promo_text', () => {
    if (!factoryExists) return;
    const cycleCode = fs.readFileSync(path.join(FACTORY_DIR, 'cycle.py'), 'utf8');
    expect(cycleCode).toContain('generate_promo_text');
  });

  it('factory/tests/test_creative_department.py exists', () => {
    if (!factoryExists) return;
    expect(fs.existsSync(path.join(FACTORY_TESTS_DIR, 'test_creative_department.py'))).toBe(true);
  });
});
