'use strict';
/**
 * Wave101 tests: client menu consolidation, CEO intelligence, experiment system,
 * admin sub-menu callbacks, strings.js coverage, health backup status.
 */

const fs = require('fs');
const path = require('path');

const botSrc = fs.readFileSync(path.join(__dirname, '..', 'bot.js'), 'utf8');
const ceoSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'factory', 'agents', 'strategic_core.py'), 'utf8');
const expSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'factory', 'agents', 'experiment_system.py'), 'utf8');
const serverSrc = fs.readFileSync(path.join(__dirname, '..', 'server.js'), 'utf8');

// ─── 1. Client keyboard consolidated (4 tests) ────────────────────────────────

describe('Client keyboard consolidated', () => {
  test('buildClientKeyboard function exists in bot.js', () => {
    expect(botSrc).toContain('buildClientKeyboard');
  });

  test('buildClientKeyboard does NOT contain cat_filter_fashion (category filters removed)', () => {
    // Extract the buildClientKeyboard function body
    const fnStart = botSrc.indexOf('async function buildClientKeyboard()');
    const fnEnd = botSrc.indexOf('\n}', fnStart) + 2;
    const fnBody = botSrc.slice(fnStart, fnEnd);
    expect(fnBody).not.toContain('cat_filter_fashion');
  });

  test('buildClientKeyboard does NOT contain search_height_input (height search merged into main search)', () => {
    const fnStart = botSrc.indexOf('async function buildClientKeyboard()');
    const fnEnd = botSrc.indexOf('\n}', fnStart) + 2;
    const fnBody = botSrc.slice(fnStart, fnEnd);
    expect(fnBody).not.toContain('search_height_input');
  });

  test('techspec_start is NOT a separate row inside buildClientKeyboard', () => {
    // techspec_start may exist elsewhere as a callback handler but must not be
    // its own standalone row inside the client keyboard builder
    const fnStart = botSrc.indexOf('async function buildClientKeyboard()');
    const fnEnd = botSrc.indexOf('\n}', fnStart) + 2;
    const fnBody = botSrc.slice(fnStart, fnEnd);
    expect(fnBody).not.toContain("'techspec_start'");
  });
});

// ─── 2. CEO Intelligence delegation (3 tests) ────────────────────────────────

describe('CEO Intelligence delegation', () => {
  test('strategic_core.py contains generate_weekly_summary method', () => {
    expect(ceoSrc).toContain('generate_weekly_summary');
  });

  test('experiment_system.py contains CEODelegation class', () => {
    expect(expSrc).toContain('CEODelegation');
  });

  test('experiment_system.py contains mark_outcome method', () => {
    expect(expSrc).toContain('mark_outcome');
  });
});

// ─── 3. Experiment system A/B tracking (4 tests) ─────────────────────────────

describe('Experiment system A/B tracking', () => {
  test('experiment_system.py contains propose_hypothesis', () => {
    expect(expSrc).toContain('propose_hypothesis');
  });

  test('experiment_system.py contains track_result', () => {
    expect(expSrc).toContain('track_result');
  });

  test('experiment_system.py contains get_winning_variant', () => {
    expect(expSrc).toContain('get_winning_variant');
  });

  test('experiment_system.py uses ceo_experiments.json for persistence', () => {
    expect(expSrc).toContain('ceo_experiments.json');
  });
});

// ─── 4. Admin sub-menu callbacks verified (4 tests) ──────────────────────────

describe('Admin sub-menu callbacks verified', () => {
  test("bot.js contains handler for data === 'adm_menu_analytics'", () => {
    expect(botSrc).toContain("data === 'adm_menu_analytics'");
  });

  test("bot.js contains handler for data === 'adm_menu_marketing'", () => {
    expect(botSrc).toContain("data === 'adm_menu_marketing'");
  });

  test('adm_menu_analytics and adm_menu_marketing handlers each check isAdmin', () => {
    // Find the analytics handler block and verify isAdmin appears nearby
    const analyticsIdx = botSrc.indexOf("data === 'adm_menu_analytics'");
    const analyticsBlock = botSrc.slice(analyticsIdx, analyticsIdx + 200);
    expect(analyticsBlock).toContain('isAdmin');

    const marketingIdx = botSrc.indexOf("data === 'adm_menu_marketing'");
    const marketingBlock = botSrc.slice(marketingIdx, marketingIdx + 200);
    expect(marketingBlock).toContain('isAdmin');
  });

  test('bot.js contains KB_ADMIN_FACTORY definition', () => {
    expect(botSrc).toContain('KB_ADMIN_FACTORY');
  });
});

// ─── 5. strings.js has 200+ keys (2 tests) ───────────────────────────────────

describe('strings.js coverage', () => {
  test('strings.js exports at least 200 string keys', () => {
    // eslint-disable-next-line global-require
    const STRINGS = require('../strings.js');
    expect(Object.keys(STRINGS).length).toBeGreaterThanOrEqual(200);
  });

  test('strings.js contains STRINGS.errorAccessDeniedShort (recently added)', () => {
    // eslint-disable-next-line global-require
    const STRINGS = require('../strings.js');
    expect(STRINGS).toHaveProperty('errorAccessDeniedShort');
  });
});

// ─── 6. Health endpoint returns backup status (2 tests) ───────────────────────

describe('Health endpoint returns backup status', () => {
  test("server.js includes 'backup' field in health response", () => {
    expect(serverSrc).toContain('backup');
  });

  test("server.js includes 'last_backup' key in backup status object", () => {
    expect(serverSrc).toContain('last_backup');
  });
});
