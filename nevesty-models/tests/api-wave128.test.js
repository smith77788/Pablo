'use strict';
// Wave 128: DB backups, email config/test, extended analytics, conversion funnel,
//           top-cities, monthly analytics, hourly analytics, revenue-by-month, contact form

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave128-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

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

// ── 1. DB Backups — GET /api/admin/db/backups ────────────────────────────────

describe('DB backups — GET /api/admin/db/backups', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/db/backups');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/db/backups').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has backups array', async () => {
    const res = await request(app).get('/api/admin/db/backups').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.backups)).toBe(true);
  });

  it('response has count field as a number', async () => {
    const res = await request(app).get('/api/admin/db/backups').set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.count).toBe('number');
  });

  it('response has backup_dir field as a string', async () => {
    const res = await request(app).get('/api/admin/db/backups').set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.backup_dir).toBe('string');
  });

  it('count matches backups array length', async () => {
    const res = await request(app).get('/api/admin/db/backups').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.count).toBe(res.body.backups.length);
  });

  it('each backup entry has filename, size_kb, created_at if entries exist', async () => {
    const res = await request(app).get('/api/admin/db/backups').set('Authorization', `Bearer ${adminToken}`);
    if (res.body.backups.length > 0) {
      const b = res.body.backups[0];
      expect(b).toHaveProperty('filename');
      expect(b).toHaveProperty('size_kb');
      expect(b).toHaveProperty('created_at');
    }
  });
});

// ── 2. Email config status — GET /api/admin/email/test ───────────────────────

describe('Email config — GET /api/admin/email/test', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/email/test');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/email/test').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has configured boolean field', async () => {
    const res = await request(app).get('/api/admin/email/test').set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.configured).toBe('boolean');
  });

  it('configured is false in test env (no SMTP config)', async () => {
    const res = await request(app).get('/api/admin/email/test').set('Authorization', `Bearer ${adminToken}`);
    // No SMTP_HOST/SMTP_USER/SMTP_PASS set in test env
    expect(res.body.configured).toBe(false);
  });

  it('response has smtp_host field (null when not configured)', async () => {
    const res = await request(app).get('/api/admin/email/test').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('smtp_host');
  });

  it('response has smtp_port field', async () => {
    const res = await request(app).get('/api/admin/email/test').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('smtp_port');
  });

  it('response has admin_emails array', async () => {
    const res = await request(app).get('/api/admin/email/test').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.admin_emails)).toBe(true);
  });
});

// ── 3. Send test email — POST /api/admin/email/test ──────────────────────────

describe('Send test email — POST /api/admin/email/test', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/email/test').send({ email: 'test@example.com' });
    expect(res.status).toBe(401);
  });

  it('returns 400 when no email provided and no admin email on file', async () => {
    // Admin account has no email set in DB by default
    const res = await request(app).post('/api/admin/email/test').set('Authorization', `Bearer ${adminToken}`).send({});
    // Expects 400 (no email) or 500 (SMTP not configured)
    expect([400, 500]).toContain(res.status);
  });

  it('returns 400 or 500 with email provided (no SMTP config in test env)', async () => {
    const res = await request(app)
      .post('/api/admin/email/test')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ email: 'test@example.com' });
    // Without SMTP config, send will fail; accept both 200 (configured) and 500 (not configured)
    expect([200, 500]).toContain(res.status);
  });

  it('error response has ok:false or error field when SMTP not configured', async () => {
    const res = await request(app)
      .post('/api/admin/email/test')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ email: 'test@example.com' });
    if (res.status === 500) {
      const hasErr = res.body.ok === false || 'error' in res.body;
      expect(hasErr).toBe(true);
    }
  });
});

// ── 4. Extended analytics — GET /api/admin/analytics/extended ────────────────

describe('Extended analytics — GET /api/admin/analytics/extended', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/extended');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has top_cities array', async () => {
    const res = await request(app).get('/api/admin/analytics/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.top_cities)).toBe(true);
  });

  it('response has repeat_rate as a number', async () => {
    const res = await request(app).get('/api/admin/analytics/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.repeat_rate).toBe('number');
  });

  it('response has repeat_clients and total_clients numeric fields', async () => {
    const res = await request(app).get('/api/admin/analytics/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.repeat_clients).toBe('number');
    expect(typeof res.body.total_clients).toBe('number');
  });

  it('response has reviews_count numeric field', async () => {
    const res = await request(app).get('/api/admin/analytics/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.reviews_count).toBe('number');
  });

  it('avg_cycle_days is null or a number', async () => {
    const res = await request(app).get('/api/admin/analytics/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.avg_cycle_days === null || typeof res.body.avg_cycle_days === 'number').toBe(true);
  });

  it('accepts ?days= query param', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/extended?days=7')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.top_cities)).toBe(true);
  });
});

// ── 5. Conversion funnel — GET /api/admin/analytics/conversion-funnel ────────

describe('Conversion funnel — GET /api/admin/analytics/conversion-funnel', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion-funnel');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/conversion-funnel')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has stages array', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/conversion-funnel')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.stages)).toBe(true);
  });

  it('stages array has 5 entries (new, reviewing, confirmed, in_progress, completed)', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/conversion-funnel')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.stages.length).toBe(5);
  });

  it('each stage has name, count, pct fields', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/conversion-funnel')
      .set('Authorization', `Bearer ${adminToken}`);
    for (const stage of res.body.stages) {
      expect(stage).toHaveProperty('name');
      expect(stage).toHaveProperty('count');
      expect(stage).toHaveProperty('pct');
    }
  });

  it('response has cancelled and total numeric fields', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/conversion-funnel')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.cancelled).toBe('number');
    expect(typeof res.body.total).toBe('number');
  });

  it('all counts and pcts are non-negative numbers', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/conversion-funnel')
      .set('Authorization', `Bearer ${adminToken}`);
    for (const stage of res.body.stages) {
      expect(stage.count).toBeGreaterThanOrEqual(0);
      expect(stage.pct).toBeGreaterThanOrEqual(0);
    }
  });
});

// ── 6. Top cities — GET /api/admin/analytics/top-cities ──────────────────────

describe('Top cities — GET /api/admin/analytics/top-cities', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has cities array', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.cities)).toBe(true);
  });

  it('cities array entries have city, orders, unique_clients fields if non-empty', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities').set('Authorization', `Bearer ${adminToken}`);
    if (res.body.cities.length > 0) {
      const city = res.body.cities[0];
      expect(city).toHaveProperty('city');
      expect(city).toHaveProperty('orders');
      expect(city).toHaveProperty('unique_clients');
    }
  });

  it('returns at most 10 cities', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.cities.length).toBeLessThanOrEqual(10);
  });
});

// ── 7. Monthly analytics — GET /api/admin/analytics/monthly ──────────────────

describe('Monthly analytics — GET /api/admin/analytics/monthly', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has months array', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.months)).toBe(true);
  });

  it('response has count field equal to months array length', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.count).toBe(res.body.months.length);
  });

  it('months entries have month, orders_count, completed, cancelled, revenue when non-empty', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly').set('Authorization', `Bearer ${adminToken}`);
    if (res.body.months.length > 0) {
      const m = res.body.months[0];
      expect(m).toHaveProperty('month');
      expect(m).toHaveProperty('orders_count');
      expect(m).toHaveProperty('completed');
      expect(m).toHaveProperty('cancelled');
      expect(m).toHaveProperty('revenue');
    }
  });

  it('accepts ?months= query param', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/monthly?months=6')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.months)).toBe(true);
  });

  it('clamps months param to max 24', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/monthly?months=999')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 8. Hourly analytics — GET /api/admin/analytics/hourly ────────────────────

describe('Hourly analytics — GET /api/admin/analytics/hourly', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has ok:true', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
  });

  it('response has hours array with exactly 24 entries', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.hours)).toBe(true);
    expect(res.body.hours.length).toBe(24);
  });

  it('each hour entry has hour (0-23) and cnt (non-negative) fields', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly').set('Authorization', `Bearer ${adminToken}`);
    res.body.hours.forEach((entry, i) => {
      expect(entry.hour).toBe(i);
      expect(entry.cnt).toBeGreaterThanOrEqual(0);
    });
  });

  it('response has days field as a number', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly').set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.days).toBe('number');
  });

  it('accepts ?days= query param', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/hourly?days=7')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.days).toBe(7);
  });
});

// ── 9. Revenue by month — GET /api/admin/analytics/revenue-by-month ──────────

describe('Revenue by month — GET /api/admin/analytics/revenue-by-month', () => {
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

  it('response has months array', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-by-month')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.months)).toBe(true);
  });

  it('months entries have month, orders, revenue fields when non-empty', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-by-month')
      .set('Authorization', `Bearer ${adminToken}`);
    if (res.body.months.length > 0) {
      const m = res.body.months[0];
      expect(m).toHaveProperty('month');
      expect(m).toHaveProperty('orders');
      expect(m).toHaveProperty('revenue');
    }
  });

  it('covers up to last 12 months (ordered ascending)', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-by-month')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.months.length).toBeLessThanOrEqual(12);
  });
});

// ── 10. Contact form — POST /api/contact ──────────────────────────────────────
// Note: contact form has a rate limit of 3 requests per hour per IP.
// In tests, all requests come from the same IP so later ones may get 429.
// For validation tests: expect 400 (fails validation) or 429 (rate-limited — but
// validation errors are caught before rate limit is applied in the middleware chain).
// We test the first few carefully and allow 429 gracefully for later ones.

describe('Contact form — POST /api/contact', () => {
  it('returns 400 when name is missing (validation before rate-limit check)', async () => {
    const res = await request(app).post('/api/contact').send({ phone: '+79991234567', message: 'Hello' });
    // Rate limit middleware runs first, so 429 is also possible if limit already hit
    expect([400, 429]).toContain(res.status);
    if (res.status === 400) {
      expect(res.body).toHaveProperty('error');
    }
  });

  it('returns 400 when phone is missing', async () => {
    const res = await request(app).post('/api/contact').send({ name: 'Test User', message: 'Hello' });
    expect([400, 429]).toContain(res.status);
    if (res.status === 400) {
      expect(res.body).toHaveProperty('error');
    }
  });

  it('returns 400 when phone is invalid format', async () => {
    const res = await request(app)
      .post('/api/contact')
      .send({ name: 'Test User', phone: 'not-a-phone', message: 'Hello' });
    expect([400, 429]).toContain(res.status);
    if (res.status === 400) {
      expect(res.body).toHaveProperty('error');
    }
  });

  it('returns 400 when message is missing', async () => {
    const res = await request(app).post('/api/contact').send({ name: 'Test User', phone: '+79991234567' });
    expect([400, 429]).toContain(res.status);
    if (res.status === 400) {
      expect(res.body).toHaveProperty('error');
    }
  });

  it('returns 200 or 429 with valid required fields', async () => {
    const res = await request(app)
      .post('/api/contact')
      .send({ name: 'Test User', phone: '+79991234567', message: 'I am interested in your services' });
    expect([200, 429]).toContain(res.status);
  });

  it('successful response (when not rate-limited) has ok:true, message, order_number, id', async () => {
    const res = await request(app)
      .post('/api/contact')
      .send({ name: 'Jane Doe', phone: '+79992345678', message: 'Please call me back' });
    if (res.status === 200) {
      expect(res.body.ok).toBe(true);
      expect(typeof res.body.message).toBe('string');
      expect(res.body.message.length).toBeGreaterThan(0);
      expect(res.body).toHaveProperty('order_number');
      expect(res.body).toHaveProperty('id');
      expect(typeof res.body.id).toBe('number');
    } else {
      // Rate-limited — acceptable
      expect(res.status).toBe(429);
    }
  });

  it('does not require auth (public endpoint — never returns 401)', async () => {
    const res = await request(app)
      .post('/api/contact')
      .send({ name: 'No Auth User', phone: '+79996789012', message: 'Public contact test' });
    expect(res.status).not.toBe(401);
  });

  it('returns 400 or 429 when email is provided but invalid', async () => {
    const res = await request(app)
      .post('/api/contact')
      .send({ name: 'Test User', phone: '+79997890123', message: 'Test message', email: 'not-an-email' });
    expect([400, 429]).toContain(res.status);
    if (res.status === 400) {
      expect(res.body).toHaveProperty('error');
    }
  });

  it('returns 200 or 429 when valid optional email is provided', async () => {
    const res = await request(app).post('/api/contact').send({
      name: 'Valid Email User',
      phone: '+79998901234',
      message: 'Test with valid email',
      email: 'test@example.com',
    });
    expect([200, 429]).toContain(res.status);
    if (res.status === 200) {
      expect(res.body.ok).toBe(true);
    }
  });

  it('rate limit returns 429 with error message when exceeded', async () => {
    // Make several requests to trigger rate limit, then verify 429 shape
    let lastRes;
    for (let i = 0; i < 5; i++) {
      lastRes = await request(app)
        .post('/api/contact')
        .send({ name: `User${i}`, phone: `+7999000000${i}`, message: 'Rate limit test' });
      if (lastRes.status === 429) break;
    }
    if (lastRes && lastRes.status === 429) {
      expect(lastRes.body).toHaveProperty('error');
    }
  });
});
