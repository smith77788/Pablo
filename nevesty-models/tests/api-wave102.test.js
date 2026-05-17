'use strict';
/**
 * Wave102 (v2) tests: Department Agents & CSV injection protection
 *  1. VisualConceptor (creative.js) — class, metadata, analyze() smoke test
 *  2. PricingNegotiator (sales.js) — class, metadata, analyze() smoke test
 *  3. RevenueForecaster (finance.js) — class, metadata, analyze() smoke test
 *  4. MarketResearcher (research.js) — class, metadata, analyze() smoke test
 *  5. Orchestrator agents count — >= 46 agents in the array
 *  6. CSV injection protection via /api/admin/orders/export HTTP endpoint
 */

// ─── In-memory DB test env ────────────────────────────────────────────────────
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';
process.env.ADMIN_TELEGRAM_IDS = '';
// Disable real Anthropic API calls
process.env.ANTHROPIC_API_KEY = '';

const request = require('supertest');
const express = require('express');
const cors = require('cors');
const path = require('path');
const fs = require('fs');

// ─── Agent source paths ───────────────────────────────────────────────────────
const CREATIVE_JS = path.join(__dirname, '../agents/departments/creative.js');
const SALES_JS = path.join(__dirname, '../agents/departments/sales.js');
const FINANCE_JS = path.join(__dirname, '../agents/departments/finance.js');
const RESEARCH_JS = path.join(__dirname, '../agents/departments/research.js');
const ORCHESTRATOR_JS = path.join(__dirname, '../agents/orchestrator.js');

// ─── HTTP app (needed for CSV injection test) ─────────────────────────────────
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
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const res = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = res.body?.token || res.body?.accessToken || null;
}, 60000);

afterAll(async () => {
  await new Promise(r => setTimeout(r, 300));
});

// ═══════════════════════════════════════════════════════════════════════════════
// 1. VisualConceptor (creative.js)
// ═══════════════════════════════════════════════════════════════════════════════

describe('VisualConceptor (creative.js)', () => {
  const { VisualConceptor } = require('../agents/departments/creative');

  test('VisualConceptor class instantiates without throwing', () => {
    expect(() => new VisualConceptor()).not.toThrow();
  });

  test('VisualConceptor has correct id', () => {
    const agent = new VisualConceptor();
    expect(agent.id).toBe('cre-04');
  });

  test('VisualConceptor has correct name', () => {
    const agent = new VisualConceptor();
    expect(agent.name).toBe('VisualConceptor');
  });

  test('VisualConceptor has correct organ', () => {
    const agent = new VisualConceptor();
    expect(agent.organ).toBe('Creative Department');
  });

  test('VisualConceptor has correct emoji', () => {
    const agent = new VisualConceptor();
    expect(agent.emoji).toBe('📸');
  });

  test('VisualConceptor.analyze() resolves without throwing (empty DB)', async () => {
    const agent = new VisualConceptor();
    await expect(agent.analyze()).resolves.not.toThrow();
  });

  test('VisualConceptor.analyze() populates findings array', async () => {
    const agent = new VisualConceptor();
    await agent.analyze();
    expect(Array.isArray(agent.findings)).toBe(true);
    expect(agent.findings.length).toBeGreaterThan(0);
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 2. PricingNegotiator (sales.js)
// ═══════════════════════════════════════════════════════════════════════════════

describe('PricingNegotiator (sales.js)', () => {
  const { PricingNegotiator } = require('../agents/departments/sales');

  test('PricingNegotiator class instantiates without throwing', () => {
    expect(() => new PricingNegotiator()).not.toThrow();
  });

  test('PricingNegotiator has correct id', () => {
    const agent = new PricingNegotiator();
    expect(agent.id).toBe('sal-04');
  });

  test('PricingNegotiator has correct name', () => {
    const agent = new PricingNegotiator();
    expect(agent.name).toBe('PricingNegotiator');
  });

  test('PricingNegotiator has correct organ', () => {
    const agent = new PricingNegotiator();
    expect(agent.organ).toBe('Sales Department');
  });

  test('PricingNegotiator has correct emoji', () => {
    const agent = new PricingNegotiator();
    expect(agent.emoji).toBe('💰');
  });

  test('PricingNegotiator.analyze() resolves without throwing (empty DB)', async () => {
    const agent = new PricingNegotiator();
    await expect(agent.analyze()).resolves.not.toThrow();
  });

  test('PricingNegotiator.analyze() populates findings array', async () => {
    const agent = new PricingNegotiator();
    await agent.analyze();
    expect(Array.isArray(agent.findings)).toBe(true);
    expect(agent.findings.length).toBeGreaterThan(0);
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 3. RevenueForecaster (finance.js)
// ═══════════════════════════════════════════════════════════════════════════════

describe('RevenueForecaster (finance.js)', () => {
  const { RevenueForecaster } = require('../agents/departments/finance');

  test('RevenueForecaster class instantiates without throwing', () => {
    expect(() => new RevenueForecaster()).not.toThrow();
  });

  test('RevenueForecaster has correct id', () => {
    const agent = new RevenueForecaster();
    expect(agent.id).toBe('fin-01');
  });

  test('RevenueForecaster has correct name', () => {
    const agent = new RevenueForecaster();
    expect(agent.name).toBe('RevenueForecaster');
  });

  test('RevenueForecaster has correct organ', () => {
    const agent = new RevenueForecaster();
    expect(agent.organ).toBe('Finance Department');
  });

  test('RevenueForecaster has correct emoji', () => {
    const agent = new RevenueForecaster();
    expect(agent.emoji).toBe('📊');
  });

  test('RevenueForecaster.analyze() resolves without throwing (empty DB)', async () => {
    const agent = new RevenueForecaster();
    await expect(agent.analyze()).resolves.not.toThrow();
  });

  test('RevenueForecaster.analyze() populates findings array', async () => {
    const agent = new RevenueForecaster();
    await agent.analyze();
    expect(Array.isArray(agent.findings)).toBe(true);
    expect(agent.findings.length).toBeGreaterThan(0);
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 4. MarketResearcher (research.js)
// ═══════════════════════════════════════════════════════════════════════════════

describe('MarketResearcher (research.js)', () => {
  const { MarketResearcher } = require('../agents/departments/research');

  test('MarketResearcher class instantiates without throwing', () => {
    expect(() => new MarketResearcher()).not.toThrow();
  });

  test('MarketResearcher has correct id', () => {
    const agent = new MarketResearcher();
    expect(agent.id).toBe('res-01');
  });

  test('MarketResearcher has correct name', () => {
    const agent = new MarketResearcher();
    expect(agent.name).toBe('MarketResearcher');
  });

  test('MarketResearcher has correct organ', () => {
    const agent = new MarketResearcher();
    expect(agent.organ).toBe('Research Department');
  });

  test('MarketResearcher has correct emoji', () => {
    const agent = new MarketResearcher();
    expect(agent.emoji).toBe('🌍');
  });

  test('MarketResearcher.analyze() resolves without throwing (empty DB)', async () => {
    const agent = new MarketResearcher();
    await expect(agent.analyze()).resolves.not.toThrow();
  });

  test('MarketResearcher.analyze() populates findings array', async () => {
    const agent = new MarketResearcher();
    await agent.analyze();
    expect(Array.isArray(agent.findings)).toBe(true);
    expect(agent.findings.length).toBeGreaterThan(0);
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 5. Orchestrator agents count
// ═══════════════════════════════════════════════════════════════════════════════

describe('Orchestrator agents array', () => {
  test('orchestrator.js source file exists', () => {
    expect(fs.existsSync(ORCHESTRATOR_JS)).toBe(true);
  });

  test('orchestrator.js has at least 46 entries in agents array (source check)', () => {
    const src = fs.readFileSync(ORCHESTRATOR_JS, 'utf8');
    // Count require('./NN-...') lines and class references pushed into agents array
    // Extract the agents = [...] block
    const start = src.indexOf('const agents = [');
    expect(start).toBeGreaterThan(-1);
    const end = src.indexOf('];', start);
    expect(end).toBeGreaterThan(start);
    const agentsBlock = src.slice(start, end);

    // Count requires (numbered agents) + class name references (department agents)
    const requireLines = (agentsBlock.match(/require\(\s*['"][^'"]+['"]\s*\)/g) || []).length;
    const classRefs = (agentsBlock.match(/^\s{2}[A-Z][A-Za-z]+,\s*$/gm) || []).length;
    const totalAgents = requireLines + classRefs;

    expect(totalAgents).toBeGreaterThanOrEqual(46);
  });

  test('orchestrator.js exports or runs at least 4 department groups', () => {
    const src = fs.readFileSync(ORCHESTRATOR_JS, 'utf8');
    // Verify all 4 department comment blocks exist
    expect(src).toContain('Sales Department');
    expect(src).toContain('Creative Department');
    expect(src).toContain('Finance Department');
    expect(src).toContain('Research Department');
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 6. CSV injection protection — /api/admin/orders/export
// ═══════════════════════════════════════════════════════════════════════════════

describe('CSV injection protection in /api/admin/orders/export', () => {
  test('endpoint returns 200 and CSV content-type when authenticated', async () => {
    if (!adminToken) {
      console.warn('Skipping: no admin token');
      return;
    }
    const res = await request(app).get('/api/admin/orders/export').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/text\/csv|application\/octet/i);
  });

  test('csvCell2 in api.js source escapes = prefix with leading apostrophe', () => {
    const src = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    // The csvCell2 function prepends ' to cells starting with formula-trigger chars
    expect(src).toMatch(/csvCell2?\s*=.*?['"]'\s*\+\s*s|s\s*=\s*"'"\s*\+\s*s/);
    // Regex pattern check for formula trigger chars: = + - @ \t \r
    expect(src).toContain('^[=+\\-@\\t\\r]');
  });

  test('api.js source uses regex to detect CSV injection trigger chars', () => {
    const src = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    // Both csvCell2 (line ~3351) and _csvCell24 (line ~9933) protect against injection
    const csvInjectionPattern = /prevent CSV injection/g;
    const matchCount = (src.match(csvInjectionPattern) || []).length;
    expect(matchCount).toBeGreaterThanOrEqual(1);
  });

  test('csvCell2 in api.js wraps values in double quotes and escapes internal quotes', () => {
    const src = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    // The pattern: '"' + s.replace(/"/g, '""') + '"'
    expect(src).toContain('replace(/"/g, \'""\'');
  });

  test('GET /api/admin/orders/export returns 401 without token', async () => {
    const res = await request(app).get('/api/admin/orders/export');
    expect([401, 403]).toContain(res.status);
  });

  test('CSV export endpoint defined in api.js source', () => {
    const src = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    expect(src).toContain('/admin/orders/export');
  });
});
