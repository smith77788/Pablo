'use strict';
/**
 * Wave99 tests: Docker config, deploy.sh, email mailer, strings, .env.example, analytics
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

function fileExistsAbsolute(absPath) {
  try {
    fs.accessSync(absPath);
    return true;
  } catch {
    return false;
  }
}

// ─── 1. Docker configuration (4 tests) ────────────────────────────────────────

describe('Docker configuration', () => {
  test('Dockerfile exists', () => {
    const dockerfilePath = path.join(__dirname, '..', 'Dockerfile');
    expect(fs.existsSync(dockerfilePath)).toBe(true);
  });

  test('Dockerfile contains FROM node: (base image)', () => {
    const dockerfile = fs.readFileSync(path.join(__dirname, '..', 'Dockerfile'), 'utf8');
    expect(dockerfile).toMatch(/FROM node:/);
  });

  test('docker-compose.yml exists', () => {
    expect(fileExists('docker-compose.yml')).toBe(true);
  });

  test('docker-compose.yml contains healthcheck', () => {
    const content = readFile('docker-compose.yml');
    expect(content).toMatch(/healthcheck|health_check/);
  });
});

// ─── 2. deploy.sh (2 tests) ───────────────────────────────────────────────────

describe('deploy.sh', () => {
  const deployPath = path.join(__dirname, '..', '..', 'deploy.sh');

  test('/home/user/Pablo/deploy.sh exists', () => {
    expect(fileExistsAbsolute(deployPath)).toBe(true);
  });

  test('deploy.sh contains docker-compose, npm install, or node command', () => {
    const content = fs.readFileSync(deployPath, 'utf8');
    expect(content).toMatch(/docker-compose|npm install|node /);
  });
});

// ─── 3. Email mailer service (4 tests) ────────────────────────────────────────

describe('Email mailer service — services/mailer.js', () => {
  test('services/mailer.js exists', () => {
    expect(fileExists('services/mailer.js')).toBe(true);
  });

  test('exports sendOrderConfirmation', () => {
    const mailer = require('../services/mailer');
    expect(typeof mailer.sendOrderConfirmation).toBe('function');
  });

  test('exports sendStatusChange', () => {
    const mailer = require('../services/mailer');
    expect(typeof mailer.sendStatusChange).toBe('function');
  });

  test('sendOrderConfirmation returns undefined/promise when email param is falsy (graceful no-op)', async () => {
    const mailer = require('../services/mailer');
    // Should not throw even with null email
    let error = null;
    let result;
    try {
      result = mailer.sendOrderConfirmation(null, {});
      if (result && typeof result.then === 'function') {
        result = await result;
      }
    } catch (e) {
      error = e;
    }
    expect(error).toBeNull();
    // result is either undefined or null — both are acceptable no-ops
    expect(result == null).toBe(true);
  });
});

// ─── 4. strings.js completeness (3 tests) ─────────────────────────────────────

describe('strings.js completeness', () => {
  const strings = require('../strings');
  const STRINGS = strings.STRINGS || strings;

  test('strings.js exports an object with 200+ keys', () => {
    expect(typeof STRINGS).toBe('object');
    expect(STRINGS).not.toBeNull();
    expect(Object.keys(STRINGS).length).toBeGreaterThanOrEqual(200);
  });

  test('contains btnBack or back button key', () => {
    const keys = Object.keys(STRINGS);
    const hasBack = keys.some(k => k === 'btnBack' || k.toLowerCase().includes('back'));
    expect(hasBack).toBe(true);
  });

  test('contains errorGeneric or generic error key', () => {
    const keys = Object.keys(STRINGS);
    const hasGenericError = keys.some(
      k =>
        k === 'errorGeneric' ||
        k === 'errorGeneral' ||
        (k.toLowerCase().startsWith('error') && k.toLowerCase().includes('generic'))
    );
    expect(hasGenericError).toBe(true);
  });
});

// ─── 5. .env.example completeness (4 tests) ───────────────────────────────────

describe('.env.example completeness', () => {
  test('.env.example exists', () => {
    expect(fileExists('.env.example')).toBe(true);
  });

  test('.env.example contains TELEGRAM_BOT_TOKEN', () => {
    const content = readFile('.env.example');
    expect(content).toContain('TELEGRAM_BOT_TOKEN');
  });

  test('.env.example contains JWT_SECRET', () => {
    const content = readFile('.env.example');
    expect(content).toContain('JWT_SECRET');
  });

  test('.env.example contains SMTP_HOST', () => {
    const content = readFile('.env.example');
    expect(content).toContain('SMTP_HOST');
  });
});

// ─── 6. analytics.js (3 tests) ────────────────────────────────────────────────

describe('analytics.js', () => {
  test('public/js/analytics.js exists', () => {
    expect(fileExists('public/js/analytics.js')).toBe(true);
  });

  test('analytics.js contains nmTrack or NM.analytics', () => {
    const content = readFile('public/js/analytics.js');
    expect(content).toMatch(/nmTrack|NM\.analytics/);
  });

  test('catalog.html references analytics.js', () => {
    const content = readFile('public/catalog.html');
    expect(content).toContain('analytics.js');
  });
});
