'use strict';

// ── Env setup BEFORE any app module is loaded ────────────────────────────────
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');

// ─── 1. calc_share whitelist validation (4 tests) ────────────────────────────

describe('calc_share whitelist validation', () => {
  const botSrc = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');

  test('calc_share callback regex pattern exists in bot.js', () => {
    expect(botSrc).toMatch(/calc_share_\(\\d\+\)_\(\\d\+\)_\(\.+\)/);
  });

  test('bot.js contains VALID_SHARE_EVENT_TYPES whitelist for evType validation', () => {
    expect(botSrc).toMatch(/VALID_SHARE_EVENT_TYPES/);
  });

  test('calcModels and calcHours clamping with Math.min exists', () => {
    // Lines 5974-5975: Math.min(Math.max(parseInt(...), 1), 50) and similar
    expect(botSrc).toMatch(/calcModels\s*=\s*Math\.min/);
    expect(botSrc).toMatch(/calcHours\s*=\s*Math\.min/);
  });

  test('calc_share callback handler exists', () => {
    expect(botSrc).toMatch(/calc_share/);
    // The handler reads the matched groups [, modelsStr, hoursStr, evType]
    expect(botSrc).toMatch(/modelsStr.*hoursStr.*evType|evType.*modelsStr.*hoursStr/);
  });
});

// ─── 2. WhatsApp phone guard (3 tests) ───────────────────────────────────────

describe('WhatsApp phone guard in routes/api.js', () => {
  const apiSrc = fs.readFileSync(path.join(ROOT, 'routes', 'api.js'), 'utf8');

  test('contains waPhone.length >= 7 minimum length guard', () => {
    expect(apiSrc).toMatch(/waPhone\.length\s*>=\s*7/);
  });

  test('contains phone sanitization with .replace(/\\D/g)', () => {
    // Matches: .replace(/\D/g, '')
    expect(apiSrc).toMatch(/\.replace\(\/\\D\/g,\s*''\)/);
  });

  test('sendText call is wrapped with .catch() making it non-blocking', () => {
    expect(apiSrc).toMatch(/sendText\([^)]+\)\.catch\(/);
  });
});

// ─── 3. robots.txt (3 tests) ─────────────────────────────────────────────────

describe('robots.txt', () => {
  const robotsPath = path.join(ROOT, 'public', 'robots.txt');
  let robotsSrc;

  beforeAll(() => {
    robotsSrc = fs.readFileSync(robotsPath, 'utf8');
  });

  test('public/robots.txt exists', () => {
    expect(fs.existsSync(robotsPath)).toBe(true);
  });

  test('robots.txt contains "User-agent: *"', () => {
    expect(robotsSrc).toContain('User-agent: *');
  });

  test('robots.txt contains "Disallow: /admin/"', () => {
    expect(robotsSrc).toContain('Disallow: /admin/');
  });
});

// ─── 4. Open Graph on index.html (3 tests) ───────────────────────────────────

describe('Open Graph on index.html', () => {
  const indexPath = path.join(ROOT, 'public', 'index.html');
  let indexSrc;

  beforeAll(() => {
    indexSrc = fs.readFileSync(indexPath, 'utf8');
  });

  test('public/index.html exists', () => {
    expect(fs.existsSync(indexPath)).toBe(true);
  });

  test('index.html contains og:title meta tag', () => {
    expect(indexSrc).toMatch(/og:title/);
  });

  test('index.html contains og:description meta tag', () => {
    expect(indexSrc).toMatch(/og:description/);
  });
});

// ─── 5. Open Graph on catalog.html (3 tests) ─────────────────────────────────

describe('Open Graph on catalog.html', () => {
  const catalogPath = path.join(ROOT, 'public', 'catalog.html');
  let catalogSrc;

  beforeAll(() => {
    catalogSrc = fs.readFileSync(catalogPath, 'utf8');
  });

  test('public/catalog.html exists', () => {
    expect(fs.existsSync(catalogPath)).toBe(true);
  });

  test('catalog.html contains og:title meta tag', () => {
    expect(catalogSrc).toMatch(/og:title/);
  });

  test('catalog.html contains canonical link', () => {
    expect(catalogSrc).toMatch(/rel="canonical"/);
  });
});

// ─── 6. model.html Schema.org (3 tests) ──────────────────────────────────────

describe('model.html Schema.org and Open Graph', () => {
  const modelPath = path.join(ROOT, 'public', 'model.html');
  let modelSrc;

  beforeAll(() => {
    modelSrc = fs.readFileSync(modelPath, 'utf8');
  });

  test('model.html contains application/ld+json script type', () => {
    expect(modelSrc).toContain('application/ld+json');
  });

  test('model.html contains schema.org reference', () => {
    expect(modelSrc).toMatch(/schema\.org/);
  });

  test('model.html contains og:image meta tag', () => {
    expect(modelSrc).toMatch(/og:image/);
  });
});
