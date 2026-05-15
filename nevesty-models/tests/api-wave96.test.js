'use strict';

// ── Env setup BEFORE any app module is loaded ────────────────────────────────
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const fs = require('fs');
const path = require('path');
const request = require('supertest');
const express = require('express');
const cors = require('cors');

const ROOT = path.join(__dirname, '..');

let app;

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

  // Mount health endpoint (mirrors server.js)
  const buildHealthResponse = async () => {
    const os = require('os');
    const { get: dbGet } = require('../database');
    let dbStatus = 'ok';
    let dbError = null;
    try {
      await dbGet('SELECT 1 as ok');
    } catch (e) {
      dbStatus = 'error';
      dbError = e.message;
    }
    const memUsed = process.memoryUsage();
    const memMb = Math.round(memUsed.rss / 1024 / 1024);
    const heapUsedMb = Math.round(memUsed.heapUsed / 1024 / 1024);
    const uptime = Math.floor(process.uptime());
    const overallStatus = dbStatus === 'ok' ? 'ok' : 'degraded';

    return {
      status: overallStatus,
      uptime_sec: uptime,
      memory_mb: memMb,
      memory: { rss_mb: memMb, heap_used_mb: heapUsedMb },
      database: dbStatus === 'ok' ? { status: 'ok' } : { status: 'error', error: dbError },
      db: dbStatus === 'ok' ? 'ok' : 'error',
    };
  };

  a.get('/api/health', async (req, res) => {
    try {
      const health = await buildHealthResponse();
      res.status(health.status === 'ok' ? 200 : 503).json(health);
    } catch (e) {
      res.status(503).json({ status: 'down', error: e.message });
    }
  });

  a.use('/api', apiRouter);
  a.use((err, req, res, _next) => res.status(500).json({ error: err.message }));
  app = a;
}, 15000);

// ─── T1: Health endpoint ──────────────────────────────────────────────────────

describe('T1: Health endpoint', () => {
  test('T01: GET /api/health returns 200 with JSON', async () => {
    const res = await request(app).get('/api/health');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/json/);
  });

  test('T02: Response contains status field ("ok" or "degraded")', async () => {
    const res = await request(app).get('/api/health');
    expect(res.body).toHaveProperty('status');
    expect(['ok', 'degraded']).toContain(res.body.status);
  });

  test('T03: Response contains uptime_sec as a number', async () => {
    const res = await request(app).get('/api/health');
    expect(res.body).toHaveProperty('uptime_sec');
    expect(typeof res.body.uptime_sec).toBe('number');
    expect(res.body.uptime_sec).toBeGreaterThanOrEqual(0);
  });

  test('T04: Response contains memory_mb as a number', async () => {
    const res = await request(app).get('/api/health');
    expect(res.body).toHaveProperty('memory_mb');
    expect(typeof res.body.memory_mb).toBe('number');
    expect(res.body.memory_mb).toBeGreaterThan(0);
  });

  test('T05: Response contains db field ("ok" or "error")', async () => {
    const res = await request(app).get('/api/health');
    expect(res.body).toHaveProperty('db');
    expect(['ok', 'error']).toContain(res.body.db);
  });
});

// ─── T2: check-factory.js script ──────────────────────────────────────────────

describe('T2: check-factory.js script', () => {
  const checkFactoryPath = path.join(ROOT, 'tools', 'check-factory.js');
  let scriptCode = '';

  beforeAll(() => {
    if (fs.existsSync(checkFactoryPath)) {
      scriptCode = fs.readFileSync(checkFactoryPath, 'utf8');
    }
  });

  test('T06: Script file exists at tools/check-factory.js', () => {
    expect(fs.existsSync(checkFactoryPath)).toBe(true);
  });

  test('T07: Script contains a reference to FACTORY_LOG or last_run', () => {
    expect(scriptCode).toMatch(/FACTORY_LOG|last_run/);
  });

  test('T08: Script calls notify.js when factory is stale', () => {
    expect(scriptCode).toMatch(/notify\.js/);
  });

  test('T09: Script has proper shebang (#!/usr/bin/env node)', () => {
    expect(scriptCode.startsWith('#!/usr/bin/env node')).toBe(true);
  });
});

// ─── T3: Auto-backup in server.js ────────────────────────────────────────────

describe('T3: Auto-backup in server.js', () => {
  const serverPath = path.join(ROOT, 'server.js');
  let serverCode = '';

  beforeAll(() => {
    serverCode = fs.readFileSync(serverPath, 'utf8');
  });

  test('T10: server.js contains backup status tracking (backupStatus or BACKUP_DIR)', () => {
    expect(serverCode).toMatch(/backupStatus|BACKUP_DIR/);
  });

  test('T11: server.js contains setInterval for periodic monitoring', () => {
    expect(serverCode).toMatch(/setInterval/);
  });

  test('T12: server.js contains backup directory or backup file references', () => {
    expect(serverCode).toMatch(/backup.*dir|backupDir|backup_dir/i);
  });
});

// ─── T4: Sitemap generation ───────────────────────────────────────────────────

describe('T4: Sitemap generation', () => {
  const sitemapPath = path.join(ROOT, 'public', 'sitemap.xml');
  let sitemapContent = '';

  beforeAll(() => {
    if (fs.existsSync(sitemapPath)) {
      sitemapContent = fs.readFileSync(sitemapPath, 'utf8');
    }
  });

  test('T13: public/sitemap.xml exists', () => {
    expect(fs.existsSync(sitemapPath)).toBe(true);
  });

  test('T14: sitemap.xml contains <urlset', () => {
    expect(sitemapContent).toMatch(/<urlset/);
  });

  test('T15: sitemap.xml contains model URLs (e.g., /model/)', () => {
    expect(sitemapContent).toMatch(/\/model\//);
  });

  test('T16: sitemap.xml contains static pages (/ and /catalog.html)', () => {
    expect(sitemapContent).toMatch(/<loc>[^<]*\/<\/loc>/);
    expect(sitemapContent).toMatch(/catalog\.html/);
  });
});

// ─── T5: strings.js ──────────────────────────────────────────────────────────

describe('T5: strings.js', () => {
  let STRINGS;

  beforeAll(() => {
    STRINGS = require('../strings');
  });

  test('T17: strings.js exports an object (STRINGS or default export)', () => {
    expect(typeof STRINGS).toBe('object');
    expect(STRINGS).not.toBeNull();
  });

  test('T18: Object has at least 200 keys', () => {
    const keys = Object.keys(STRINGS).filter(k => k !== 'getString');
    expect(keys.length).toBeGreaterThanOrEqual(200);
  });

  test('T19: Keys include "wishlistTitle" and "faqTitle"', () => {
    expect(STRINGS).toHaveProperty('wishlistTitle');
    expect(STRINGS).toHaveProperty('faqTitle');
  });
});
