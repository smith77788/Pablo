'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');
const dbCode = fs.readFileSync(path.join(ROOT, 'database.js'), 'utf8');
const cyclePy = fs.existsSync(path.join(ROOT, '../factory/cycle.py'))
  ? fs.readFileSync(path.join(ROOT, '../factory/cycle.py'), 'utf8')
  : '';
const contentDept = fs.existsSync(path.join(ROOT, '../factory/agents/content_dept.py'))
  ? fs.readFileSync(path.join(ROOT, '../factory/agents/content_dept.py'), 'utf8')
  : '';

// ─── T1: Search menu breadcrumb ──────────────────────────────────────────────

describe('T1: Search menu breadcrumb', () => {
  test('T01: showSearchMenu function exists in bot.js', () => {
    expect(botCode).toMatch(/async function showSearchMenu\s*\(/);
  });

  test('T02: showSearchMenu has breadcrumb navigation header (🏠 Главная)', () => {
    // The breadcrumb line is: _🏠 Главная › 🔍 Поиск_
    expect(botCode).toMatch(/🏠 Главная\s*›\s*🔍\s*Поиск/);
  });
});

// ─── T2: Factory content department ──────────────────────────────────────────

describe('T2: Factory content department', () => {
  test('T03: content_dept.py file exists and has ContentDepartment class', () => {
    expect(contentDept).toBeTruthy();
    expect(contentDept).toMatch(/class ContentDepartment/);
  });

  test('T04: ModelDescriptionAgent class exists', () => {
    expect(contentDept).toMatch(/class ModelDescriptionAgent/);
  });

  test('T05: FAQContentAgent class exists', () => {
    expect(contentDept).toMatch(/class FAQContentAgent/);
  });

  test('T06: cycle.py imports or uses ContentDepartment', () => {
    expect(cyclePy).toBeTruthy();
    expect(cyclePy).toMatch(/ContentDepartment/);
  });
});

// ─── T3: Schema completeness ──────────────────────────────────────────────────

describe('T3: Schema completeness', () => {
  test('T07: faq table exists in database.js', () => {
    expect(dbCode).toMatch(/CREATE TABLE IF NOT EXISTS faq/);
  });

  test('T08: bot_broadcasts table exists in database.js', () => {
    expect(dbCode).toMatch(/CREATE TABLE IF NOT EXISTS bot_broadcasts/);
  });

  test('T09: social_posts table exists in database.js', () => {
    expect(dbCode).toMatch(/CREATE TABLE IF NOT EXISTS social_posts/);
  });
});

// ─── T4: Admin toggle features ───────────────────────────────────────────────

describe('T4: Admin toggle features', () => {
  test('T10: adm_toggle_wishlist callback handled in bot.js', () => {
    expect(botCode).toMatch(/adm_toggle_wishlist/);
  });

  test('T11: adm_toggle_faq callback handled in bot.js', () => {
    expect(botCode).toMatch(/adm_toggle_faq/);
  });

  test('T12: adm_toggle_loyalty callback handled in bot.js', () => {
    expect(botCode).toMatch(/adm_toggle_loyalty/);
  });

  test('T13: wishlist_enabled used in buildClientKeyboard', () => {
    // buildClientKeyboard function must reference wishlist_enabled
    expect(botCode).toMatch(/async function buildClientKeyboard/);
    expect(botCode).toMatch(/wishlist_enabled/);
  });
});

// ─── T5: Client order features ───────────────────────────────────────────────

describe('T5: Client order features', () => {
  test('T14: repeat_order_ callback handler exists', () => {
    expect(botCode).toMatch(/data\.startsWith\(['"]repeat_order_['"]\)/);
  });

  test('T15: repeatOrder function exists in bot.js', () => {
    expect(botCode).toMatch(/async function repeatOrder\s*\(/);
  });

  test('T16: repeated order preserves client name and phone', () => {
    // repeatOrder prefills client_name and client_phone from original order
    expect(botCode).toMatch(/client_name:\s*o\.client_name/);
    expect(botCode).toMatch(/client_phone:\s*o\.client_phone/);
  });
});
