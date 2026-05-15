'use strict';
// Wave 113: Catalog filters, Settings public API, Admin review management, Factory status

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-wave113-minimum-32chars!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.BOT_TOKEN = '123:test';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken;

beforeAll(async () => {
  const { initDatabase, run } = require('../database');
  await initDatabase();

  // Seed models with various heights/ages for filter tests
  await run(
    `INSERT INTO models (name, age, height, category, available, featured, archived)
     VALUES (?,?,?,?,?,?,?)`,
    ['Anna Young', 22, 168, 'fashion', 1, 0, 0]
  );
  await run(
    `INSERT INTO models (name, age, height, category, available, featured, archived)
     VALUES (?,?,?,?,?,?,?)`,
    ['Bella Mid', 25, 175, 'fashion', 1, 1, 0]
  );
  await run(
    `INSERT INTO models (name, age, height, category, available, featured, archived)
     VALUES (?,?,?,?,?,?,?)`,
    ['Cara Tall', 28, 182, 'events', 1, 0, 0]
  );
  await run(
    `INSERT INTO models (name, age, height, category, available, featured, archived)
     VALUES (?,?,?,?,?,?,?)`,
    ['Diana Senior', 35, 170, 'events', 0, 0, 0]
  );

  // Seed a review for admin reviews tests
  await run(`INSERT INTO reviews (client_name, rating, text, approved) VALUES (?,?,?,?)`, [
    'Test Reviewer',
    5,
    'Great service wave113 test',
    0,
  ]);

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

  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
}, 20000);

// ── 1. Catalog Filters — GET /api/models ─────────────────────────────────────

describe('Catalog filters — GET /api/models', () => {
  it('returns 200 with no filters', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('height_min filter excludes models below threshold', async () => {
    const res = await request(app).get('/api/models?height_min=170');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    res.body.forEach(m => expect(m.height).toBeGreaterThanOrEqual(170));
  });

  it('height_max filter excludes models above threshold', async () => {
    const res = await request(app).get('/api/models?height_max=175');
    expect(res.status).toBe(200);
    res.body.forEach(m => expect(m.height).toBeLessThanOrEqual(175));
  });

  it('height_min + height_max together narrow results correctly', async () => {
    const res = await request(app).get('/api/models?height_min=170&height_max=180');
    expect(res.status).toBe(200);
    res.body.forEach(m => {
      expect(m.height).toBeGreaterThanOrEqual(170);
      expect(m.height).toBeLessThanOrEqual(180);
    });
  });

  it('age_min filter excludes models younger than threshold', async () => {
    const res = await request(app).get('/api/models?age_min=25');
    expect(res.status).toBe(200);
    res.body.forEach(m => expect(m.age).toBeGreaterThanOrEqual(25));
  });

  it('age_max filter excludes models older than threshold', async () => {
    const res = await request(app).get('/api/models?age_max=25');
    expect(res.status).toBe(200);
    res.body.forEach(m => expect(m.age).toBeLessThanOrEqual(25));
  });

  it('age_min + age_max together filter the age range', async () => {
    const res = await request(app).get('/api/models?age_min=20&age_max=30');
    expect(res.status).toBe(200);
    res.body.forEach(m => {
      expect(m.age).toBeGreaterThanOrEqual(20);
      expect(m.age).toBeLessThanOrEqual(30);
    });
  });

  it('sort=featured returns array (featured first)', async () => {
    const res = await request(app).get('/api/models?sort=featured');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // If there are featured models they should appear before non-featured
    if (res.body.length > 1) {
      const featuredIdx = res.body.findIndex(m => m.featured === 1);
      const nonFeaturedIdx = res.body.findIndex(m => m.featured === 0);
      if (featuredIdx !== -1 && nonFeaturedIdx !== -1) {
        expect(featuredIdx).toBeLessThan(nonFeaturedIdx);
      }
    }
  });

  it('sort=name returns models sorted alphabetically', async () => {
    const res = await request(app).get('/api/models?sort=name');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // Verify ascending alpha order
    for (let i = 1; i < res.body.length; i++) {
      expect(res.body[i].name.localeCompare(res.body[i - 1].name)).toBeGreaterThanOrEqual(0);
    }
  });

  it('each model in response has required fields', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
    res.body.forEach(m => {
      expect(m).toHaveProperty('id');
      expect(m).toHaveProperty('name');
      expect(m).toHaveProperty('age');
      expect(m).toHaveProperty('height');
    });
  });

  it('min_height alias works the same as height_min', async () => {
    const res1 = await request(app).get('/api/models?height_min=170');
    const res2 = await request(app).get('/api/models?min_height=170');
    expect(res1.status).toBe(200);
    expect(res2.status).toBe(200);
    expect(res1.body.length).toBe(res2.body.length);
  });
});

// ── 2. Settings public API — GET /api/settings/public ────────────────────────

describe('Settings public API — GET /api/settings/public', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
  });

  it('response is an object', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    expect(typeof res.body).toBe('object');
    expect(Array.isArray(res.body)).toBe(false);
  });

  it('does not expose JWT_SECRET', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    const keys = Object.keys(res.body);
    expect(keys).not.toContain('JWT_SECRET');
    expect(keys).not.toContain('jwt_secret');
  });

  it('does not expose ADMIN_PASSWORD', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    const keys = Object.keys(res.body);
    expect(keys).not.toContain('ADMIN_PASSWORD');
    expect(keys).not.toContain('admin_password');
  });

  it('does not expose BOT_TOKEN', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    const keys = Object.keys(res.body);
    expect(keys).not.toContain('BOT_TOKEN');
    expect(keys).not.toContain('bot_token');
  });

  it('if contacts_phone is present it is a string', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    if (Object.prototype.hasOwnProperty.call(res.body, 'contacts_phone')) {
      expect(typeof res.body.contacts_phone).toBe('string');
    }
  });

  it('if contacts_email is present it is a string', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    if (Object.prototype.hasOwnProperty.call(res.body, 'contacts_email')) {
      expect(typeof res.body.contacts_email).toBe('string');
    }
  });

  it('if manager_hours is present it is a string', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    if (Object.prototype.hasOwnProperty.call(res.body, 'manager_hours')) {
      expect(typeof res.body.manager_hours).toBe('string');
    }
  });
});

// ── 3. Admin review management ────────────────────────────────────────────────

describe('Admin reviews — GET /api/admin/reviews', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/reviews');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/reviews').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has reviews array and total field', async () => {
    const res = await request(app).get('/api/admin/reviews').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews)).toBe(true);
    expect(typeof res.body.total).toBe('number');
  });

  it('filter=pending returns only unapproved reviews', async () => {
    const res = await request(app)
      .get('/api/admin/reviews?filter=pending')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews)).toBe(true);
    res.body.reviews.forEach(r => expect(r.approved).toBe(0));
  });

  it('filter=approved returns only approved reviews', async () => {
    const res = await request(app)
      .get('/api/admin/reviews?filter=approved')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    res.body.reviews.forEach(r => expect(r.approved).toBe(1));
  });
});

describe('Admin reviews — PUT /api/admin/reviews/:id/approve', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).put('/api/admin/reviews/1/approve');
    expect(res.status).toBe(401);
  });
});

describe('Admin reviews — PATCH /api/admin/reviews/:id/reject', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).patch('/api/admin/reviews/1/reject');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth and toggles review to rejected', async () => {
    // First find the seeded review id
    const listRes = await request(app)
      .get('/api/admin/reviews?filter=pending')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(listRes.status).toBe(200);
    const reviews = listRes.body.reviews;
    if (reviews.length === 0) return; // no pending reviews to test

    const reviewId = reviews[0].id;
    const res = await request(app)
      .patch(`/api/admin/reviews/${reviewId}/reject`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.approved).toBe(0);
  });
});

describe('Admin reviews — PATCH /api/admin/reviews/:id/approve', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).patch('/api/admin/reviews/1/approve');
    expect(res.status).toBe(401);
  });
});

// ── 4. Factory status endpoint ────────────────────────────────────────────────

describe('Factory status — GET /api/admin/factory/status', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/factory/status');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid auth', async () => {
    const res = await request(app).get('/api/admin/factory/status').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has a status field', async () => {
    const res = await request(app).get('/api/admin/factory/status').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('status');
    expect(typeof res.body.status).toBe('string');
  });

  it('response has an available field (boolean)', async () => {
    const res = await request(app).get('/api/admin/factory/status').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('available');
    expect(typeof res.body.available).toBe('boolean');
  });

  it('status is one of known values (ok, unavailable, error)', async () => {
    const res = await request(app).get('/api/admin/factory/status').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(['ok', 'unavailable', 'error']).toContain(res.body.status);
  });
});
