'use strict';
/**
 * Wave100 tests: Admin menu restructure — compact KB_MAIN_ADMIN + sub-menus
 *
 * The old KB_MAIN_ADMIN had 9+ rows with all buttons in one place.
 * Now it has 4 compact rows pointing to sub-menu sections:
 *   KB_ADMIN_ANALYTICS, KB_ADMIN_MARKETING, KB_ADMIN_TEAM, KB_ADMIN_FACTORY
 */

const botSrc = require('fs').readFileSync(require('path').join(__dirname, '..', 'bot.js'), 'utf8');

// ─── 1. KB_MAIN_ADMIN structure (4 tests) ────────────────────────────────────

describe('KB_MAIN_ADMIN structure', () => {
  test("contains 'adm_menu_analytics' callback", () => {
    expect(botSrc).toContain('adm_menu_analytics');
  });

  test("contains 'adm_menu_marketing' callback", () => {
    expect(botSrc).toContain('adm_menu_marketing');
  });

  test("contains 'adm_menu_team' callback", () => {
    expect(botSrc).toContain('adm_menu_team');
  });

  test("contains 'adm_menu_factory' callback", () => {
    expect(botSrc).toContain('adm_menu_factory');
  });
});

// ─── 2. KB_ADMIN_ANALYTICS sub-menu (3 tests) ────────────────────────────────

describe('KB_ADMIN_ANALYTICS sub-menu', () => {
  test('KB_ADMIN_ANALYTICS constant exists in bot.js', () => {
    expect(botSrc).toContain('KB_ADMIN_ANALYTICS');
  });

  test("contains 'adm_stats' callback (Статистика button)", () => {
    expect(botSrc).toContain("'adm_stats'");
  });

  test("contains 'adm_audit_log' callback (Журнал button)", () => {
    expect(botSrc).toContain("'adm_audit_log'");
  });
});

// ─── 3. KB_ADMIN_MARKETING sub-menu (3 tests) ────────────────────────────────

describe('KB_ADMIN_MARKETING sub-menu', () => {
  test('KB_ADMIN_MARKETING constant exists in bot.js', () => {
    expect(botSrc).toContain('KB_ADMIN_MARKETING');
  });

  test("contains 'adm_broadcast' callback", () => {
    expect(botSrc).toContain("'adm_broadcast'");
  });

  test("contains 'adm_export' callback", () => {
    expect(botSrc).toContain("'adm_export'");
  });
});

// ─── 4. KB_ADMIN_TEAM sub-menu (3 tests) ─────────────────────────────────────

describe('KB_ADMIN_TEAM sub-menu', () => {
  test('KB_ADMIN_TEAM constant exists in bot.js', () => {
    expect(botSrc).toContain('KB_ADMIN_TEAM');
  });

  test("contains 'adm_admins' callback", () => {
    expect(botSrc).toContain("'adm_admins'");
  });

  test("contains 'adm_reviews' callback", () => {
    expect(botSrc).toContain("'adm_reviews'");
  });
});

// ─── 5. Reply keyboard removed (2 tests) ─────────────────────────────────────

describe('Reply keyboard removed from admin menu', () => {
  test('resize_keyboard: true is NOT used in an admin reply keyboard context', () => {
    // The only resize_keyboard: true must be in REPLY_KB_CLIENT (client keyboard),
    // not in any REPLY_KB_ADMIN constant — verify no REPLY_KB_ADMIN exists
    expect(botSrc).not.toContain('REPLY_KB_ADMIN');
  });

  test("showAdminMenu does NOT send old activation text 'Панель администратора — меню активировано'", () => {
    expect(botSrc).not.toContain('Панель администратора — меню активировано');
  });
});

// ─── 6. Callback handlers for sub-menus (4 tests) ────────────────────────────

describe('Callback handlers for sub-menu navigation', () => {
  test("bot.js handles 'adm_menu_analytics' callback (shows sub-menu)", () => {
    expect(botSrc).toContain("data === 'adm_menu_analytics'");
  });

  test("bot.js handles 'adm_menu_marketing' callback", () => {
    expect(botSrc).toContain("data === 'adm_menu_marketing'");
  });

  test("bot.js handles 'adm_menu_team' callback", () => {
    expect(botSrc).toContain("data === 'adm_menu_team'");
  });

  test("bot.js handles 'adm_menu_factory' callback", () => {
    expect(botSrc).toContain("data === 'adm_menu_factory'");
  });
});
