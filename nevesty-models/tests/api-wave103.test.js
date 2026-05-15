'use strict';
/**
 * Wave103 tests: 500 page, factory notifier, CORS/XSS security fixes,
 * service worker precache, factory cycle notifications.
 */

const fs = require('fs');
const path = require('path');

const html500 = fs.readFileSync(path.join(__dirname, '..', 'public', '500.html'), 'utf8');
const html404 = fs.readFileSync(path.join(__dirname, '..', 'public', '404.html'), 'utf8');
const serverSrc = fs.readFileSync(path.join(__dirname, '..', 'server.js'), 'utf8');
const notifierSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'factory', 'notifier.py'), 'utf8');
const cycleSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'factory', 'cycle.py'), 'utf8');
const analyticsSrc = fs.readFileSync(path.join(__dirname, '..', 'public', 'admin', 'analytics.html'), 'utf8');
const swSrc = fs.readFileSync(path.join(__dirname, '..', 'public', 'sw.js'), 'utf8');

// ─── 1. 500.html error page (3 tests) ────────────────────────────────────────

describe('500.html error page', () => {
  test('public/500.html exists and is readable', () => {
    expect(html500.length).toBeGreaterThan(0);
  });

  test('public/500.html contains "500" text', () => {
    expect(html500).toContain('500');
  });

  test('public/500.html contains link to catalog.html', () => {
    expect(html500).toContain('catalog.html');
  });
});

// ─── 2. 404.html error page (2 tests) ────────────────────────────────────────

describe('404.html error page', () => {
  test('public/404.html exists and is readable', () => {
    expect(html404.length).toBeGreaterThan(0);
  });

  test('public/404.html contains link to catalog.html', () => {
    expect(html404).toContain('catalog.html');
  });
});

// ─── 3. Express 500 handler in server.js (2 tests) ───────────────────────────

describe('Express 500 error handler', () => {
  test('server.js serves 500.html from error handler', () => {
    expect(serverSrc).toContain('500.html');
  });

  test('server.js serves JSON for /api/ paths near error handler', () => {
    expect(serverSrc).toMatch(/req\.path\.startsWith\(['"`]\/api\//);
  });
});

// ─── 4. Factory notifier (4 tests) ───────────────────────────────────────────

describe('factory/notifier.py', () => {
  test('factory/notifier.py exists and is readable', () => {
    expect(notifierSrc.length).toBeGreaterThan(100);
  });

  test('factory/notifier.py contains send_telegram function', () => {
    expect(notifierSrc).toContain('send_telegram');
  });

  test('factory/notifier.py contains notify_cycle_complete function', () => {
    expect(notifierSrc).toContain('notify_cycle_complete');
  });

  test('factory/notifier.py uses ADMIN_TELEGRAM_IDS env var', () => {
    expect(notifierSrc).toContain('ADMIN_TELEGRAM_IDS');
  });
});

// ─── 5. CORS security fix (2 tests) ──────────────────────────────────────────

describe('CORS security in server.js', () => {
  test('server.js uses origin: false as CORS fallback (not open CORS)', () => {
    expect(serverSrc).toContain('origin: false');
  });

  test('server.js checks ALLOWED_ORIGINS env var', () => {
    expect(serverSrc).toContain('ALLOWED_ORIGINS');
  });
});

// ─── 6. XSS fix in analytics.html (2 tests) ──────────────────────────────────

describe('XSS fix in public/admin/analytics.html', () => {
  test('public/admin/analytics.html exists and is readable', () => {
    expect(analyticsSrc.length).toBeGreaterThan(0);
  });

  test('analytics.html wraps c.city with escHtml() (XSS protection)', () => {
    expect(analyticsSrc).toContain('escHtml(c.city');
  });
});

// ─── 7. Factory cycle sends notifications (2 tests) ──────────────────────────

describe('factory/cycle.py notification integration', () => {
  test('factory/cycle.py imports or references notifier module', () => {
    const hasNotifyCycleComplete = cycleSrc.includes('notify_cycle_complete');
    const hasNotifierImport = cycleSrc.includes('notifier');
    expect(hasNotifyCycleComplete || hasNotifierImport).toBe(true);
  });

  test('factory/cycle.py calls notify.js or uses notifier module', () => {
    const hasNotifyJs = cycleSrc.includes('notify.js');
    const hasNotifier = cycleSrc.includes('notifier');
    expect(hasNotifyJs || hasNotifier).toBe(true);
  });
});

// ─── 8. Service worker caches 500.html (2 tests) ─────────────────────────────

describe('Service worker precache includes 500.html', () => {
  test('public/sw.js exists and is readable', () => {
    expect(swSrc.length).toBeGreaterThan(0);
  });

  test('public/sw.js contains /500.html in precache list', () => {
    expect(swSrc).toContain('/500.html');
  });
});
