'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');
const apiCode = fs.readFileSync(path.join(ROOT, 'routes/api.js'), 'utf8');

describe('T1: Factory webhook endpoint', () => {
  test('T01: /api/factory/cycle-complete endpoint exists', () => {
    expect(apiCode).toMatch(/cycle-complete|cycleComplete/);
  });
  test('T02: cycle-complete validates authorization', () => {
    const idx =
      apiCode.indexOf('cycle-complete') !== -1 ? apiCode.indexOf('cycle-complete') : apiCode.indexOf('cycleComplete');
    const nearby = apiCode.slice(Math.max(0, idx - 100), idx + 500);
    expect(nearby).toMatch(/auth|secret|jwt|JWT|authorization/i);
  });
  test('T03: cycle-complete calls notifyAdmin', () => {
    const idx =
      apiCode.indexOf('cycle-complete') !== -1 ? apiCode.indexOf('cycle-complete') : apiCode.indexOf('cycleComplete');
    // The handler body extends ~2100 chars from the route declaration
    const nearby = apiCode.slice(Math.max(0, idx), idx + 2200);
    expect(nearby).toMatch(/notifyAdmin/);
  });
});

describe('T2: Factory run → bot notification wiring', () => {
  test('T04: /admin/factory/run notifies admin on trigger from api.js', () => {
    const idx = apiCode.indexOf('/admin/factory/run');
    const nearby = apiCode.slice(Math.max(0, idx), idx + 600);
    expect(nearby).toMatch(/notifyAdmin/);
  });
  test('T05: bot adm_factory_run handler notifies admin on start', () => {
    const idx = botCode.indexOf('adm_factory_run');
    const nearby = botCode.slice(Math.max(0, idx), idx + 800);
    expect(nearby).toMatch(/notifyAdmin/);
  });
  test('T06: cycle-complete endpoint accepts x-factory-secret header', () => {
    const idx =
      apiCode.indexOf('cycle-complete') !== -1 ? apiCode.indexOf('cycle-complete') : apiCode.indexOf('cycleComplete');
    const nearby = apiCode.slice(Math.max(0, idx), idx + 800);
    expect(nearby).toMatch(/x-factory-secret|FACTORY_WEBHOOK_SECRET/);
  });
  test('T07: cycle-complete formats MarkdownV2 notification', () => {
    const idx =
      apiCode.indexOf('cycle-complete') !== -1 ? apiCode.indexOf('cycle-complete') : apiCode.indexOf('cycleComplete');
    const nearby = apiCode.slice(Math.max(0, idx), idx + 1500);
    expect(nearby).toMatch(/MarkdownV2/);
  });
});
