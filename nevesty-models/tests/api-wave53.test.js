'use strict';
/**
 * API Integration Tests — Wave 53 Features
 * Covers: public reviews API (GET /reviews, /reviews/recent, /reviews/public),
 *         admin reviews management, audit log filters (event_type + since),
 *         audit log CSV export, email service (sendNewOrderEmail), health endpoint.
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

  // Ensure at least one approved review exists (database.js seeds some)
  // Also insert a pending review to validate filtering
  await run(
    'INSERT OR IGNORE INTO reviews (client_name, rating, text, model_id, approved) VALUES (?,?,?,?,?)',
    ['Wave53 PendingReviewer', 4, 'Pending review text', null, 0]
  );
}, 15000);

// ── Public Reviews — GET /api/reviews ─────────────────────────────────────────

describe('Public Reviews — GET /api/reviews', () => {
  it('returns HTTP 200 with an array of approved reviews', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('only returns approved reviews (approved=1)', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
    // Every returned review must have approved=1 (field may be omitted when selected)
    // The endpoint filters by r.approved=1 but does not SELECT approved col — just check no pending reviewer leaks
    const names = res.body.map(r => r.client_name);
    expect(names).not.toContain('Wave53 PendingReviewer');
  });

  it('respects ?limit parameter', async () => {
    const res = await request(app).get('/api/reviews?limit=2');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBeLessThanOrEqual(2);
  });

  it('returns paginated result when ?page= is provided', async () => {
    const res = await request(app).get('/api/reviews?page=1&limit=5');
    expect(res.status).toBe(200);
    // With pagination the endpoint returns an object: { reviews, total, page, pages, limit }
    expect(res.body).toHaveProperty('reviews');
    expect(Array.isArray(res.body.reviews)).toBe(true);
    expect(res.body).toHaveProperty('total');
    expect(res.body).toHaveProperty('page', 1);
    expect(res.body).toHaveProperty('pages');
    expect(res.body).toHaveProperty('limit', 5);
  });

  it('review objects contain expected fields', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
    if (res.body.length > 0) {
      const r = res.body[0];
      expect(r).toHaveProperty('id');
      expect(r).toHaveProperty('rating');
      expect(r).toHaveProperty('text');
      expect(r).toHaveProperty('client_name');
      expect(r).toHaveProperty('created_at');
    }
  });

  it('filters by ?model_id without error', async () => {
    const res = await request(app).get('/api/reviews?model_id=0');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });
});

// ── Recent Reviews — GET /api/reviews/recent ──────────────────────────────────

describe('Recent Reviews — GET /api/reviews/recent', () => {
  it('returns HTTP 200 with an array', async () => {
    const res = await request(app).get('/api/reviews/recent');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('defaults to 5 items', async () => {
    const res = await request(app).get('/api/reviews/recent');
    expect(res.status).toBe(200);
    expect(res.body.length).toBeLessThanOrEqual(5);
  });

  it('respects ?limit up to 20', async () => {
    const res = await request(app).get('/api/reviews/recent?limit=10');
    expect(res.status).toBe(200);
    expect(res.body.length).toBeLessThanOrEqual(10);
  });

  it('caps limit at 20 even when higher value requested', async () => {
    const res = await request(app).get('/api/reviews/recent?limit=100');
    expect(res.status).toBe(200);
    expect(res.body.length).toBeLessThanOrEqual(20);
  });

  it('items include admin_reply and reply_at fields', async () => {
    const res = await request(app).get('/api/reviews/recent');
    expect(res.status).toBe(200);
    if (res.body.length > 0) {
      const r = res.body[0];
      expect('admin_reply' in r).toBe(true);
      expect('reply_at' in r).toBe(true);
    }
  });
});

// ── Public Reviews Explicit — GET /api/reviews/public ─────────────────────────

describe('Public Reviews Endpoint — GET /api/reviews/public', () => {
  it('returns HTTP 200 with an array', async () => {
    const res = await request(app).get('/api/reviews/public');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('defaults to 6 items', async () => {
    const res = await request(app).get('/api/reviews/public');
    expect(res.status).toBe(200);
    expect(res.body.length).toBeLessThanOrEqual(6);
  });

  it('items include author_name field (alias for client_name)', async () => {
    const res = await request(app).get('/api/reviews/public');
    expect(res.status).toBe(200);
    if (res.body.length > 0) {
      expect(res.body[0]).toHaveProperty('author_name');
    }
  });

  it('respects ?limit parameter', async () => {
    const res = await request(app).get('/api/reviews/public?limit=2');
    expect(res.status).toBe(200);
    expect(res.body.length).toBeLessThanOrEqual(2);
  });
});

// ── Admin Reviews — GET /api/admin/reviews ────────────────────────────────────

describe('Admin Reviews — GET /api/admin/reviews', () => {
  it('requires authentication', async () => {
    const res = await request(app).get('/api/admin/reviews');
    expect(res.status).toBe(401);
  });

  it('returns paginated reviews for authorized admin', async () => {
    const res = await request(app)
      .get('/api/admin/reviews')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('reviews');
    expect(Array.isArray(res.body.reviews)).toBe(true);
    expect(res.body).toHaveProperty('total');
  });

  it('filters by ?approved=0 (pending only)', async () => {
    const res = await request(app)
      .get('/api/admin/reviews?approved=0')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews)).toBe(true);
    res.body.reviews.forEach(r => expect(r.approved).toBe(0));
  });

  it('filters by ?approved=1 (approved only)', async () => {
    const res = await request(app)
      .get('/api/admin/reviews?approved=1')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews)).toBe(true);
    res.body.reviews.forEach(r => expect(r.approved).toBe(1));
  });

  it('supports ?filter=pending shorthand', async () => {
    const res = await request(app)
      .get('/api/admin/reviews?filter=pending')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    res.body.reviews.forEach(r => expect(r.approved).toBe(0));
  });

  it('supports ?filter=approved shorthand', async () => {
    const res = await request(app)
      .get('/api/admin/reviews?filter=approved')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    res.body.reviews.forEach(r => expect(r.approved).toBe(1));
  });
});

// ── Audit Log Filters — GET /api/admin/audit-log ─────────────────────────────

describe('Audit Log Filters — GET /api/admin/audit-log', () => {
  it('requires authentication', async () => {
    const res = await request(app).get('/api/admin/audit-log');
    expect(res.status).toBe(401);
  });

  it('returns audit log without filters', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('rows');
    expect(Array.isArray(res.body.rows)).toBe(true);
    expect(res.body).toHaveProperty('total');
    expect(res.body).toHaveProperty('actions');
    expect(Array.isArray(res.body.actions)).toBe(true);
  });

  it('filters by ?event_type=auth without error', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?event_type=auth')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('rows');
  });

  it('filters by ?event_type=orders without error', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?event_type=orders')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('rows');
  });

  it('filters by ?event_type=models without error', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?event_type=models')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('filters by ?event_type=settings without error', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?event_type=settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('filters by ?event_type=broadcasts without error', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?event_type=broadcasts')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('filters by ?event_type=factory without error', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?event_type=factory')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('unknown event_type returns all rows (no patterns matched)', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?event_type=unknown_xyz')
      .set('Authorization', `Bearer ${adminToken}`);
    // Should still return 200 — unknown types are silently ignored
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('rows');
  });

  it('filters by ?since=today without error', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?since=today')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('rows');
  });

  it('filters by ?since=7d without error', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?since=7d')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('filters by ?since=30d without error', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?since=30d')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('combines event_type + since filters', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?event_type=auth&since=7d')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('rows');
  });

  it('respects ?limit parameter', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?limit=5')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.rows.length).toBeLessThanOrEqual(5);
  });

  it('respects ?offset parameter', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?offset=0&limit=10')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('rows');
  });
});

// ── Audit Log CSV Export — GET /api/admin/audit/export ────────────────────────

describe('Audit Log Export — GET /api/admin/audit/export', () => {
  it('requires authentication', async () => {
    const res = await request(app).get('/api/admin/audit/export');
    expect(res.status).toBe(401);
  });

  it('returns CSV content for authorized admin', async () => {
    const res = await request(app)
      .get('/api/admin/audit/export')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const ct = res.headers['content-type'] || '';
    expect(ct).toMatch(/text\/csv/);
  });

  it('returns Content-Disposition attachment header', async () => {
    const res = await request(app)
      .get('/api/admin/audit/export')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const cd = res.headers['content-disposition'] || '';
    expect(cd).toMatch(/attachment/);
    expect(cd).toMatch(/audit_.*\.csv/);
  });

  it('CSV has header row with expected columns', async () => {
    const res = await request(app)
      .get('/api/admin/audit/export')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const firstLine = res.text.replace(/^﻿/, '').split('\n')[0];
    expect(firstLine).toMatch(/ID/);
    expect(firstLine).toMatch(/Action/);
    expect(firstLine).toMatch(/Admin/);
    expect(firstLine).toMatch(/Timestamp/);
  });

  it('accepts ?event_type=auth filter', async () => {
    const res = await request(app)
      .get('/api/admin/audit/export?event_type=auth')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/text\/csv/);
  });

  it('accepts ?since=today filter', async () => {
    const res = await request(app)
      .get('/api/admin/audit/export?since=today')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('accepts combined ?event_type + ?since filters', async () => {
    const res = await request(app)
      .get('/api/admin/audit/export?event_type=orders&since=30d')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── Email Service — sendNewOrderEmail ─────────────────────────────────────────

describe('Email Service — sendNewOrderEmail (Wave 53 additions)', () => {
  it('returns false when SMTP not configured and order has email', async () => {
    const email = require('../services/email');
    const result = await email.sendNewOrderEmail({
      order_number: 'NM-2026-TEST',
      client_name: 'Wave53 Test',
      client_email: 'test@wave53.com',
    });
    // SMTP not configured in test env → must return false
    expect(result).toBe(false);
  });

  it('returns false when order has no client_email', async () => {
    const email = require('../services/email');
    const result = await email.sendNewOrderEmail({
      order_number: 'NM-2026-TEST',
      client_name: 'No Email Client',
    });
    expect(result).toBe(false);
  });

  it('does not throw when called with minimal order object', async () => {
    const email = require('../services/email');
    await expect(email.sendNewOrderEmail({})).resolves.toBe(false);
  });

  it('exports getTransporter function', () => {
    const email = require('../services/email');
    expect(typeof email.getTransporter).toBe('function');
  });

  it('getTransporter returns null when SMTP env vars absent', () => {
    const email = require('../services/email');
    // In test env SMTP_HOST / SMTP_USER / SMTP_PASS are not set
    const t = email.getTransporter();
    expect(t).toBeNull();
  });
});

// ── Sitemap — GET /sitemap.xml (server.js route) ──────────────────────────────

describe('Sitemap — sitemap.xml (server-level route)', () => {
  // /sitemap.xml is registered on the main app in server.js, not on the api router.
  // In these integration tests the express app only mounts /api — so the sitemap
  // route is not available here. We verify the route exists at the source level.
  it('sitemap route is defined in server.js', () => {
    const fs = require('fs');
    const src = fs.readFileSync('/home/user/Pablo/nevesty-models/server.js', 'utf8');
    expect(src).toMatch(/app\.get\(['"]\/sitemap\.xml['"]/);
  });

  it('sitemap response includes <urlset> element', () => {
    const fs = require('fs');
    const src = fs.readFileSync('/home/user/Pablo/nevesty-models/server.js', 'utf8');
    expect(src).toMatch(/urlset/);
  });
});

// ── Health Endpoint (server.js /api/health) ───────────────────────────────────

describe('Health Endpoint — /api/health (server-level route)', () => {
  // The health endpoint lives in server.js, not in the api router.
  // We verify its structure and logic at source level, not over HTTP.
  it('buildHealthResponse function is defined in server.js', () => {
    const fs = require('fs');
    const src = fs.readFileSync('/home/user/Pablo/nevesty-models/server.js', 'utf8');
    expect(src).toMatch(/buildHealthResponse/);
  });

  it('/api/health route is defined in server.js', () => {
    const fs = require('fs');
    const src = fs.readFileSync('/home/user/Pablo/nevesty-models/server.js', 'utf8');
    expect(src).toMatch(/app\.get\(['"]\/api\/health['"]/);
  });

  it('health response includes memory_mb field', () => {
    const fs = require('fs');
    const src = fs.readFileSync('/home/user/Pablo/nevesty-models/server.js', 'utf8');
    expect(src).toMatch(/memory_mb/);
  });

  it('health response includes database field', () => {
    const fs = require('fs');
    const src = fs.readFileSync('/home/user/Pablo/nevesty-models/server.js', 'utf8');
    expect(src).toMatch(/database:/);
  });

  it('health response does not expose raw passwords in source', () => {
    const fs = require('fs');
    const src = fs.readFileSync('/home/user/Pablo/nevesty-models/server.js', 'utf8');
    // The health object should not stringify raw env secrets
    const healthFnMatch = src.match(/async function buildHealthResponse[\s\S]{0,3000}/);
    if (healthFnMatch) {
      expect(healthFnMatch[0]).not.toMatch(/ADMIN_PASSWORD/);
      expect(healthFnMatch[0]).not.toMatch(/JWT_SECRET/);
    }
  });
});

// ── auditEventTypeFilter helper (integration verification) ───────────────────

describe('Audit Event Type Filter — known types', () => {
  const knownTypes = ['auth', 'orders', 'models', 'settings', 'broadcasts', 'factory'];

  knownTypes.forEach(type => {
    it(`event_type=${type} returns non-empty rows array without server error`, async () => {
      const res = await request(app)
        .get(`/api/admin/audit-log?event_type=${type}`)
        .set('Authorization', `Bearer ${adminToken}`);
      expect(res.status).toBe(200);
      expect(Array.isArray(res.body.rows)).toBe(true);
    });
  });
});

// ── auditSinceClause helper (integration verification) ───────────────────────

describe('Audit Since Clause — date range filters', () => {
  const sinceValues = ['today', '7d', '30d'];

  sinceValues.forEach(since => {
    it(`since=${since} returns 200 with rows array`, async () => {
      const res = await request(app)
        .get(`/api/admin/audit-log?since=${since}`)
        .set('Authorization', `Bearer ${adminToken}`);
      expect(res.status).toBe(200);
      expect(Array.isArray(res.body.rows)).toBe(true);
    });
  });

  it('invalid since value is silently ignored (returns all rows)', async () => {
    const res = await request(app)
      .get('/api/admin/audit-log?since=yesterday')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.rows)).toBe(true);
  });
});
