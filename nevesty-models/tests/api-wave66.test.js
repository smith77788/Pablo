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

// ─── 1. GET /api/admin/analytics/top-cities ───────────────────────────────────
describe('GET /api/admin/analytics/top-cities', () => {
  it('returns 401 without token', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid token', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/top-cities')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has cities array', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/top-cities')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.cities)).toBe(true);
  });

  it('each city entry has orders and unique_clients fields', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/top-cities')
      .set('Authorization', `Bearer ${adminToken}`);
    for (const entry of res.body.cities) {
      expect(typeof entry.orders).toBe('number');
      expect(typeof entry.unique_clients).toBe('number');
    }
  });

  it('handles empty data gracefully (cities array is empty or valid)', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/top-cities')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.cities)).toBe(true);
    expect(res.body.cities.length).toBeGreaterThanOrEqual(0);
  });
});

// ─── 2. GET /api/admin/settings/sections ──────────────────────────────────────
describe('GET /api/admin/settings/sections', () => {
  it('returns 401 without token', async () => {
    const res = await request(app).get('/api/admin/settings/sections');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid token', async () => {
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has sections object', async () => {
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.sections).toBe('object');
    expect(res.body.sections).not.toBeNull();
  });

  it('sections includes contacts', async () => {
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.sections).toHaveProperty('contacts');
  });

  it('sections includes catalog', async () => {
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.sections).toHaveProperty('catalog');
  });

  it('sections includes booking', async () => {
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.sections).toHaveProperty('booking');
  });

  it('sections includes reviews', async () => {
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.sections).toHaveProperty('reviews');
  });

  it('each section has a label string', async () => {
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    for (const key of Object.keys(res.body.sections)) {
      expect(typeof res.body.sections[key].label).toBe('string');
      expect(res.body.sections[key].label.length).toBeGreaterThan(0);
    }
  });

  it('each section has a settings object', async () => {
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    for (const key of Object.keys(res.body.sections)) {
      expect(typeof res.body.sections[key].settings).toBe('object');
    }
  });

  it('more than 3 sections total', async () => {
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(Object.keys(res.body.sections).length).toBeGreaterThan(3);
  });
});

// ─── 3. bot.js tech spec feature ──────────────────────────────────────────────
describe('bot.js tech spec feature', () => {
  const botPath = path.join(__dirname, '../bot.js');
  let botContent;

  beforeAll(() => {
    botContent = fs.readFileSync(botPath, 'utf8');
  });

  it('bot.js contains techspec_input state', () => {
    expect(botContent).toContain('techspec_input');
  });

  it('bot.js contains generateTechSpec function', () => {
    expect(botContent).toContain('generateTechSpec');
  });

  it('bot.js contains startTechSpec function', () => {
    expect(botContent).toContain('startTechSpec');
  });

  it('bot.js contains techspec_confirm_yes callback', () => {
    expect(botContent).toContain('techspec_confirm_yes');
  });

  it('bot.js contains Тех. задание button text', () => {
    expect(botContent).toContain('Тех. задание');
  });
});

// ─── 4. Security broadcast fix ────────────────────────────────────────────────
describe('bot.js broadcast security', () => {
  const botPath = path.join(__dirname, '../bot.js');
  let botContent;

  beforeAll(() => {
    botContent = fs.readFileSync(botPath, 'utf8');
  });

  it('bot.js truncates broadcast messages with .slice(0, 4096)', () => {
    expect(botContent).toContain('.slice(0, 4096)');
  });

  it('bot.js contains _sendOneBroadcastMsg function', () => {
    expect(botContent).toContain('_sendOneBroadcastMsg');
  });

  it('.slice(0, 4096) appears at least once in broadcast code', () => {
    const matches = (botContent.match(/\.slice\(0,\s*4096\)/g) || []);
    expect(matches.length).toBeGreaterThanOrEqual(1);
  });
});

// ─── 5. SalesDepartment Python ────────────────────────────────────────────────
describe('SalesDepartment Python agent', () => {
  const pyPath = path.join(__dirname, '../../factory/agents/sales_department.py');
  let pyContent;

  beforeAll(() => {
    if (fs.existsSync(pyPath)) {
      pyContent = fs.readFileSync(pyPath, 'utf8');
    }
  });

  it('factory/agents/sales_department.py exists', () => {
    expect(fs.existsSync(pyPath)).toBe(true);
  });

  it('contains class SalesDepartment', () => {
    expect(pyContent).toContain('class SalesDepartment');
  });

  it('contains qualify_lead method', () => {
    expect(pyContent).toContain('qualify_lead');
  });

  it('contains suggest_pricing method', () => {
    expect(pyContent).toContain('suggest_pricing');
  });
});
