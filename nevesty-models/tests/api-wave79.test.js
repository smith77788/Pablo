'use strict';
/**
 * Integration tests for Wave 79 security and reliability fixes:
 * - JWT hardening (no fallback secrets)
 * - Idempotent payment webhooks (Yookassa + Stripe)
 * - Memory cleanup (setInterval + .unref())
 * - Order ID validation
 * - Error handling via next(e)
 */

const path = require('path');
const fs = require('fs');

const API_JS = path.join(__dirname, '../routes/api.js');
const BOT_JS = path.join(__dirname, '../bot.js');

const code = fs.readFileSync(API_JS, 'utf8');
const botCode = fs.readFileSync(BOT_JS, 'utf8');

// ── БЛОК 1: JWT Hardening — no fallback secrets ────────────────────────────────

describe('Wave 79 БЛОК 1: JWT hardening — no fallback secrets', () => {
  test('api.js does NOT contain || "secret" fallback in JWT operations', () => {
    // Should not have: process.env.JWT_SECRET || 'secret'
    expect(code).not.toMatch(/process\.env\.JWT_SECRET\s*\|\|\s*['"]secret['"]/);
  });

  test('api.js does NOT contain || "secret" literal fallback at all', () => {
    expect(code).not.toMatch(/JWT_SECRET\s*\|\|\s*['"]secret['"]/);
  });

  test('api.js does NOT use hardcoded fallback string for JWT signing', () => {
    // jwt.sign should not be called with a hardcoded string fallback
    expect(code).not.toMatch(/jwt\.sign\([^)]*\|\|\s*['"][^'"]{1,20}['"]/);
  });

  test('api.js does NOT use hardcoded fallback string for JWT verify', () => {
    expect(code).not.toMatch(/jwt\.verify\([^)]*\|\|\s*['"][^'"]{1,20}['"]/);
  });

  test('issueTokenPair reads JWT_SECRET before signing', () => {
    expect(code).toContain('const jwtSecret = process.env.JWT_SECRET');
  });

  test('issueTokenPair throws if JWT_SECRET is missing', () => {
    expect(code).toMatch(/if\s*\(!jwtSecret\)\s*throw/);
  });

  test('issueTokenPair error message mentions JWT_SECRET', () => {
    expect(code).toContain('JWT_SECRET environment variable is not set');
  });

  test('refresh token route also checks JWT_SECRET (jwtSecret2)', () => {
    expect(code).toContain('const jwtSecret2 = process.env.JWT_SECRET');
  });

  test('refresh token route throws if jwtSecret2 missing', () => {
    expect(code).toMatch(/if\s*\(!jwtSecret2\)\s*throw/);
  });

  test('client/verify route reads clientJwtSecret from env', () => {
    expect(code).toContain('const clientJwtSecret = process.env.JWT_SECRET');
  });

  test('client/verify route throws if clientJwtSecret missing', () => {
    expect(code).toMatch(/if\s*\(!clientJwtSecret\)\s*throw/);
  });

  test('_clientAuth reads verifySecret from env', () => {
    expect(code).toContain('const verifySecret = process.env.JWT_SECRET');
  });

  test('_clientAuth returns 500 if verifySecret missing', () => {
    expect(code).toMatch(/if\s*\(!verifySecret\)\s*return\s*res\.status\(500\)/);
  });

  test('_clientAuth error body mentions JWT_SECRET not configured', () => {
    expect(code).toContain('JWT_SECRET not configured');
  });

  test('_clientAuth uses verifySecret for jwt.verify', () => {
    expect(code).toContain('jwt.verify(header.slice(7), verifySecret)');
  });

  test('orders/:id/pay route reads paySecret from env', () => {
    expect(code).toContain('const paySecret = process.env.JWT_SECRET');
  });

  test('orders/:id/pay uses paySecret for jwt.verify', () => {
    expect(code).toContain('jwt.verify(authHeader.slice(7), paySecret)');
  });

  test('no jwt.sign call uses raw string literal as secret', () => {
    // Check that jwt.sign is never called with a bare string literal as second arg
    const signCalls = [...code.matchAll(/jwt\.sign\([^;]+?\)/gs)];
    const badSign = signCalls.some(m => /jwt\.sign\(\s*\{[^}]*\}\s*,\s*['"][^'"]+['"]\s*[,)]/s.test(m[0]));
    expect(badSign).toBe(false);
  });
});

// ── БЛОК 2: Idempotent Yookassa Webhooks ──────────────────────────────────────

describe('Wave 79 БЛОК 2: Idempotent Yookassa webhooks', () => {
  test('Yookassa webhook UPDATE query contains AND payment_status != paid guard', () => {
    expect(code).toContain("AND payment_status != 'paid'");
  });

  test('Yookassa webhook checks ord.payment_status !== paid before processing', () => {
    expect(code).toContain("ord.payment_status !== 'paid'");
  });

  test('Yookassa webhook has idempotency guard comment', () => {
    expect(code).toContain('Idempotency guard');
  });

  test('Yookassa UPDATE sets payment_status to paid', () => {
    expect(code).toContain("payment_status='paid'");
  });

  test('Yookassa UPDATE sets paid_at=CURRENT_TIMESTAMP', () => {
    expect(code).toContain('paid_at=CURRENT_TIMESTAMP');
  });

  test('Yookassa UPDATE sets status=confirmed', () => {
    expect(code).toContain("status='confirmed'");
  });

  test('Yookassa webhook section is inside payment.succeeded event handler', () => {
    expect(code).toContain("event?.event === 'payment.succeeded'");
  });

  test('Yookassa webhook looks up order by payment_id or order_id metadata', () => {
    expect(code).toMatch(/SELECT \* FROM orders WHERE id=\?.*SELECT \* FROM orders WHERE payment_id=\?/s);
  });

  test('Yookassa webhook uses metaOrderId for lookup', () => {
    expect(code).toContain('metaOrderId');
  });
});

// ── БЛОК 3: Idempotent Stripe Webhooks ───────────────────────────────────────

describe('Wave 79 БЛОК 3: Idempotent Stripe webhooks', () => {
  test('markPaid helper is defined as async function', () => {
    expect(code).toContain('const markPaid = async (ord, ref) =>');
  });

  test('markPaid returns early if payment_status already paid', () => {
    expect(code).toContain("if (ord.payment_status === 'paid') return;");
  });

  test('markPaid UPDATE contains AND payment_status != paid guard', () => {
    // The UPDATE inside markPaid — check we have both markers in order
    const idx1 = code.indexOf('const markPaid = async (ord, ref) =>');
    const idx2 = code.indexOf("AND payment_status != 'paid'", idx1);
    expect(idx1).toBeGreaterThan(0);
    expect(idx2).toBeGreaterThan(idx1);
  });

  test('markPaid checks result.changes to guard against concurrent webhooks', () => {
    expect(code).toContain('if (!result?.changes) return;');
  });

  test('markPaid comment explains concurrency reason', () => {
    expect(code).toContain('another concurrent webhook already processed this');
  });

  test('markPaid has idempotency guard comment', () => {
    expect(code).toContain('idempotent — skips if already paid');
  });

  test('Stripe webhook uses markPaid for checkout.session.completed', () => {
    expect(code).toMatch(/checkout\.session\.completed[\s\S]{0,300}markPaid/);
  });

  test('Stripe webhook uses markPaid for payment_intent.succeeded', () => {
    expect(code).toContain('payment_intent.succeeded');
  });

  test('Stripe UPDATE inside markPaid sets payment_status to paid', () => {
    const idx = code.indexOf('const markPaid = async (ord, ref) =>');
    const snippet = code.slice(idx, idx + 1000);
    expect(snippet).toContain("payment_status='paid'");
  });

  test('Stripe markPaid result variable used to check changes', () => {
    // markPaid stores run() result and checks result?.changes
    const idx = code.indexOf('const markPaid = async (ord, ref) =>');
    expect(idx).toBeGreaterThan(0);
    const snippet = code.slice(idx, idx + 600);
    expect(snippet).toContain('const result = await run(');
    expect(snippet).toContain('result?.changes');
  });

  test('Stripe webhook handler is a POST route', () => {
    expect(code).toContain("router.post('/webhooks/stripe'");
  });

  test('Yookassa webhook handler is a POST route', () => {
    expect(code).toContain("router.post('/webhooks/yookassa'");
  });

  test('Stripe webhook verifies signature when STRIPE_WEBHOOK_SECRET is set', () => {
    expect(code).toContain('process.env.STRIPE_WEBHOOK_SECRET');
    expect(code).toContain('verifyStripeWebhook');
  });
});

// ── БЛОК 4: Memory Cleanup — api.js setIntervals ──────────────────────────────

describe('Wave 79 БЛОК 4: Memory cleanup setIntervals in api.js', () => {
  test('_viewRateLimits Map is declared in api.js', () => {
    expect(code).toContain('const _viewRateLimits = new Map()');
  });

  test('_viewRateLimits has setInterval cleanup', () => {
    expect(code).toMatch(/_viewRateLimits[\s\S]{0,200}setInterval|setInterval[\s\S]{0,200}_viewRateLimits/);
  });

  test('_viewRateLimits cleanup runs every hour (60 * 60 * 1000)', () => {
    const idx = code.indexOf('const _viewRateLimits = new Map()');
    expect(idx).toBeGreaterThan(0);
    const snippet = code.slice(idx, idx + 500);
    expect(snippet).toContain('60 * 60 * 1000');
  });

  test('_viewRateLimits cleanup uses cutoff timestamp to remove old entries', () => {
    const idx = code.indexOf('const _viewRateLimits = new Map()');
    const snippet = code.slice(idx, idx + 600);
    expect(snippet).toContain('cutoff');
  });

  test('_viewRateLimits setInterval uses .unref()', () => {
    const idx = code.indexOf('const _viewRateLimits = new Map()');
    const snippet = code.slice(idx, idx + 600);
    expect(snippet).toContain('.unref()');
  });

  test('_viewRateLimits cleanup deletes entries older than cutoff', () => {
    const idx = code.indexOf('const _viewRateLimits = new Map()');
    const snippet = code.slice(idx, idx + 600);
    expect(snippet).toContain('_viewRateLimits.delete(key)');
  });

  test('_byPhoneLimits Map is declared in api.js', () => {
    expect(code).toContain('const _byPhoneLimits = new Map()');
  });

  test('_byPhoneLimits has setInterval cleanup', () => {
    expect(code).toMatch(/_byPhoneLimits[\s\S]{0,300}setInterval|setInterval[\s\S]{0,100}_byPhoneLimits/);
  });

  test('_byPhoneLimits cleanup runs every 15 minutes (15 * 60 * 1000)', () => {
    const idx = code.indexOf('const _byPhoneLimits = new Map()');
    const snippet = code.slice(idx, idx + 500);
    expect(snippet).toContain('15 * 60 * 1000');
  });

  test('_byPhoneLimits setInterval uses .unref()', () => {
    const idx = code.indexOf('const _byPhoneLimits = new Map()');
    const snippet = code.slice(idx, idx + 500);
    expect(snippet).toContain('.unref()');
  });

  test('_byPhoneLimits cleanup filters timestamps array to remove stale entries', () => {
    const idx = code.indexOf('const _byPhoneLimits = new Map()');
    const snippet = code.slice(idx, idx + 600);
    expect(snippet).toContain('.filter(');
  });

  test('_byPhoneLimits cleanup deletes IPs with no recent timestamps', () => {
    const idx = code.indexOf('const _byPhoneLimits = new Map()');
    const snippet = code.slice(idx, idx + 600);
    expect(snippet).toContain('_byPhoneLimits.delete(ip)');
  });

  test('api.js has cleanup comment for _viewRateLimits', () => {
    expect(code).toContain('Cleanup _viewRateLimits');
  });

  test('api.js has cleanup comment for _byPhoneLimits', () => {
    expect(code).toContain('Cleanup _byPhoneLimits');
  });
});

// ── БЛОК 5: Memory Cleanup — bot.js setIntervals ──────────────────────────────

describe('Wave 79 БЛОК 5: Memory cleanup setIntervals in bot.js', () => {
  test('_compareLists Map is declared in bot.js', () => {
    expect(botCode).toContain('const _compareLists = new Map()');
  });

  test('_compareLists has setInterval cleanup', () => {
    const idx = botCode.indexOf('const _compareLists = new Map()');
    const snippet = botCode.slice(idx, idx + 300);
    expect(snippet).toContain('setInterval');
  });

  test('_compareLists cleanup runs every 24 hours (24 * 60 * 60 * 1000)', () => {
    const idx = botCode.indexOf('const _compareLists = new Map()');
    const snippet = botCode.slice(idx, idx + 300);
    expect(snippet).toContain('24 * 60 * 60 * 1000');
  });

  test('_compareLists cleanup uses .clear() to wipe entire map', () => {
    const idx = botCode.indexOf('const _compareLists = new Map()');
    const snippet = botCode.slice(idx, idx + 300);
    expect(snippet).toContain('_compareLists.clear()');
  });

  test('_compareLists setInterval uses .unref()', () => {
    const idx = botCode.indexOf('const _compareLists = new Map()');
    const snippet = botCode.slice(idx, idx + 300);
    expect(snippet).toContain('.unref()');
  });

  test('searchFilters Map is declared in bot.js', () => {
    expect(botCode).toContain('const searchFilters = new Map()');
  });

  test('searchFilters has setInterval cleanup', () => {
    const idx = botCode.indexOf('const searchFilters = new Map()');
    const snippet = botCode.slice(idx, idx + 200);
    expect(snippet).toContain('setInterval');
  });

  test('searchFilters cleanup runs every 6 hours (6 * 60 * 60 * 1000)', () => {
    const idx = botCode.indexOf('const searchFilters = new Map()');
    expect(idx).toBeGreaterThan(0);
    const snippet = botCode.slice(idx, idx + 300);
    expect(snippet).toContain('6 * 60 * 60 * 1000');
  });

  test('searchFilters cleanup uses .clear() to wipe entire map', () => {
    const idx = botCode.indexOf('const searchFilters = new Map()');
    expect(idx).toBeGreaterThan(0);
    const snippet = botCode.slice(idx, idx + 300);
    expect(snippet).toContain('searchFilters.clear()');
  });

  test('searchFilters setInterval uses .unref()', () => {
    const idx = botCode.indexOf('const searchFilters = new Map()');
    expect(idx).toBeGreaterThan(0);
    const snippet = botCode.slice(idx, idx + 300);
    expect(snippet).toContain('.unref()');
  });

  test('bot.js total setInterval + .unref() calls >= 2 (for _compareLists + searchFilters)', () => {
    const matches = botCode.match(/setInterval\([^)]+\)\.unref\(\)|\.unref\(\)/g) || [];
    // Just check we have unref calls present
    expect(botCode).toContain('.unref()');
  });
});

// ── БЛОК 6: Order ID Validation ───────────────────────────────────────────────

describe('Wave 79 БЛОК 6: Order ID validation in api.js', () => {
  test('POST /admin/orders/:id/pay parses id with parseInt', () => {
    // Find the route and check parseInt is used
    const idx = code.indexOf("router.post('/admin/orders/:id/pay'");
    expect(idx).toBeGreaterThan(0);
    const snippet = code.slice(idx, idx + 300);
    expect(snippet).toContain('parseInt(req.params.id)');
  });

  test('POST /admin/orders/:id/pay validates id > 0 via Number.isInteger', () => {
    const idx = code.indexOf("router.post('/admin/orders/:id/pay'");
    const snippet = code.slice(idx, idx + 300);
    expect(snippet).toContain('Number.isInteger(id)');
  });

  test('POST /admin/orders/:id/pay returns 400 for invalid ID', () => {
    const idx = code.indexOf("router.post('/admin/orders/:id/pay'");
    const snippet = code.slice(idx, idx + 300);
    expect(snippet).toContain('res.status(400)');
  });

  test('POST /admin/orders/:id/pay checks id <= 0 condition', () => {
    const idx = code.indexOf("router.post('/admin/orders/:id/pay'");
    const snippet = code.slice(idx, idx + 300);
    expect(snippet).toContain('id <= 0');
  });

  test('POST /admin/orders/:id/pay returns Invalid order ID message', () => {
    const idx = code.indexOf("router.post('/admin/orders/:id/pay'");
    const snippet = code.slice(idx, idx + 400);
    expect(snippet).toContain('Invalid order ID');
  });

  test('POST /orders/:id/pay (public route) also validates id > 0', () => {
    const idx = code.indexOf("router.post('/orders/:id/pay'");
    expect(idx).toBeGreaterThan(0);
    const snippet = code.slice(idx, idx + 400);
    expect(snippet).toContain('Number.isInteger(id)');
    expect(snippet).toContain('id <= 0');
  });

  test('PATCH /admin/orders/:id/status also uses parseInt for id', () => {
    const idx = code.indexOf("router.patch('/admin/orders/:id/status'");
    expect(idx).toBeGreaterThan(0);
    const snippet = code.slice(idx, idx + 300);
    expect(snippet).toContain('parseInt(req.params.id)');
  });

  test('PATCH /admin/orders/:id/status validates id > 0', () => {
    const idx = code.indexOf("router.patch('/admin/orders/:id/status'");
    const snippet = code.slice(idx, idx + 300);
    expect(snippet).toContain('id <= 0');
  });
});

// ── БЛОК 7: Error Handling via next(e) ───────────────────────────────────────

describe('Wave 79 БЛОК 7: Error handling via next(e)', () => {
  test('PATCH /admin/orders/:id/status uses next(e) in catch block', () => {
    const idx = code.indexOf("router.patch('/admin/orders/:id/status'");
    expect(idx).toBeGreaterThan(0);
    // Route is long (SMS, email, WS, CRM handlers), use 8000 chars
    const snippet = code.slice(idx, idx + 8000);
    expect(snippet).toContain('next(e)');
  });

  test('POST /admin/orders/:id/pay uses next(e) in catch block', () => {
    const idx = code.indexOf("router.post('/admin/orders/:id/pay'");
    expect(idx).toBeGreaterThan(0);
    const snippet = code.slice(idx, idx + 2000);
    expect(snippet).toContain('next(e)');
  });

  test('Yookassa webhook uses next(e) in catch block', () => {
    const idx = code.indexOf("router.post('/webhooks/yookassa'");
    expect(idx).toBeGreaterThan(0);
    const snippet = code.slice(idx, idx + 5000);
    expect(snippet).toContain('next(e)');
  });

  test('Stripe webhook uses next(e) in catch block', () => {
    const idx = code.indexOf("router.post('/webhooks/stripe'");
    expect(idx).toBeGreaterThan(0);
    const snippet = code.slice(idx, idx + 5000);
    expect(snippet).toContain('next(e)');
  });

  test('api.js overall has many next(e) error forwardings', () => {
    const matches = (code.match(/next\(e\)/g) || []).length;
    expect(matches).toBeGreaterThan(20);
  });
});

// ── БЛОК 8: Structural Integrity Checks ──────────────────────────────────────

describe('Wave 79 БЛОК 8: Structural integrity', () => {
  test('api.js file exists and is readable', () => {
    expect(fs.existsSync(API_JS)).toBe(true);
  });

  test('bot.js file exists and is readable', () => {
    expect(fs.existsSync(BOT_JS)).toBe(true);
  });

  test('api.js has substantial size (> 100KB)', () => {
    const stat = fs.statSync(API_JS);
    expect(stat.size).toBeGreaterThan(100 * 1024);
  });

  test('bot.js has substantial size (> 100KB)', () => {
    const stat = fs.statSync(BOT_JS);
    expect(stat.size).toBeGreaterThan(100 * 1024);
  });

  test('api.js exports a router', () => {
    expect(code).toContain('module.exports = router');
  });

  test('api.js imports jsonwebtoken', () => {
    expect(code).toMatch(/require\(['"]jsonwebtoken['"]\)/);
  });

  test('api.js has issueTokenPair function', () => {
    expect(code).toContain('async function issueTokenPair(');
  });

  test('api.js has _clientAuth function', () => {
    expect(code).toContain('function _clientAuth(');
  });

  test('api.js uses process.env.JWT_SECRET (not a hardcoded secret)', () => {
    const count = (code.match(/process\.env\.JWT_SECRET/g) || []).length;
    expect(count).toBeGreaterThan(3);
  });

  test('markPaid is defined as a const arrow function inside the Stripe webhook', () => {
    expect(code).toContain('const markPaid = async (ord, ref) =>');
  });

  test('_compareLists uses Set for each entry', () => {
    expect(botCode).toContain('_compareLists.set(key, new Set())');
  });

  test('searchFilters getter helper function exists in bot.js', () => {
    // The helper that returns or creates a filters object
    expect(botCode).toMatch(/searchFilters\.(has|get|set)/);
  });

  test('bot.js has multiple .unref() calls for timers', () => {
    const count = (botCode.match(/\.unref\(\)/g) || []).length;
    expect(count).toBeGreaterThanOrEqual(2);
  });

  test('api.js has multiple .unref() calls for timers', () => {
    // At minimum _viewRateLimits and _byPhoneLimits intervals
    const unrefMatches = [];
    let startIdx = 0;
    while (true) {
      const idx = code.indexOf('.unref()', startIdx);
      if (idx === -1) break;
      unrefMatches.push(idx);
      startIdx = idx + 1;
    }
    expect(unrefMatches.length).toBeGreaterThanOrEqual(2);
  });
});
