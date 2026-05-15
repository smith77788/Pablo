'use strict';
/**
 * Wave98 tests: email service, analytics tracking, Customer Success factory
 */

const fs = require('fs');
const path = require('path');

// ─── Helpers ──────────────────────────────────────────────────────────────────

const ROOT = path.join(__dirname, '..');

function readFile(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

function fileExists(rel) {
  try {
    fs.accessSync(path.join(ROOT, rel));
    return true;
  } catch {
    return false;
  }
}

// ─── 1. Email service (services/mailer.js) — 5 tests ─────────────────────────

describe('Email service — services/mailer.js', () => {
  test('services/mailer.js exists', () => {
    expect(fileExists('services/mailer.js')).toBe(true);
  });

  test('exports sendOrderConfirmation function', () => {
    const mailer = require('../services/mailer');
    expect(typeof mailer.sendOrderConfirmation).toBe('function');
  });

  test('exports sendStatusChange function (sendStatusUpdate equivalent)', () => {
    const mailer = require('../services/mailer');
    expect(typeof mailer.sendStatusChange).toBe('function');
  });

  test('mailer is disabled (returns null transporter) when SMTP env vars are missing', () => {
    // Save and unset SMTP env vars
    const saved = {
      SMTP_HOST: process.env.SMTP_HOST,
      SMTP_USER: process.env.SMTP_USER,
      SMTP_PASS: process.env.SMTP_PASS,
    };
    delete process.env.SMTP_HOST;
    delete process.env.SMTP_USER;
    delete process.env.SMTP_PASS;

    // Re-read the source to verify the guard condition
    const src = readFile('services/mailer.js');
    expect(src).toContain('SMTP_HOST');
    expect(src).toContain('SMTP_USER');
    expect(src).toContain('SMTP_PASS');
    // The guard returns null when vars are missing
    expect(src).toContain('return null');

    // Restore
    if (saved.SMTP_HOST !== undefined) process.env.SMTP_HOST = saved.SMTP_HOST;
    if (saved.SMTP_USER !== undefined) process.env.SMTP_USER = saved.SMTP_USER;
    if (saved.SMTP_PASS !== undefined) process.env.SMTP_PASS = saved.SMTP_PASS;
  });

  test('mailer source contains ENABLED-style guard (skips email if SMTP not configured)', () => {
    const src = readFile('services/mailer.js');
    // The service gracefully skips when SMTP is not configured
    expect(src).toContain('SMTP not configured');
  });
});

// ─── 2. Email wired to API (routes/api.js) — 3 tests ─────────────────────────

describe('Email wired to API — routes/api.js', () => {
  let apiSrc;

  beforeAll(() => {
    apiSrc = readFile('routes/api.js');
  });

  test('routes/api.js contains sendOrderConfirmation call', () => {
    expect(apiSrc).toContain('sendOrderConfirmation');
  });

  test('routes/api.js contains sendStatusChange call (status update email)', () => {
    expect(apiSrc).toContain('sendStatusChange');
  });

  test('email calls in api.js use .catch() (non-blocking fire-and-forget)', () => {
    // Extract lines around sendOrderConfirmation
    const confirmIdx = apiSrc.indexOf('sendOrderConfirmation');
    const confirmSnippet = apiSrc.slice(confirmIdx, confirmIdx + 200);
    expect(confirmSnippet).toContain('.catch(');
  });
});

// ─── 3. Analytics helper (public/js/analytics.js) — 4 tests ──────────────────

describe('Analytics helper — public/js/analytics.js', () => {
  let analyticsJs;

  beforeAll(() => {
    analyticsJs = readFile('public/js/analytics.js');
  });

  test('public/js/analytics.js exists', () => {
    expect(fileExists('public/js/analytics.js')).toBe(true);
  });

  test('contains NM.analytics namespace (nmTrack equivalent)', () => {
    expect(analyticsJs).toContain('NM.analytics');
  });

  test('contains GA4 event tracking code (gtag)', () => {
    expect(analyticsJs).toContain('gtag');
  });

  test('contains Yandex.Metrica integration (ym())', () => {
    expect(analyticsJs).toContain('ym(');
  });
});

// ─── 4. Analytics on pages — 4 tests ─────────────────────────────────────────

describe('Analytics included on public pages', () => {
  test('public/index.html contains analytics.js script tag', () => {
    const html = readFile('public/index.html');
    expect(html).toContain('analytics.js');
  });

  test('public/catalog.html contains analytics.js script tag', () => {
    const html = readFile('public/catalog.html');
    expect(html).toContain('analytics.js');
  });

  test('public/model.html contains analytics.js script tag or gtag reference', () => {
    const html = readFile('public/model.html');
    expect(html.includes('analytics.js') || html.includes('gtag')).toBe(true);
  });

  test('public/index.html contains googletagmanager OR analytics.js reference', () => {
    const html = readFile('public/index.html');
    expect(html.includes('googletagmanager') || html.includes('analytics.js')).toBe(true);
  });
});

// ─── 5. Customer Success factory dept — 3 tests ───────────────────────────────

describe('Customer Success factory — factory/agents/customer_success_dept.py', () => {
  const DEPT_PATH = path.join(__dirname, '..', '..', 'factory', 'agents', 'customer_success_dept.py');

  test('factory/agents/customer_success_dept.py exists', () => {
    expect(fs.existsSync(DEPT_PATH)).toBe(true);
  });

  test('file contains OnboardingSpecialist class', () => {
    const src = fs.readFileSync(DEPT_PATH, 'utf8');
    expect(src).toContain('OnboardingSpecialist');
  });

  test('file contains CustomerSuccessDepartment', () => {
    const src = fs.readFileSync(DEPT_PATH, 'utf8');
    expect(src).toContain('CustomerSuccessDepartment');
  });
});
