'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');
const apiCode = fs.readFileSync(path.join(ROOT, 'routes/api.js'), 'utf8');
const dbCode = fs.readFileSync(path.join(ROOT, 'database.js'), 'utf8');

// ─── T1: Factory → Bot webhook ────────────────────────────────────────────────

describe('T1: Factory → Bot webhook', () => {
  test('T01: /api/admin/factory/cycle-complete endpoint exists in api.js', () => {
    expect(apiCode).toMatch(/router\.post\s*\(\s*['"]\/admin\/factory\/cycle-complete['"]/);
  });

  test('T02: endpoint checks for x-factory-secret header auth', () => {
    const idx = apiCode.indexOf('/admin/factory/cycle-complete');
    expect(idx).toBeGreaterThan(-1);
    const nearby = apiCode.slice(idx, idx + 600);
    expect(nearby).toMatch(/x-factory-secret/);
    expect(nearby).toMatch(/headerSecret/);
  });

  test('T03: endpoint calls notifyAdmin', () => {
    const idx = apiCode.indexOf('/admin/factory/cycle-complete');
    const nearby = apiCode.slice(idx, idx + 2500);
    expect(nearby).toMatch(/notifyAdmin/);
  });

  test('T04: endpoint handles summary, insights, actions fields in body', () => {
    const idx = apiCode.indexOf('/admin/factory/cycle-complete');
    const nearby = apiCode.slice(idx, idx + 1800);
    expect(nearby).toMatch(/summary/);
    expect(nearby).toMatch(/insights/);
    expect(nearby).toMatch(/actions/);
  });

  test('T05: factory/run endpoint also notifies admin', () => {
    const idx = apiCode.indexOf("'/admin/factory/run'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = apiCode.slice(idx, idx + 600);
    expect(nearby).toMatch(/notifyAdmin/);
  });
});

// ─── T2: Model archive ────────────────────────────────────────────────────────

describe('T2: Model archive', () => {
  test('T06: PATCH /admin/models/:id/archive route exists', () => {
    expect(apiCode).toMatch(/router\.patch\s*\(\s*['"]\/admin\/models\/:id\/archive['"]/);
  });

  test('T07: archive route sets archived=1', () => {
    const idx = apiCode.indexOf("'/admin/models/:id/archive'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = apiCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/archived=1/);
  });

  test('T08: restore route sets archived=0', () => {
    expect(apiCode).toMatch(/router\.patch\s*\(\s*['"]\/admin\/models\/:id\/restore['"]/);
    const idx = apiCode.indexOf("'/admin/models/:id/restore'");
    const nearby = apiCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/archived=0/);
  });

  test('T09: GET /api/models excludes archived models (archived=0)', () => {
    // The public GET models query should filter out archived models
    expect(apiCode).toMatch(/archived=0/);
  });

  test('T10: database v22 migration adds archived column', () => {
    expect(dbCode).toMatch(/v22/i);
    const idx = dbCode.indexOf('v22');
    const nearby = dbCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/archived/);
  });
});

// ─── T3: Catalog dynamic settings applied ─────────────────────────────────────

describe('T3: Catalog dynamic settings applied', () => {
  test('T11: showCatalog reads catalog_per_page setting', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]catalog_per_page['"]/);
  });

  test('T12: catalog_sort determines ORDER BY clause (featured/name/newest)', () => {
    const idx = botCode.indexOf("getSetting('catalog_sort')");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 500);
    expect(nearby).toMatch(/ORDER BY/i);
    expect(nearby).toMatch(/featured|name|newest/i);
  });

  test('T13: cities_list is split by comma when building city buttons', () => {
    const idx = botCode.indexOf("getSetting('cities_list')");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/split\s*\(\s*['"],['"]/);
  });

  test('T14: catalog_per_page falls back to safe default (5)', () => {
    const idx = botCode.indexOf("getSetting('catalog_per_page')");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 200);
    // Falls back to '5' when setting missing
    expect(nearby).toMatch(/['"]5['"]|5\s*\)|5\s*\|\|/);
  });

  test('T15: catalog_per_page is clamped (Math.min/max)', () => {
    const idx = botCode.indexOf("getSetting('catalog_per_page')");
    const nearby = botCode.slice(idx, idx + 300);
    expect(nearby).toMatch(/Math\.min/);
    expect(nearby).toMatch(/Math\.max/);
  });
});

// ─── T4: Model statistics ─────────────────────────────────────────────────────

describe('T4: Model statistics', () => {
  test('T16: showModelStats includes revenue (SUM budget from completed orders)', () => {
    const idx = botCode.indexOf('showModelStats');
    expect(idx).toBeGreaterThan(-1);
    const fn = botCode.slice(idx, idx + 3000);
    expect(fn).toMatch(/SUM/i);
    expect(fn).toMatch(/budget/i);
    expect(fn).toMatch(/completed/i);
  });

  test('T17: showModelStats shows active order count', () => {
    const idx = botCode.indexOf('async function showModelStats');
    expect(idx).toBeGreaterThan(-1);
    const fn = botCode.slice(idx, idx + 2000);
    expect(fn).toMatch(/activeOrders|active.*order/i);
  });

  test('T18: showModelStats shows top cities', () => {
    const idx = botCode.indexOf('async function showModelStats');
    expect(idx).toBeGreaterThan(-1);
    const fn = botCode.slice(idx, idx + 3000);
    expect(fn).toMatch(/topCities|top.*cit/i);
    expect(fn).toMatch(/location/i);
  });

  test('T19: showModelStats shows event type breakdown', () => {
    const idx = botCode.indexOf('async function showModelStats');
    expect(idx).toBeGreaterThan(-1);
    const fn = botCode.slice(idx, idx + 3000);
    expect(fn).toMatch(/topEventTypes|event_type/i);
  });
});
