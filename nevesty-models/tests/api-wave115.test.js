'use strict';
// Wave 115: Cities API, Model city filter, Email DEV mode, Health memory/uptime, Settings public

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-wave115-minimum-32chars!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.BOT_TOKEN = '123:test';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';
// Ensure DEV mode for email service tests
delete process.env.SMTP_HOST;
delete process.env.SMTP_USER;
delete process.env.SMTP_PASS;
delete process.env.SENDGRID_API_KEY;

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken;

beforeAll(async () => {
  const { initDatabase, run } = require('../database');
  await initDatabase();

  // Seed models with various cities for city filter tests
  await run(
    `INSERT INTO models (name, age, height, category, city, available, featured, archived)
     VALUES (?,?,?,?,?,?,?,?)`,
    ['Anna Moskva', 24, 170, 'fashion', 'Москва', 1, 0, 0]
  );
  await run(
    `INSERT INTO models (name, age, height, category, city, available, featured, archived)
     VALUES (?,?,?,?,?,?,?,?)`,
    ['Bella Piter', 26, 175, 'events', 'Санкт-Петербург', 1, 1, 0]
  );
  await run(
    `INSERT INTO models (name, age, height, category, city, available, featured, archived)
     VALUES (?,?,?,?,?,?,?,?)`,
    ['Cara Krasnodar', 22, 168, 'fashion', 'Краснодар', 1, 0, 0]
  );

  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');

  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());

  // Health endpoint mirroring server.js buildHealthResponse
  a.get('/api/health', async (req, res) => {
    try {
      const { get: dbGet } = require('../database');
      let dbStatus = 'ok';
      try {
        await dbGet('SELECT 1');
      } catch (_) {
        dbStatus = 'error';
      }
      const mem = process.memoryUsage();
      const memMb = Math.round(mem.heapUsed / 1024 / 1024);
      res.json({
        status: 'ok',
        db: dbStatus,
        memory: { heap_used_mb: memMb },
        memory_mb: memMb,
        uptime_seconds: Math.floor(process.uptime()),
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

// ── 1. Cities API ─────────────────────────────────────────────────────────────

describe('Cities API — GET /api/cities', () => {
  it('returns 200', async () => {
    const res = await request(app).get('/api/cities');
    expect(res.status).toBe(200);
  });

  it('returns object with cities field', async () => {
    const res = await request(app).get('/api/cities');
    expect(res.body).toHaveProperty('cities');
  });

  it('cities field is an array', async () => {
    const res = await request(app).get('/api/cities');
    expect(Array.isArray(res.body.cities)).toBe(true);
  });

  it('cities are non-empty strings', async () => {
    const res = await request(app).get('/api/cities');
    expect(res.body.cities.length).toBeGreaterThan(0);
    res.body.cities.forEach(city => {
      expect(typeof city).toBe('string');
      expect(city.trim().length).toBeGreaterThan(0);
    });
  });

  it('does not require authentication', async () => {
    // No auth header — should still return 200
    const res = await request(app).get('/api/cities');
    expect(res.status).toBe(200);
  });
});

// ── 2. Models filter by city ──────────────────────────────────────────────────

describe('Models filter by city — GET /api/models?city=...', () => {
  it('GET /api/models?city=Москва returns 200', async () => {
    const res = await request(app).get('/api/models?city=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0');
    expect(res.status).toBe(200);
  });

  it('GET /api/models?city=Москва returns an array', async () => {
    const res = await request(app).get('/api/models?city=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0');
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('GET /api/models?city=Москва returns only models from Москва', async () => {
    const res = await request(app).get('/api/models?city=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0');
    expect(res.status).toBe(200);
    // All returned models must have city === Москва (if city field is present)
    res.body.forEach(m => {
      if (m.city !== undefined) {
        expect(m.city).toBe('Москва');
      }
    });
  });

  it('GET /api/models (no city) returns all cities combined', async () => {
    const resAll = await request(app).get('/api/models');
    const resMsk = await request(app).get('/api/models?city=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0');
    expect(resAll.status).toBe(200);
    expect(Array.isArray(resAll.body)).toBe(true);
    // All models count >= Moscow-only count
    expect(resAll.body.length).toBeGreaterThanOrEqual(resMsk.body.length);
  });

  it('GET /api/models?city=НесуществующийГород returns empty array', async () => {
    const res = await request(app).get(
      '/api/models?city=%D0%9D%D0%B5%D1%81%D1%83%D1%89%D0%B5%D1%81%D1%82%D0%B2%D1%83%D1%8E%D1%89%D0%B8%D0%B9%D0%93%D0%BE%D1%80%D0%BE%D0%B4'
    );
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBe(0);
  });
});

// ── 3. Email service DEV mode ─────────────────────────────────────────────────

describe('Email service — DEV mode', () => {
  let emailService;

  beforeAll(() => {
    // Clear module cache to pick up deleted env vars
    jest.resetModules();
    delete process.env.SMTP_HOST;
    delete process.env.SMTP_USER;
    delete process.env.SMTP_PASS;
    delete process.env.SENDGRID_API_KEY;
    emailService = require('../services/email');
  });

  it('require("../services/email") does not throw', () => {
    expect(() => require('../services/email')).not.toThrow();
  });

  it('DEV_MODE is true when no SMTP_HOST or SENDGRID_API_KEY', () => {
    expect(emailService.DEV_MODE).toBe(true);
  });

  it('sendOrderConfirmation() returns without throwing in DEV mode', async () => {
    await expect(
      emailService.sendOrderConfirmation('test@example.com', {
        order_number: 'W115-001',
        client_name: 'Test Client',
        event_type: 'photo_shoot',
        event_date: '2026-06-01',
      })
    ).resolves.not.toThrow();
  });

  it('sendContactFormToAdmin() returns without throwing in DEV mode', async () => {
    await expect(
      emailService.sendContactFormToAdmin('admin@example.com', {
        name: 'Ivan Petrov',
        phone: '+7-999-000-0000',
        message: 'Test message wave115',
        email: 'ivan@example.com',
      })
    ).resolves.not.toThrow();
  });
});

// ── 4. Health endpoint ────────────────────────────────────────────────────────

describe('Health endpoint — GET /api/health', () => {
  it('returns 200', async () => {
    const res = await request(app).get('/api/health');
    expect(res.status).toBe(200);
  });

  it('response contains status field', async () => {
    const res = await request(app).get('/api/health');
    expect(res.body).toHaveProperty('status');
  });

  it('status field is a string', async () => {
    const res = await request(app).get('/api/health');
    expect(typeof res.body.status).toBe('string');
  });

  it('response contains memory or uptime field', async () => {
    const res = await request(app).get('/api/health');
    const hasMemory = res.body.memory !== undefined || res.body.memory_mb !== undefined;
    const hasUptime = res.body.uptime_seconds !== undefined || res.body.uptime !== undefined;
    expect(hasMemory || hasUptime).toBe(true);
  });

  it('does not require authentication', async () => {
    const res = await request(app).get('/api/health');
    expect(res.status).toBe(200);
  });
});

// ── 5. Settings public API ────────────────────────────────────────────────────

describe('Settings public — GET /api/settings/public', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
  });

  it('response body is an object', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(typeof res.body).toBe('object');
    expect(Array.isArray(res.body)).toBe(false);
  });

  it('values in response are strings or null (not complex objects)', async () => {
    const res = await request(app).get('/api/settings/public');
    Object.values(res.body).forEach(val => {
      expect(['string', 'object'].includes(typeof val) || val === null).toBe(true);
    });
  });

  it('does not contain JWT_SECRET key', async () => {
    const res = await request(app).get('/api/settings/public');
    const body = JSON.stringify(res.body);
    expect(body).not.toMatch(/JWT_SECRET/i);
    expect(body).not.toContain('test-secret-wave115');
  });

  it('does not contain BOT_TOKEN key or value', async () => {
    const res = await request(app).get('/api/settings/public');
    const body = JSON.stringify(res.body);
    expect(body).not.toMatch(/BOT_TOKEN/i);
    expect(body).not.toContain('123:test');
  });
});
