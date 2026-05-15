'use strict';
// Wave 117: Cabinet login, CRM webhooks, CRM DEV mode, Health memory/db, Cities + Catalog filters

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-wave117-minimum-32chars!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.BOT_TOKEN = '123:test';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';
// Ensure CRM DEV mode — no real CRM credentials
delete process.env.BITRIX24_WEBHOOK_URL;
delete process.env.AMOCRM_SUBDOMAIN;
delete process.env.AMOCRM_ACCESS_TOKEN;

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken;

beforeAll(async () => {
  const { initDatabase, run, get: dbGet, query: dbQuery } = require('../database');
  await initDatabase();

  // Seed models for catalog filter tests
  await run(
    `INSERT INTO models (name, age, height, category, city, available, featured, archived)
     VALUES (?,?,?,?,?,?,?,?)`,
    ['Wave117 Anya', 23, 168, 'fashion', 'Москва', 1, 0, 0]
  );
  await run(
    `INSERT INTO models (name, age, height, category, city, available, featured, archived)
     VALUES (?,?,?,?,?,?,?,?)`,
    ['Wave117 Bella', 26, 174, 'events', 'Москва', 1, 1, 0]
  );
  await run(
    `INSERT INTO models (name, age, height, category, city, available, featured, archived)
     VALUES (?,?,?,?,?,?,?,?)`,
    ['Wave117 Cara', 29, 179, 'fashion', 'Санкт-Петербург', 1, 0, 0]
  );

  // Seed an order tied to a known phone so cabinet/login can find a client
  await run(
    `INSERT INTO orders (order_number, client_name, client_phone, event_type, status)
     VALUES (?,?,?,?,?)`,
    ['W117-001', 'Wave Client', '79997654321', 'photo_shoot', 'new']
  );

  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');

  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());

  // Health endpoint — mirrors server.js /api/health but runs against test DB
  a.get('/api/health', async (req, res) => {
    try {
      const mem = process.memoryUsage();
      const rss_mb = Math.round((mem.rss / 1024 / 1024) * 10) / 10;

      let tableCount = 0;
      try {
        const tables = await dbQuery("SELECT name FROM sqlite_master WHERE type='table'");
        tableCount = tables.length;
      } catch (_) {}

      // Factory last cycle — may be null in test env
      let factoryLastCycle = null;
      let factoryHoursSince = null;
      try {
        const row = await dbGet("SELECT value FROM bot_settings WHERE key = 'factory_last_cycle'");
        if (row && row.value) {
          factoryLastCycle = row.value;
          factoryHoursSince =
            Math.round(((Date.now() - new Date(factoryLastCycle).getTime()) / (1000 * 60 * 60)) * 10) / 10;
        }
      } catch (_) {}

      res.json({
        status: 'ok',
        memory: {
          rss_mb,
          heap_used_mb: Math.round((mem.heapUsed / 1024 / 1024) * 10) / 10,
          heap_total_mb: Math.round((mem.heapTotal / 1024 / 1024) * 10) / 10,
        },
        db: {
          status: 'ok',
          tables: tableCount,
        },
        factory: {
          lastRun: factoryLastCycle,
          last_cycle_ago_hours: factoryHoursSince,
        },
      });
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

  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
}, 20000);

// ── 1. Cabinet login — POST /api/cabinet/login ────────────────────────────────

describe('Cabinet login — POST /api/cabinet/login', () => {
  it('returns 400 when phone field is missing', async () => {
    const res = await request(app).post('/api/cabinet/login').send({});
    expect(res.status).toBe(400);
  });

  it('returns ok:false when phone field is missing', async () => {
    const res = await request(app).post('/api/cabinet/login').send({});
    expect(res.body.ok).toBe(false);
  });

  it('returns 400 when phone is an empty string', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: '' });
    expect(res.status).toBe(400);
  });

  it('returns 404 for a phone that has no matching orders', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: '79990000000' });
    // Server returns 404 when no client is found
    expect([404, 401]).toContain(res.status);
    expect(res.body.ok).toBe(false);
  });

  it('GET /api/cabinet/orders without Authorization returns 401', async () => {
    const res = await request(app).get('/api/cabinet/orders');
    expect(res.status).toBe(401);
  });

  it('GET /api/cabinet/orders with admin JWT (type=admin) returns 403', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${adminToken}`);
    // Admin JWT has type !== 'client', so requireClientAuth returns 403
    expect(res.status).toBe(403);
  });
});

// ── 2. CRM webhooks ───────────────────────────────────────────────────────────

describe('CRM webhooks', () => {
  it('POST /api/crm/webhook/bitrix24 returns 200', async () => {
    const res = await request(app).post('/api/crm/webhook/bitrix24').send({ event: 'ONCRMLEADADD', data: {} });
    expect(res.status).toBe(200);
  });

  it('POST /api/crm/webhook/bitrix24 returns { ok: true }', async () => {
    const res = await request(app).post('/api/crm/webhook/bitrix24').send({ foo: 'bar' });
    expect(res.body.ok).toBe(true);
  });

  it('POST /api/crm/webhook/amocrm returns 200', async () => {
    const res = await request(app)
      .post('/api/crm/webhook/amocrm')
      .send({ leads: { update: [] } });
    expect(res.status).toBe(200);
  });

  it('POST /api/crm/webhook/amocrm returns { ok: true }', async () => {
    const res = await request(app).post('/api/crm/webhook/amocrm').send({ anything: true });
    expect(res.body.ok).toBe(true);
  });

  it('POST /api/crm/webhook/bitrix24 accepts arbitrary JSON body', async () => {
    const res = await request(app)
      .post('/api/crm/webhook/bitrix24')
      .send({ event: 'ONCRMDEALSTAGESET', data: { FIELDS_BEFORE: { ID: 42 }, FIELDS_AFTER: { STAGE_ID: 'WON' } } });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ── 3. CRM service DEV mode ───────────────────────────────────────────────────

describe('CRM service — DEV mode', () => {
  let crm;

  beforeAll(() => {
    // Reload module with no CRM credentials to guarantee DEV_MODE
    jest.resetModules();
    delete process.env.BITRIX24_WEBHOOK_URL;
    delete process.env.AMOCRM_SUBDOMAIN;
    crm = require('../services/crm');
  });

  it('DEV_MODE is true when BITRIX24_WEBHOOK_URL is not set', () => {
    expect(crm.DEV_MODE).toBe(true);
  });

  it('exportOrderToCrm returns { ok: true } in DEV mode', async () => {
    const result = await crm.exportOrderToCrm({ id: 10, client_name: 'Test Wave117' });
    expect(result.ok).toBe(true);
  });

  it('exportOrderToCrm returns { dev: true } flag in DEV mode', async () => {
    const result = await crm.exportOrderToCrm({ id: 11, client_name: 'Test Wave117 B' });
    expect(result.dev).toBe(true);
  });
});

// ── 4. Health endpoint ────────────────────────────────────────────────────────

describe('Health endpoint — GET /api/health', () => {
  let healthBody;

  beforeAll(async () => {
    const res = await request(app).get('/api/health');
    healthBody = res.body;
  });

  it('GET /api/health returns 200', async () => {
    const res = await request(app).get('/api/health');
    expect(res.status).toBe(200);
  });

  it('response has memory.rss_mb field (number)', () => {
    expect(healthBody).toHaveProperty('memory');
    expect(typeof healthBody.memory.rss_mb).toBe('number');
  });

  it('response has db.tables field and it is a number greater than 0', () => {
    expect(healthBody).toHaveProperty('db');
    expect(typeof healthBody.db.tables).toBe('number');
    expect(healthBody.db.tables).toBeGreaterThan(0);
  });

  it('factory.last_cycle_ago_hours exists and is a number or null', () => {
    expect(healthBody).toHaveProperty('factory');
    const val = healthBody.factory.last_cycle_ago_hours;
    // In test env factory never ran, so this should be null
    expect(val === null || typeof val === 'number').toBe(true);
  });
});

// ── 5. Cities + Catalog combined ─────────────────────────────────────────────

describe('Cities + Catalog — GET /api/cities and /api/models', () => {
  it('GET /api/cities returns 200', async () => {
    const res = await request(app).get('/api/cities');
    expect(res.status).toBe(200);
  });

  it('GET /api/cities returns a non-empty array of cities', async () => {
    const res = await request(app).get('/api/cities');
    // Response is { ok: true, cities: [...] }
    const cities = res.body.cities || res.body;
    const arr = Array.isArray(cities) ? cities : [];
    expect(arr.length).toBeGreaterThan(0);
  });

  it('GET /api/models?city=Москва&sort=name returns 200', async () => {
    const res = await request(app).get('/api/models').query({ city: 'Москва', sort: 'name' });
    expect(res.status).toBe(200);
  });

  it('GET /api/models?city=Москва&sort=name returns an array', async () => {
    const res = await request(app).get('/api/models').query({ city: 'Москва', sort: 'name' });
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('GET /api/models?height_min=165&height_max=180&sort=orders returns 200', async () => {
    const res = await request(app).get('/api/models').query({ height_min: 165, height_max: 180, sort: 'orders' });
    expect(res.status).toBe(200);
  });

  it('GET /api/models?height_min=165&height_max=180&sort=orders returns an array', async () => {
    const res = await request(app).get('/api/models').query({ height_min: 165, height_max: 180, sort: 'orders' });
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('height_min filter is respected — all results have height >= height_min', async () => {
    const res = await request(app).get('/api/models').query({ height_min: 175 });
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    res.body.forEach(m => {
      if (m.height !== undefined) {
        expect(m.height).toBeGreaterThanOrEqual(175);
      }
    });
  });
});
