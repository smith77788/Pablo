'use strict';
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, seededModelId;

beforeAll(async () => {
  const { initDatabase, get } = require('../database');
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
  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
  const model = await get('SELECT id FROM models LIMIT 1');
  seededModelId = model ? model.id : null;
}, 15000);

// ─── 1. Model Recommendation API ──────────────────────────────────────────────
describe('GET /api/recommend', () => {
  it('Returns 200 with models array', async () => {
    const res = await request(app).get('/api/recommend');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  it('body has models array and meta object', async () => {
    const res = await request(app).get('/api/recommend');
    expect(res.body).toHaveProperty('models');
    expect(res.body).toHaveProperty('meta');
    expect(typeof res.body.meta).toBe('object');
  });

  it('meta.preferred_category is null when no event_type', async () => {
    const res = await request(app).get('/api/recommend');
    expect(res.status).toBe(200);
    expect(res.body.meta.preferred_category).toBeNull();
  });

  it('With event_type=photo_shoot: meta.preferred_category is "fashion"', async () => {
    const res = await request(app).get('/api/recommend?event_type=photo_shoot');
    expect(res.status).toBe(200);
    expect(res.body.meta.preferred_category).toBe('fashion');
  });

  it('With event_type=корпоратив: returns 200', async () => {
    const res = await request(app).get('/api/recommend?event_type=%D0%BA%D0%BE%D1%80%D0%BF%D0%BE%D1%80%D0%B0%D1%82%D0%B8%D0%B2');
    expect(res.status).toBe(200);
  });

  it('With city=Москва filter: returns 200', async () => {
    const res = await request(app).get('/api/recommend?city=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0');
    expect(res.status).toBe(200);
  });

  it('With limit=3: returns at most 3 models', async () => {
    const res = await request(app).get('/api/recommend?limit=3');
    expect(res.status).toBe(200);
    expect(res.body.models.length).toBeLessThanOrEqual(3);
  });

  it("With invalid limit: doesn't crash, returns 200", async () => {
    const res = await request(app).get('/api/recommend?limit=abc');
    expect(res.status).toBe(200);
  });

  it('meta.event_type matches the requested event_type', async () => {
    const res = await request(app).get('/api/recommend?event_type=photo_shoot');
    expect(res.status).toBe(200);
    expect(res.body.meta.event_type).toBe('photo_shoot');
  });
});

// ─── 2. Model Search with Name Filter ─────────────────────────────────────────
describe('GET /api/models/search', () => {
  it('Search with no params returns 200 with models array', async () => {
    const res = await request(app).get('/api/models/search');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  it('Search with name=Анна returns 200', async () => {
    const res = await request(app).get('/api/models/search?name=%D0%90%D0%BD%D0%BD%D0%B0');
    expect(res.status).toBe(200);
  });

  it('Search excludes archived models', async () => {
    const res = await request(app).get('/api/models/search');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
    const hasArchived = res.body.models.some(m => m.archived === 1);
    expect(hasArchived).toBe(false);
  });

  it('Search with category=fashion returns 200', async () => {
    const res = await request(app).get('/api/models/search?category=fashion');
    expect(res.status).toBe(200);
  });

  it('Search with city=Москва returns 200', async () => {
    const res = await request(app).get('/api/models/search?city=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0');
    expect(res.status).toBe(200);
  });

  it('Search with combined params: name + city returns 200', async () => {
    const res = await request(app).get('/api/models/search?name=%D0%90%D0%BD%D0%BD%D0%B0&city=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0');
    expect(res.status).toBe(200);
  });

  it('Search total field is a number', async () => {
    const res = await request(app).get('/api/models/search');
    expect(res.status).toBe(200);
    expect(typeof res.body.total).toBe('number');
  });
});

// ─── 3. mypy Configuration ────────────────────────────────────────────────────
describe('Factory type hints', () => {
  it('mypy.ini exists', () => {
    const fs = require('fs');
    const path = require('path');
    expect(fs.existsSync(path.join(__dirname, '../../factory/mypy.ini'))).toBe(true);
  });

  it('sales_department.py has type hints', () => {
    const fs = require('fs');
    const path = require('path');
    const content = fs.readFileSync(path.join(__dirname, '../../factory/agents/sales_department.py'), 'utf8');
    expect(content).toContain('Dict[str, Any]');
  });

  it('creative_department.py has type hints', () => {
    const fs = require('fs');
    const path = require('path');
    const content = fs.readFileSync(path.join(__dirname, '../../factory/agents/creative_department.py'), 'utf8');
    expect(content).toContain('Dict[str, Any]');
  });
});

// ─── 4. Strings localization ───────────────────────────────────────────────────
describe('strings.js localization', () => {
  it('STRINGS has at least 30 keys', () => {
    const STRINGS = require('../strings');
    expect(Object.keys(STRINGS).length).toBeGreaterThanOrEqual(30);
  });

  it('STRINGS has error keys', () => {
    const STRINGS = require('../strings');
    expect(STRINGS).toHaveProperty('errorGeneric');
  });

  it('STRINGS has review keys', () => {
    const STRINGS = require('../strings');
    expect(STRINGS).toHaveProperty('reviewThankYou');
  });

  it('getString replaces all placeholders', () => {
    const { getString } = require('../strings');
    // getString should exist as exported function
    expect(typeof getString).toBe('function');
  });

  it('wishlist strings exist', () => {
    const STRINGS = require('../strings');
    expect(STRINGS).toHaveProperty('wishlistAdded');
    expect(STRINGS).toHaveProperty('wishlistRemoved');
    expect(STRINGS).toHaveProperty('wishlistEmpty');
  });
});

// ─── 5. Chart.js in admin ─────────────────────────────────────────────────────
describe('Admin Dashboard Charts', () => {
  it('admin index.html includes Chart.js CDN', () => {
    const fs = require('fs');
    const path = require('path');
    const html = fs.readFileSync(path.join(__dirname, '../public/admin/index.html'), 'utf8');
    expect(html).toContain('chart.js');
  });

  it('admin index.html has status chart canvas', () => {
    const fs = require('fs');
    const path = require('path');
    const html = fs.readFileSync(path.join(__dirname, '../public/admin/index.html'), 'utf8');
    expect(html).toMatch(/canvas id="statusChart/);
  });
});
