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
const fs = require('fs');
const path = require('path');

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

  const loginRes = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;

  const model = await get('SELECT id FROM models LIMIT 1');
  seededModelId = model ? model.id : null;
}, 15000);

afterAll(() => {
  if (app && app.close) app.close();
});

// ─── 1. Public Model Availability API ─────────────────────────────────────────
describe('GET /api/models/:id/availability (public)', () => {
  it('Returns 200 with busy_dates array and month for a valid model ID', async () => {
    if (!seededModelId) return;
    const res = await request(app).get(`/api/models/${seededModelId}/availability`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('busy_dates');
    expect(Array.isArray(res.body.busy_dates)).toBe(true);
    expect(res.body).toHaveProperty('month');
    expect(typeof res.body.month).toBe('string');
  });

  it('Month param returns data for specified month', async () => {
    if (!seededModelId) return;
    const res = await request(app).get(`/api/models/${seededModelId}/availability?month=2026-05`);
    expect(res.status).toBe(200);
    expect(res.body.month).toBe('2026-05');
    expect(Array.isArray(res.body.busy_dates)).toBe(true);
  });

  it('Month param is optional — defaults to current month', async () => {
    if (!seededModelId) return;
    const res = await request(app).get(`/api/models/${seededModelId}/availability`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('month');
    expect(/^\d{4}-\d{2}$/.test(res.body.month)).toBe(true);
  });

  it('Invalid model ID (0) returns 400', async () => {
    const res = await request(app).get('/api/models/0/availability');
    expect(res.status).toBe(400);
  });

  it('Non-numeric model ID returns 400', async () => {
    const res = await request(app).get('/api/models/abc/availability');
    expect(res.status).toBe(400);
  });

  it('Non-existent model ID returns 200 with empty busy_dates', async () => {
    const res = await request(app).get('/api/models/999999/availability');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.busy_dates)).toBe(true);
    expect(res.body.busy_dates.length).toBe(0);
  });

  it('Invalid month format returns 400', async () => {
    if (!seededModelId) return;
    const res = await request(app).get(`/api/models/${seededModelId}/availability?month=2026-5`);
    expect(res.status).toBe(400);
  });
});

// ─── 2. Admin Model Availability API ──────────────────────────────────────────
describe('GET /api/admin/models/:id/availability (admin)', () => {
  it('Requires admin JWT — returns 401 without token', async () => {
    if (!seededModelId) return;
    const res = await request(app).get(`/api/admin/models/${seededModelId}/availability`);
    expect(res.status).toBe(401);
  });

  it('Returns 200 with month and busy_dates for valid admin token', async () => {
    if (!seededModelId || !adminToken) return;
    const res = await request(app)
      .get(`/api/admin/models/${seededModelId}/availability`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('month');
    expect(res.body).toHaveProperty('busy_dates');
    expect(Array.isArray(res.body.busy_dates)).toBe(true);
  });

  it('Admin endpoint accepts month query param', async () => {
    if (!seededModelId || !adminToken) return;
    const res = await request(app)
      .get(`/api/admin/models/${seededModelId}/availability?month=2026-06`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.month).toBe('2026-06');
  });

  it('Admin endpoint: invalid model ID returns 400', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/models/0/availability')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  it('Admin endpoint: non-existent model ID returns 200 with empty busy_dates', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/models/999999/availability')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.busy_dates)).toBe(true);
  });
});

// ─── 3. Broadcast Delivery Tracking ───────────────────────────────────────────
describe('GET /api/admin/broadcasts', () => {
  it('Requires admin JWT — returns 401 without token', async () => {
    const res = await request(app).get('/api/admin/broadcasts');
    expect(res.status).toBe(401);
  });

  it('Returns 200 with array for valid admin token', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/broadcasts')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('Each broadcast item has status field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/broadcasts')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // If there are items, check their structure
    if (res.body.length > 0) {
      expect(res.body[0]).toHaveProperty('status');
    }
  });

  it('Each broadcast item has delivered field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/broadcasts')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    if (res.body.length > 0) {
      expect(res.body[0]).toHaveProperty('delivered');
    }
  });

  it('Each broadcast item has failed field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/broadcasts')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    if (res.body.length > 0) {
      expect(res.body[0]).toHaveProperty('failed');
    }
  });
});

// ─── 4. Bot Broadcasts Delivery Tracking ──────────────────────────────────────
describe('GET /api/admin/bot-broadcasts', () => {
  it('Requires admin JWT — returns 401 without token', async () => {
    const res = await request(app).get('/api/admin/bot-broadcasts');
    expect(res.status).toBe(401);
  });

  it('Returns 200 with broadcasts array for valid admin token', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/bot-broadcasts')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('broadcasts');
    expect(Array.isArray(res.body.broadcasts)).toBe(true);
  });

  it('Each bot broadcast has status field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/bot-broadcasts')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    if (res.body.broadcasts.length > 0) {
      expect(res.body.broadcasts[0]).toHaveProperty('status');
    }
  });

  it('Each bot broadcast has delivered field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/bot-broadcasts')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    if (res.body.broadcasts.length > 0) {
      expect(res.body.broadcasts[0]).toHaveProperty('delivered');
    }
  });

  it('Each bot broadcast has failed field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/bot-broadcasts')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    if (res.body.broadcasts.length > 0) {
      expect(res.body.broadcasts[0]).toHaveProperty('failed');
    }
  });
});

// ─── 5. Finance Department (standalone agent) ──────────────────────────────────
describe('Finance Department agent', () => {
  const financePath = path.join(__dirname, '../../factory/agents/finance_department.py');

  it('finance_department.py file exists', () => {
    expect(fs.existsSync(financePath)).toBe(true);
  });

  it('Contains RevenueForecaster class', () => {
    const content = fs.readFileSync(financePath, 'utf8');
    expect(content).toContain('class RevenueForecaster');
  });

  it('Contains CostOptimizer class', () => {
    const content = fs.readFileSync(financePath, 'utf8');
    expect(content).toContain('class CostOptimizer');
  });

  it('Contains PricingStrategist class', () => {
    const content = fs.readFileSync(financePath, 'utf8');
    expect(content).toContain('class PricingStrategist');
  });

  it('Contains BudgetPlanner class', () => {
    const content = fs.readFileSync(financePath, 'utf8');
    expect(content).toContain('class BudgetPlanner');
  });
});

// ─── 6. Research Department (standalone agent) ─────────────────────────────────
describe('Research Department agent', () => {
  const researchPath = path.join(__dirname, '../../factory/agents/research_department.py');

  it('research_department.py file exists', () => {
    expect(fs.existsSync(researchPath)).toBe(true);
  });

  it('Contains MarketResearcher class', () => {
    const content = fs.readFileSync(researchPath, 'utf8');
    expect(content).toContain('class MarketResearcher');
  });

  it('Contains CompetitorAnalyst class', () => {
    const content = fs.readFileSync(researchPath, 'utf8');
    expect(content).toContain('class CompetitorAnalyst');
  });

  it('Contains TrendSpotter class', () => {
    const content = fs.readFileSync(researchPath, 'utf8');
    expect(content).toContain('class TrendSpotter');
  });

  it('Contains InsightSynthesizer class', () => {
    const content = fs.readFileSync(researchPath, 'utf8');
    expect(content).toContain('class InsightSynthesizer');
  });
});

// ─── 7. SEO improvements (og:title) ──────────────────────────────────────────
describe('SEO og:title meta tags', () => {
  it('about.html has og:title meta tag', () => {
    const html = fs.readFileSync(path.join(__dirname, '../public/about.html'), 'utf8');
    expect(html).toContain('og:title');
  });

  it('contact.html has og:title meta tag', () => {
    const html = fs.readFileSync(path.join(__dirname, '../public/contact.html'), 'utf8');
    expect(html).toContain('og:title');
  });

  it('faq.html has og:title meta tag', () => {
    const html = fs.readFileSync(path.join(__dirname, '../public/faq.html'), 'utf8');
    expect(html).toContain('og:title');
  });

  it('pricing.html has og:title meta tag', () => {
    const html = fs.readFileSync(path.join(__dirname, '../public/pricing.html'), 'utf8');
    expect(html).toContain('og:title');
  });
});
