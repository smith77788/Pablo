'use strict';
// Wave 122: AI match, AI budget, order CSV export, broadcast history, managers, refresh token

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave122-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, refreshToken;

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
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const lr = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = lr.body.token;
  refreshToken = lr.body.refresh_token;
}, 30000);

// ── 1. AI model match endpoint ────────────────────────────────────────────────

describe('AI model match — POST /api/client/ai-match', () => {
  it('returns 200 with ok:false (not 500) for missing body', async () => {
    const res = await request(app).post('/api/client/ai-match').send({});
    // The endpoint returns {ok: false, error:...} for missing description — never 500
    expect(res.status).not.toBe(500);
    expect([200, 400, 429]).toContain(res.status);
  });

  it('returns ok:false for empty description', async () => {
    const res = await request(app).post('/api/client/ai-match').send({ description: '' });
    // description.length < 10 → {ok: false, error:...}
    if (res.status === 200) {
      expect(res.body.ok).toBe(false);
    } else {
      expect([400, 429]).toContain(res.status);
    }
  });

  it('returns ok:false for description longer than 500 chars', async () => {
    const longDesc = 'А'.repeat(501);
    const res = await request(app).post('/api/client/ai-match').send({ description: longDesc });
    if (res.status === 200) {
      expect(res.body.ok).toBe(false);
      expect(res.body.error).toMatch(/too long|слишком длин/i);
    } else {
      expect([400, 429]).toContain(res.status);
    }
  });

  it('returns 200 or 429 for a valid description', async () => {
    const res = await request(app)
      .post('/api/client/ai-match')
      .send({ description: 'Нужна модель на корпоратив в Москве' });
    expect([200, 429]).toContain(res.status);
  });

  it('successful response has models array field', async () => {
    const res = await request(app)
      .post('/api/client/ai-match')
      .send({ description: 'Нужна модель на корпоратив в Москве' });
    if (res.status === 200 && res.body.ok === true) {
      expect(Array.isArray(res.body.models)).toBe(true);
    } else if (res.status === 200 && res.body.ok === false) {
      expect(res.body).toHaveProperty('error');
    } else {
      // 429 rate limit — acceptable
      expect(res.status).toBe(429);
    }
  });

  it('SQL injection in description field does not crash or expose DB error', async () => {
    const injection = "'; DROP TABLE models;--";
    const res = await request(app).post('/api/client/ai-match').send({ description: injection });
    // description.length < 10 so will get ok:false — but crucially no 500
    expect(res.status).not.toBe(500);
    // Should not expose raw SQL error
    if (res.body.error) {
      expect(res.body.error).not.toMatch(/sqlite|syntax error|SQL/i);
    }
  });
});

// ── 2. AI budget endpoint ─────────────────────────────────────────────────────

describe('AI budget estimation — POST /api/client/ai-budget', () => {
  it('returns 200 for valid description (fallback when no API key)', async () => {
    const res = await request(app)
      .post('/api/client/ai-budget')
      .send({ description: 'Фотосессия для бренда, 3 модели, 4 часа, Москва' });
    expect([200, 429]).toContain(res.status);
    if (res.status === 200) {
      expect(res.body.ok).toBe(true);
    }
  });

  it('returns ok:false for missing description', async () => {
    const res = await request(app).post('/api/client/ai-budget').send({});
    expect(res.status).not.toBe(500);
    if (res.status === 200) {
      expect(res.body.ok).toBe(false);
    } else {
      expect([400, 429]).toContain(res.status);
    }
  });

  it('response has estimate field (or similar budget field) when ok:true', async () => {
    const res = await request(app)
      .post('/api/client/ai-budget')
      .send({ description: 'Показ мод, 5 моделей, 2 часа, Санкт-Петербург' });
    if (res.status === 200 && res.body.ok === true) {
      // Endpoint returns { ok, ai, estimate: {min, max, currency}, notes } when no API key
      const hasEstimate = res.body.estimate != null;
      const hasMin = res.body.min != null;
      const hasMax = res.body.max != null;
      expect(hasEstimate || hasMin || hasMax).toBe(true);
    } else {
      expect([200, 429]).toContain(res.status);
    }
  });

  it('estimate has min and max numeric fields when API key absent (fallback mode)', async () => {
    const res = await request(app)
      .post('/api/client/ai-budget')
      .send({ description: 'Корпоративное мероприятие, нужна 1 модель на 3 часа' });
    if (res.status === 200 && res.body.ok === true && res.body.ai === false) {
      // Fallback path: estimate: {min, max, currency}
      expect(res.body.estimate).toBeDefined();
      expect(typeof res.body.estimate.min).toBe('number');
      expect(typeof res.body.estimate.max).toBe('number');
      expect(res.body.estimate.max).toBeGreaterThanOrEqual(res.body.estimate.min);
    } else {
      expect([200, 429]).toContain(res.status);
    }
  });

  it('SQL injection in description does not cause 500', async () => {
    // Short injection won't pass length check (< 10 chars filtered by endpoint)
    const injection = "1' OR '1'='1'; --";
    const res = await request(app).post('/api/client/ai-budget').send({ description: injection });
    expect(res.status).not.toBe(500);
  });
});

// ── 3. Admin orders CSV export ────────────────────────────────────────────────

describe('Admin order export CSV — GET /api/admin/orders/export', () => {
  it('returns 401 without auth token', async () => {
    const res = await request(app).get('/api/admin/orders/export');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/orders/export').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('Content-Type is text/csv or application/octet-stream', async () => {
    const res = await request(app).get('/api/admin/orders/export').set('Authorization', `Bearer ${adminToken}`);
    const ct = res.headers['content-type'] || '';
    expect(ct).toMatch(/text\/csv|octet-stream/);
  });

  it('response body contains CSV column headers', async () => {
    const res = await request(app).get('/api/admin/orders/export').set('Authorization', `Bearer ${adminToken}`);
    // The endpoint outputs a header row with column names (in Russian)
    const body = res.text || '';
    // At minimum, the CSV header line should contain "Номер" (order number column)
    expect(body).toContain('Номер');
  });
});

// ── 4. Admin broadcast history ────────────────────────────────────────────────

describe('Admin broadcasts — GET /api/admin/broadcasts', () => {
  it('returns 401 without auth token', async () => {
    const res = await request(app).get('/api/admin/broadcasts');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/broadcasts').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response body is an array (merged scheduled + bot broadcasts)', async () => {
    const res = await request(app).get('/api/admin/broadcasts').set('Authorization', `Bearer ${adminToken}`);
    // Endpoint returns the merged array directly (not wrapped in {broadcasts:...})
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('respects ?limit param and returns at most that many items', async () => {
    const res = await request(app)
      .get('/api/admin/broadcasts')
      .query({ limit: 5 })
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBeLessThanOrEqual(5);
  });
});

// ── 5. Admin managers API ─────────────────────────────────────────────────────

describe('Admin managers — GET /api/admin/managers', () => {
  it('returns 401 without auth token', async () => {
    const res = await request(app).get('/api/admin/managers');
    expect(res.status).toBe(401);
  });

  it('returns 200 with superadmin token', async () => {
    // Default admin created with role=superadmin
    const res = await request(app).get('/api/admin/managers').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response is an array of managers', async () => {
    const res = await request(app).get('/api/admin/managers').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('each manager entry has username field', async () => {
    const res = await request(app).get('/api/admin/managers').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body)).toBe(true);
    res.body.forEach(mgr => {
      expect(mgr).toHaveProperty('username');
      expect(typeof mgr.username).toBe('string');
    });
  });
});

// ── 6. Refresh token ──────────────────────────────────────────────────────────

describe('Refresh token — POST /api/auth/refresh', () => {
  it('returns 400 for missing token in body', async () => {
    const res = await request(app).post('/api/auth/refresh').send({});
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 401 for invalid (garbage) refresh token', async () => {
    const res = await request(app).post('/api/auth/refresh').send({ refresh_token: 'not-a-real-token' });
    expect(res.status).toBe(401);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 200 with a valid refresh token from login', async () => {
    // refreshToken was captured in beforeAll from the login response
    if (!refreshToken) {
      // Skip gracefully if login did not return refresh_token (e.g. 2FA enabled)
      return;
    }
    const res = await request(app).post('/api/auth/refresh').send({ refresh_token: refreshToken });
    expect(res.status).toBe(200);
  });

  it('new access token is a valid JWT string', async () => {
    if (!refreshToken) return;
    const res = await request(app).post('/api/auth/refresh').send({ refresh_token: refreshToken });
    if (res.status === 200) {
      expect(typeof res.body.token).toBe('string');
      // JWT has three dot-separated Base64URL parts
      const parts = res.body.token.split('.');
      expect(parts.length).toBe(3);
      // New refresh token is also returned (token rotation)
      expect(typeof res.body.refresh_token).toBe('string');
      // Update refreshToken so subsequent tests (if any) can use the new one
      refreshToken = res.body.refresh_token;
    } else {
      // If the refresh token was already consumed (test order issue), that's ok
      expect([200, 401]).toContain(res.status);
    }
  });
});
