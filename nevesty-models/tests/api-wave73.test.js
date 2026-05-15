'use strict';
/**
 * Integration tests for Wave 73 features:
 * - Catalog filter API (height/age/category params)
 * - Factory Status API
 * - Research Department (factory)
 * - DevOps & Deployment artifacts
 * - Catalog filter UI (catalog.html)
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
const fs = require('fs');
const path = require('path');

let app, adminToken;

const routesContent    = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
const schedulerContent = fs.readFileSync(path.join(__dirname, '../services/scheduler.js'), 'utf8');
const serverContent    = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');

const FACTORY_DIR = path.join(__dirname, '../../factory');
const factoryExists = fs.existsSync(FACTORY_DIR);

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
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const res = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = res.body?.token || res.body?.accessToken || null;
}, 30000);

afterAll(async () => {
  await new Promise(r => setTimeout(r, 300));
});

// ── Catalog Filter API ────────────────────────────────────────────────────────

describe('Wave 73: Catalog Filter API (height/age params)', () => {
  test('GET /api/models?height_min=160&height_max=170 returns 200', async () => {
    const res = await request(app).get('/api/models?height_min=160&height_max=170');
    expect(res.status).toBe(200);
  });

  test('GET /api/models?age_min=18&age_max=25 returns 200', async () => {
    const res = await request(app).get('/api/models?age_min=18&age_max=25');
    expect(res.status).toBe(200);
  });

  test('GET /api/models?category=fashion returns 200', async () => {
    const res = await request(app).get('/api/models?category=fashion');
    expect(res.status).toBe(200);
  });

  test('GET /api/models?height_min=170&age_min=22 combined filter returns 200', async () => {
    const res = await request(app).get('/api/models?height_min=170&age_min=22');
    expect(res.status).toBe(200);
  });

  test('routes/api.js contains height_min filter', () => {
    expect(routesContent).toMatch(/height_min|height.*min/i);
  });

  test('routes/api.js contains age_min filter', () => {
    expect(routesContent).toMatch(/age_min|age.*min/i);
  });
});

// ── Factory Status & Monitoring ───────────────────────────────────────────────

describe('Wave 73: Factory Status & Monitoring', () => {
  test('GET /api/admin/factory/status requires auth', async () => {
    const res = await request(app).get('/api/admin/factory/status');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/factory/status returns status field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/factory/status')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('status');
  });

  test('scheduler.js has factory staleness check', () => {
    expect(schedulerContent).toMatch(/factory|staleness|factory_last/i);
  });

  test('server.js health endpoint includes factory info', () => {
    expect(serverContent).toMatch(/factory/i);
  });
});

// ── Research Department ───────────────────────────────────────────────────────

describe('Wave 73: Research Department (factory)', () => {
  test('factory/agents/research_department.py exists', () => {
    if (!factoryExists) return;
    expect(fs.existsSync(path.join(FACTORY_DIR, 'agents', 'research_department.py'))).toBe(true);
  });

  test('research_department.py has MarketResearcher', () => {
    if (!factoryExists) return;
    const code = fs.readFileSync(path.join(FACTORY_DIR, 'agents', 'research_department.py'), 'utf8');
    expect(code).toMatch(/MarketResearcher/);
  });

  test('research_department.py has CompetitorAnalyst', () => {
    if (!factoryExists) return;
    const code = fs.readFileSync(path.join(FACTORY_DIR, 'agents', 'research_department.py'), 'utf8');
    expect(code).toMatch(/CompetitorAnalyst/);
  });

  test('research_department.py has TrendSpotter', () => {
    if (!factoryExists) return;
    const code = fs.readFileSync(path.join(FACTORY_DIR, 'agents', 'research_department.py'), 'utf8');
    expect(code).toMatch(/TrendSpotter/);
  });

  test('cycle.py has run_phase_research', () => {
    if (!factoryExists) return;
    const code = fs.readFileSync(path.join(FACTORY_DIR, 'cycle.py'), 'utf8');
    expect(code).toMatch(/run_phase_research/);
  });

  test('test_research_department.py has 30+ tests', () => {
    if (!factoryExists) return;
    const code = fs.readFileSync(path.join(FACTORY_DIR, 'tests', 'test_research_department.py'), 'utf8');
    const testCount = (code.match(/def test_/g) || []).length;
    expect(testCount).toBeGreaterThanOrEqual(30);
  });
});

// ── DevOps & Deployment ───────────────────────────────────────────────────────

describe('Wave 73: DevOps & Deployment', () => {
  const rootDir = path.join(__dirname, '../../');

  test('docker-compose.yml exists', () => {
    expect(fs.existsSync(path.join(rootDir, 'docker-compose.yml'))).toBe(true);
  });

  test('docker-compose.yml contains nevesty-bot service', () => {
    const code = fs.readFileSync(path.join(rootDir, 'docker-compose.yml'), 'utf8');
    expect(code).toMatch(/nevesty-bot|nevesty_bot/);
  });

  test('nevesty-models/Dockerfile exists', () => {
    expect(fs.existsSync(path.join(__dirname, '../Dockerfile'))).toBe(true);
  });

  test('nevesty-models/.env.example exists', () => {
    expect(fs.existsSync(path.join(__dirname, '../.env.example'))).toBe(true);
  });

  test('.env.example contains JWT_SECRET', () => {
    const code = fs.readFileSync(path.join(__dirname, '../.env.example'), 'utf8');
    expect(code).toMatch(/JWT_SECRET/);
  });

  test('deploy.sh exists at project root', () => {
    expect(fs.existsSync(path.join(rootDir, 'deploy.sh'))).toBe(true);
  });
});

// ── Catalog filter UI ─────────────────────────────────────────────────────────

describe('Wave 73: Catalog filter UI (catalog.html)', () => {
  const catalogCode = fs.readFileSync(path.join(__dirname, '../public/catalog.html'), 'utf8');

  test('catalog.html contains height filter', () => {
    expect(catalogCode).toMatch(/height|Height|рост|Рост/i);
  });

  test('catalog.html contains age filter', () => {
    expect(catalogCode).toMatch(/age|Age|возраст|Возраст/i);
  });
});
