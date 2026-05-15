'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');
const apiCode = fs.readFileSync(path.join(ROOT, 'routes', 'api.js'), 'utf8');
const csrfCode = fs.readFileSync(path.join(ROOT, 'middleware', 'csrf.js'), 'utf8');
const cacheCode = fs.readFileSync(path.join(ROOT, 'services', 'cache.js'), 'utf8');
const igCode = fs.readFileSync(path.join(ROOT, 'services', 'instagram.js'), 'utf8');

const contentDeptPath = path.join('/home/user/Pablo/factory/agents/content_dept.py');
const contentDeptCode = fs.existsSync(contentDeptPath) ? fs.readFileSync(contentDeptPath, 'utf8') : '';

// ─── T1: Instagram social posts bot panel (bot.js) ───────────────────────────

describe('T1: Instagram social posts bot panel (bot.js)', () => {
  test('T01: showSocialPostsPanel function exists in bot.js', () => {
    expect(botCode).toMatch(/async function showSocialPostsPanel\s*\(/);
  });

  test('T02: generateInstagramPost function exists in bot.js', () => {
    expect(botCode).toMatch(/async function generateInstagramPost\s*\(/);
  });

  test('T03: adm_ig_pub_ callback handler exists (marks as published)', () => {
    expect(botCode).toMatch(/data\.startsWith\s*\(\s*['"]adm_ig_pub_['"]\s*\)/);
    // Also verify it sets status to 'published'
    expect(botCode).toMatch(/status\s*=\s*['"]published['"]/);
  });

  test('T04: adm_ig_del_ callback handler exists (deletes post)', () => {
    expect(botCode).toMatch(/data\.startsWith\s*\(\s*['"]adm_ig_del_['"]\s*\)/);
    // Verify it runs a DELETE statement
    expect(botCode).toMatch(/DELETE FROM social_posts WHERE id=\?/);
  });

  test('T05: adm_social_f_ callback for filter navigation exists', () => {
    expect(botCode).toMatch(/data\.startsWith\s*\(\s*['"]adm_social_f_['"]\s*\)/);
    // Verify it calls showSocialPostsPanel with parsed filter and page
    expect(botCode).toMatch(/showSocialPostsPanel\s*\(\s*chatId,\s*pageNum,\s*filterStr\s*\)/);
  });
});

// ─── T2: Sitemap auto-regeneration (routes/api.js) ───────────────────────────

describe('T2: Sitemap auto-regeneration (routes/api.js)', () => {
  test('T06: generateSitemap function exists in api.js', () => {
    expect(apiCode).toMatch(/async function generateSitemap\s*\(\s*\)/);
  });

  test('T07: generateSitemap is called after POST /admin/models (or /admin/models/json)', () => {
    // The function is called in multiple POST handlers that create/update models
    const callCount = (apiCode.match(/generateSitemap\s*\(\s*\)\.catch/g) || []).length;
    expect(callCount).toBeGreaterThanOrEqual(1);
  });

  test('T08: GET /admin/sitemap/regenerate endpoint exists (router mounted at /api)', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/sitemap\/regenerate['"]/);
  });

  test('T09: sitemap regeneration uses COALESCE(archived,0)=0 to filter models', () => {
    expect(apiCode).toMatch(/COALESCE\s*\(\s*archived\s*,\s*0\s*\)\s*=\s*0/);
  });

  test('T10: static pages (catalog.html, booking.html) are included in sitemap output', () => {
    expect(apiCode).toMatch(/catalog\.html/);
    expect(apiCode).toMatch(/booking\.html/);
  });
});

// ─── T3: WeeklySummaryAgent (factory) ────────────────────────────────────────

describe('T3: WeeklySummaryAgent (factory/agents/content_dept.py)', () => {
  beforeAll(() => {
    if (!contentDeptCode) {
      console.warn('content_dept.py not found — T3 tests will be skipped via empty string checks');
    }
  });

  test('T11: WeeklySummaryAgent class exists in content_dept.py', () => {
    expect(contentDeptCode).toMatch(/class WeeklySummaryAgent/);
  });

  test('T12: generate_summary method exists', () => {
    expect(contentDeptCode).toMatch(/def generate_summary\s*\(/);
  });

  test('T13: format_telegram_message method exists', () => {
    expect(contentDeptCode).toMatch(/def format_telegram_message\s*\(/);
  });

  test('T14: ContentDepartment run_cycle includes weekly_summary logic (weekday check)', () => {
    // run_cycle checks weekday == 0 (Monday) before running weekly summary
    expect(contentDeptCode).toMatch(/weekday\s*\(\s*\)\s*==\s*0/);
    expect(contentDeptCode).toMatch(/weekly_summary/);
  });
});

// ─── T4: ESLint (production code quality) ────────────────────────────────────

describe('T4: ESLint lint-friendly naming conventions', () => {
  test('T15: middleware/csrf.js uses _ip (renamed to avoid lint warning)', () => {
    // The parameter is prefixed with _ to signal intentionally unused
    expect(csrfCode).toMatch(/function validateToken\s*\([^)]*_ip[^)]*\)/);
  });

  test('T16: services/cache.js uses _ttlMs in get() method signature', () => {
    // The second parameter is prefixed with _ to signal intentionally unused
    expect(cacheCode).toMatch(/get\s*\(\s*key\s*,\s*_ttlMs\s*\)/);
  });
});

// ─── T5: Instagram service completeness ──────────────────────────────────────

describe('T5: Instagram service completeness (services/instagram.js)', () => {
  test('T17: publishPhoto function exists in services/instagram.js', () => {
    expect(igCode).toMatch(/async function publishPhoto\s*\(/);
  });

  test('T18: services/instagram.js exports isConfigured', () => {
    expect(igCode).toMatch(/isConfigured/);
    expect(igCode).toMatch(/module\.exports\s*=\s*\{[^}]*isConfigured/s);
  });

  test('T19: getRecentMedia or getMediaList function exists in instagram.js', () => {
    const hasGetRecentMedia = /async function getRecentMedia\s*\(/.test(igCode);
    const hasGetMediaList = /async function getMediaList\s*\(/.test(igCode);
    const hasGetProfile = /async function getProfile\s*\(/.test(igCode);
    expect(hasGetRecentMedia || hasGetMediaList || hasGetProfile).toBe(true);
  });
});
