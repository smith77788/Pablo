'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const adminCode = fs.readFileSync(path.join(ROOT, 'handlers', 'admin.js'), 'utf8');
const apiCode = fs.readFileSync(path.join(ROOT, 'routes', 'api.js'), 'utf8');
const dbCode = fs.readFileSync(path.join(ROOT, 'database.js'), 'utf8');
const instagramCode = fs.readFileSync(path.join(ROOT, 'services', 'instagram.js'), 'utf8');

// ─── T1: Admin stats completeness ────────────────────────────────────────────

describe('T1: Admin stats completeness (handlers/admin.js)', () => {
  test('T01: showAdminStats function exists in handlers/admin.js', () => {
    expect(adminCode).toMatch(/async function showAdminStats\s*\(/);
  });

  test('T02: stats include conversion calculation (new→confirmed)', () => {
    // The conversion rate (new → confirmed) is computed in showAdminStats
    expect(adminCode).toMatch(/conversion/);
    expect(adminCode).toMatch(/confirmed/);
  });

  test('T03: stats include revenue (SUM budget)', () => {
    expect(adminCode).toMatch(/SUM\s*\(/i);
    expect(adminCode).toMatch(/budget/i);
  });

  test('T04: stats include topModels (top 5 by orders)', () => {
    expect(adminCode).toMatch(/topModels/);
  });

  test('T05: stats include newClientsMonth', () => {
    expect(adminCode).toMatch(/newClientsMonth/);
  });
});

// ─── T2: Social posts API ─────────────────────────────────────────────────────

describe('T2: Social posts API (routes/api.js)', () => {
  test('T06: GET /admin/social/posts endpoint exists', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/social\/posts['"]/);
  });

  test('T07: POST /admin/social/posts endpoint exists', () => {
    expect(apiCode).toMatch(/router\.post\s*\(\s*['"]\/admin\/social\/posts['"]/);
  });

  test('T08: PATCH /admin/social/posts/:id/status endpoint exists', () => {
    expect(apiCode).toMatch(/router\.patch\s*\(\s*['"]\/admin\/social\/posts\/:id\/status['"]/);
  });

  test('T09: social_posts table exists in database.js', () => {
    expect(dbCode).toMatch(/CREATE TABLE IF NOT EXISTS social_posts/);
  });
});

// ─── T3: Instagram service ────────────────────────────────────────────────────

describe('T3: Instagram service (services/instagram.js)', () => {
  test('T10: instagram.js exports isConfigured function', () => {
    expect(instagramCode).toMatch(/function isConfigured\s*\(/);
    expect(instagramCode).toMatch(/isConfigured/);
    // Confirm it's actually exported
    expect(instagramCode).toMatch(/module\.exports\s*=\s*\{[^}]*isConfigured/s);
  });

  test('T11: createPhotoContainer function exists', () => {
    expect(instagramCode).toMatch(/async function createPhotoContainer\s*\(/);
  });

  test('T12: publishPhoto (one-step publish) function exists', () => {
    // The actual one-step publish function is named publishPhoto, not publishPost
    expect(instagramCode).toMatch(/async function publishPhoto\s*\(/);
    expect(instagramCode).toMatch(/module\.exports\s*=\s*\{[^}]*publishPhoto/s);
  });
});

// ─── T4: Client OTP cabinet API ───────────────────────────────────────────────

describe('T4: Client OTP cabinet API (routes/api.js)', () => {
  test('T13: POST /api/client/request-code endpoint exists', () => {
    expect(apiCode).toMatch(/router\.post\s*\(\s*['"]\/client\/request-code['"]/);
  });

  test('T14: POST /api/client/verify endpoint exists', () => {
    expect(apiCode).toMatch(/router\.post\s*\(\s*['"]\/client\/verify['"]/);
  });

  test('T15: GET /api/orders/by-phone endpoint exists', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/orders\/by-phone['"]/);
  });

  test('T16: client/verify returns token (JWT)', () => {
    // The verify endpoint issues a JWT and returns it as `token` in the response
    expect(apiCode).toMatch(/jwt\.sign\s*\(\s*\{[^}]*type:\s*['"]client['"]/);
    expect(apiCode).toMatch(/res\.json\s*\(\s*\{[^}]*token/);
  });
});

// ─── T5: Schema completeness ──────────────────────────────────────────────────

describe('T5: Schema completeness (database.js)', () => {
  test('T17: wishlists table exists (v11 or v21)', () => {
    expect(dbCode).toMatch(/CREATE TABLE IF NOT EXISTS wishlists/);
  });

  test('T18: archived column added to models (v4 inline or v22 ALTER)', () => {
    // Either defined inline in CREATE TABLE or added via ALTER TABLE
    expect(dbCode).toMatch(/archived/);
    // At least one of: inline column definition or ALTER TABLE
    const hasInline = /archived\s+INTEGER/.test(dbCode);
    const hasAlter = /ALTER TABLE models ADD COLUMN archived/.test(dbCode);
    expect(hasInline || hasAlter).toBe(true);
  });

  test('T19: schema_versions table exists', () => {
    expect(dbCode).toMatch(/CREATE TABLE IF NOT EXISTS schema_versions/);
  });
});
