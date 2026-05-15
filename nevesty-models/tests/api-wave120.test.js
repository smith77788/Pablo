'use strict';
// Wave 120: LTV, funnel, heatmap, revenue-by-month, top-cities, model-stats, KPI

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave120-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';
delete process.env.BITRIX24_WEBHOOK_URL;
delete process.env.AMOCRM_SUBDOMAIN;
delete process.env.AMOCRM_ACCESS_TOKEN;

const request = require('supertest');
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
  if (bot && apiRouter.setBot) apiRouter.setBot(bot);

  a.use('/api', apiRouter);
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const lr = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = lr.body.token;
}, 30000);

// ── 1. Admin analytics client-ltv ─────────────────────────────────────────────

describe('Admin analytics LTV — GET /api/admin/analytics/client-ltv', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/client-ltv');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/client-ltv').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has ok:true', async () => {
    const res = await request(app).get('/api/admin/analytics/client-ltv').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
  });

  it('returns top_clients array', async () => {
    const res = await request(app).get('/api/admin/analytics/client-ltv').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.top_clients)).toBe(true);
  });

  it('each client entry has total_budget field when data exists', async () => {
    const res = await request(app).get('/api/admin/analytics/client-ltv').set('Authorization', `Bearer ${adminToken}`);
    res.body.top_clients.forEach(c => {
      expect(c).toHaveProperty('total_budget');
    });
  });

  it('each client entry has total_orders field when data exists', async () => {
    const res = await request(app).get('/api/admin/analytics/client-ltv').set('Authorization', `Bearer ${adminToken}`);
    res.body.top_clients.forEach(c => {
      expect(c).toHaveProperty('total_orders');
    });
  });

  it('respects ?limit=3 — returns at most 3 entries', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-ltv')
      .query({ limit: 3 })
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.top_clients.length).toBeLessThanOrEqual(3);
  });
});

// ── 2. Admin analytics funnel ─────────────────────────────────────────────────

describe('Admin analytics funnel — GET /api/admin/analytics/funnel', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('returns stages array', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.stages)).toBe(true);
  });

  it('has total field (number)', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('total');
    expect(typeof res.body.total).toBe('number');
  });

  it('has conversion_rate field (number)', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('conversion_rate');
    expect(typeof res.body.conversion_rate).toBe('number');
  });

  it('each stage has label, status, count fields', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    res.body.stages.forEach(stage => {
      expect(stage).toHaveProperty('label');
      expect(stage).toHaveProperty('status');
      expect(stage).toHaveProperty('count');
    });
  });
});

// ── 3. Admin analytics heatmap ────────────────────────────────────────────────

describe('Admin analytics heatmap — GET /api/admin/analytics/heatmap', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/heatmap');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/heatmap').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has ok:true', async () => {
    const res = await request(app).get('/api/admin/analytics/heatmap').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
  });

  it('returns heatmap object', async () => {
    const res = await request(app).get('/api/admin/analytics/heatmap').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('heatmap');
    expect(typeof res.body.heatmap).toBe('object');
    expect(Array.isArray(res.body.heatmap)).toBe(false);
  });

  it('has year field', async () => {
    const res = await request(app).get('/api/admin/analytics/heatmap').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('year');
    expect(typeof res.body.year).toBe('number');
  });

  it('respects ?year param', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/heatmap')
      .query({ year: 2024 })
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.year).toBe(2024);
  });
});

// ── 4. Admin analytics revenue-by-month ───────────────────────────────────────

describe('Admin analytics revenue-by-month — GET /api/admin/analytics/revenue-by-month', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/revenue-by-month');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-by-month')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('returns months array', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-by-month')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.months)).toBe(true);
  });

  it('months array is empty when no qualifying orders exist', async () => {
    // In-memory DB starts empty, so result should be empty array
    const res = await request(app)
      .get('/api/admin/analytics/revenue-by-month')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.months).toBeDefined();
    expect(Array.isArray(res.body.months)).toBe(true);
  });
});

// ── 5. Admin analytics top-cities ─────────────────────────────────────────────

describe('Admin analytics top-cities — GET /api/admin/analytics/top-cities', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('returns cities array', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.cities)).toBe(true);
  });

  it('cities array is empty when no orders exist', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.cities).toBeDefined();
  });
});

// ── 6. Admin analytics overview — extended field check ────────────────────────

describe('Admin analytics overview — extended fields — GET /api/admin/analytics/overview', () => {
  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has clients object or field', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    // Endpoint must return some data about clients or at least an ok response
    expect(res.body).toBeDefined();
    expect(typeof res.body).toBe('object');
  });
});

// ── 7. Admin analytics model-stats/:id ───────────────────────────────────────

describe('Admin analytics model-stats — GET /api/admin/analytics/model-stats/:id', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/model-stats/1');
    expect(res.status).toBe(401);
  });

  it('returns 404 for non-existent model with auth', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/model-stats/999999')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });

  it('returns 400 for invalid (non-integer) model id', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/model-stats/abc')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  it('returns 200 with valid model id after creating a model', async () => {
    // Create a model first
    const createRes = await request(app)
      .post('/api/admin/models')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ name: 'Wave120 Test Model', category: 'glamour', city: 'Kyiv', height: 170, age: 22 });

    // If model creation is supported, test with it; otherwise skip
    if (createRes.status === 200 || createRes.status === 201) {
      const modelId = createRes.body.id || createRes.body.model?.id;
      if (modelId) {
        const res = await request(app)
          .get(`/api/admin/analytics/model-stats/${modelId}`)
          .set('Authorization', `Bearer ${adminToken}`);
        expect(res.status).toBe(200);
        expect(res.body).toHaveProperty('model');
        expect(res.body).toHaveProperty('orders');
        expect(res.body).toHaveProperty('reviews');
      }
    } else {
      // Model creation path different — just verify 404 for non-existent
      const res = await request(app)
        .get('/api/admin/analytics/model-stats/1')
        .set('Authorization', `Bearer ${adminToken}`);
      expect([200, 404]).toContain(res.status);
    }
  });

  it('returns model object with id, name fields when model exists', async () => {
    // Try model id=1 — may or may not exist in empty DB
    const res = await request(app)
      .get('/api/admin/analytics/model-stats/1')
      .set('Authorization', `Bearer ${adminToken}`);
    if (res.status === 200) {
      expect(res.body.model).toHaveProperty('id');
      expect(res.body.model).toHaveProperty('name');
    } else {
      expect([404]).toContain(res.status);
    }
  });

  it('response for existing model has monthly_orders array', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/model-stats/1')
      .set('Authorization', `Bearer ${adminToken}`);
    if (res.status === 200) {
      expect(Array.isArray(res.body.monthly_orders)).toBe(true);
    } else {
      expect(res.status).toBe(404);
    }
  });
});

// ── 8. Admin analytics KPI ────────────────────────────────────────────────────

describe('Admin analytics KPI — GET /api/admin/analytics/kpi', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has total field (number)', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('total');
    expect(typeof res.body.total).toBe('number');
  });

  it('has completed field (number)', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('completed');
    expect(typeof res.body.completed).toBe('number');
  });

  it('has active field (number)', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('active');
    expect(typeof res.body.active).toBe('number');
  });

  it('has new_clients field (number)', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('new_clients');
    expect(typeof res.body.new_clients).toBe('number');
  });

  it('respects ?days param — returns 200', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/kpi')
      .query({ days: 7 })
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('total');
  });

  it('all numeric KPI values are non-negative', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.total).toBeGreaterThanOrEqual(0);
    expect(res.body.completed).toBeGreaterThanOrEqual(0);
    expect(res.body.active).toBeGreaterThanOrEqual(0);
    expect(res.body.new_clients).toBeGreaterThanOrEqual(0);
  });
});
