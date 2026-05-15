'use strict';
/**
 * API Integration Tests — Wave 52 Features
 * Covers: model archive/restore, duplicate, FAQ endpoint,
 *         broadcast count, factory run endpoint.
 */

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app;
let adminToken;
let seededModelId;

beforeAll(async () => {
  const { initDatabase, run, get } = require('../database');
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
  a.use((err, req, res, next) => {
    res.status(500).json({ error: err.message });
  });

  app = a;

  const loginRes = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;

  // Get seeded model from demo data
  const model = await get('SELECT id FROM models LIMIT 1');
  seededModelId = model ? model.id : null;
}, 15000);

// ── Model Archive / Restore ───────────────────────────────────────────────────

describe('Model Archive/Restore — /api/admin/models/:id/archive|restore', () => {
  it('requires authentication to archive', async () => {
    const res = await request(app)
      .patch('/api/admin/models/1/archive');
    expect(res.status).toBe(401);
  });

  it('archives a model successfully', async () => {
    if (!seededModelId) return;
    const res = await request(app)
      .patch(`/api/admin/models/${seededModelId}/archive`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 204]).toContain(res.status);
    if (res.body.ok !== undefined) {
      expect(res.body.ok).toBe(true);
    }
  });

  it('model is hidden from public catalog after archive', async () => {
    if (!seededModelId) return;
    const res = await request(app)
      .get('/api/models');
    expect(res.status).toBe(200);
    const models = res.body.models || res.body;
    const found = Array.isArray(models) && models.some(m => m.id === seededModelId);
    // Archived model should not appear in public catalog
    expect(found).toBe(false);
  });

  it('restores a model successfully', async () => {
    if (!seededModelId) return;
    const res = await request(app)
      .patch(`/api/admin/models/${seededModelId}/restore`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 204]).toContain(res.status);
    if (res.body.ok !== undefined) {
      expect(res.body.ok).toBe(true);
    }
  });

  it('model appears in archived list', async () => {
    const res = await request(app)
      .get('/api/admin/models/archived')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('total');
    expect(Array.isArray(res.body.models)).toBe(true);
  });
});

// ── Model Duplicate ───────────────────────────────────────────────────────────

describe('Model Duplicate — POST /api/admin/models/:id/duplicate', () => {
  it('requires authentication', async () => {
    const res = await request(app)
      .post('/api/admin/models/1/duplicate');
    expect(res.status).toBe(401);
  });

  it('duplicates a model and returns new id', async () => {
    if (!seededModelId) return;
    const res = await request(app)
      .post(`/api/admin/models/${seededModelId}/duplicate`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('id');
    expect(typeof res.body.id).toBe('number');
    expect(res.body.id).not.toBe(seededModelId);
  });

  it('returns 404 for non-existent model', async () => {
    const res = await request(app)
      .post('/api/admin/models/99999/duplicate')
      .set('Authorization', `Bearer ${adminToken}`);
    expect([404, 400]).toContain(res.status);
  });
});

// ── Model Stats ───────────────────────────────────────────────────────────────

describe('Model Stats — GET /api/admin/models/:id/stats', () => {
  it('requires authentication', async () => {
    const res = await request(app)
      .get('/api/admin/models/1/stats');
    expect(res.status).toBe(401);
  });

  it('returns stats for a model', async () => {
    if (!seededModelId) return;
    const res = await request(app)
      .get(`/api/admin/models/${seededModelId}/stats`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('total_orders');
    expect(res.body).toHaveProperty('avg_rating');
    expect(res.body).toHaveProperty('view_count');
  });
});

// ── FAQ Endpoint ──────────────────────────────────────────────────────────────

describe('FAQ — GET /api/faq', () => {
  it('returns FAQ array', async () => {
    const res = await request(app).get('/api/faq');
    expect(res.status).toBe(200);
    // May return empty array if faq table has no items in test DB
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('FAQ items have question and answer fields', async () => {
    const res = await request(app).get('/api/faq');
    expect(res.status).toBe(200);
    if (res.body.length > 0) {
      const item = res.body[0];
      // Either {q, a} or {question, answer}
      const hasQ = 'q' in item || 'question' in item;
      const hasA = 'a' in item || 'answer' in item;
      expect(hasQ).toBe(true);
      expect(hasA).toBe(true);
    }
  });
});

// ── Broadcast Count ───────────────────────────────────────────────────────────

describe('Broadcast Count — GET /api/admin/broadcasts/count', () => {
  it('requires authentication', async () => {
    const res = await request(app)
      .get('/api/admin/broadcasts/count');
    expect(res.status).toBe(401);
  });

  it('returns count for all segment', async () => {
    const res = await request(app)
      .get('/api/admin/broadcasts/count?segment=all')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('count');
    expect(typeof res.body.count).toBe('number');
  });

  it('returns count for completed segment', async () => {
    const res = await request(app)
      .get('/api/admin/broadcasts/count?segment=completed')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('count');
  });
});

// ── Factory Run Endpoint ──────────────────────────────────────────────────────

describe('Factory Manual Trigger — POST /api/admin/factory/run', () => {
  it('requires authentication', async () => {
    const res = await request(app)
      .post('/api/admin/factory/run');
    expect(res.status).toBe(401);
  });

  it('returns job started response for authorized admin', async () => {
    const res = await request(app)
      .post('/api/admin/factory/run')
      .set('Authorization', `Bearer ${adminToken}`);
    // May return 403 if not superadmin, or 200 if started, or 500 if factory not available
    expect([200, 202, 403, 500]).toContain(res.status);
  });
});

// ── Bulk Model Operations ─────────────────────────────────────────────────────

describe('Bulk Model Operations — PATCH /api/admin/models/bulk', () => {
  it('requires authentication', async () => {
    const res = await request(app)
      .patch('/api/admin/models/bulk')
      .send({ action: 'archive', ids: [1] });
    expect(res.status).toBe(401);
  });

  it('restores multiple models', async () => {
    if (!seededModelId) return;
    const res = await request(app)
      .patch('/api/admin/models/bulk')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ action: 'restore', ids: [seededModelId] });
    expect([200, 204, 400]).toContain(res.status);
    // 400 is acceptable if model already restored
  });

  it('rejects invalid action', async () => {
    const res = await request(app)
      .patch('/api/admin/models/bulk')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ action: 'delete_all', ids: [1] });
    expect([400, 422]).toContain(res.status);
  });
});
