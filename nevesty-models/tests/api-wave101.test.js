'use strict';
/**
 * Wave 27-29 integration tests:
 *  1. SEO meta tags — og:image:alt & JSON-LD Person schema (wave 27)
 *  2. DB backup system — taskDatabaseBackup in scheduler.js (wave 28)
 *  3. CB_DATA constants — keyboards/constants.js (wave 28)
 *  4. Sitemap auto-regeneration — generateSitemap in api.js + robots.txt (wave 28)
 *  5. Frontend accessibility — aria-busy in catalog.js, lightbox alt in main.js (wave 26)
 */

const fs = require('fs');
const path = require('path');

const PUBLIC_DIR = path.join(__dirname, '../public');
const INDEX_HTML = path.join(PUBLIC_DIR, 'index.html');
const CATALOG_HTML = path.join(PUBLIC_DIR, 'catalog.html');
const MODEL_HTML = path.join(PUBLIC_DIR, 'model.html');
const ROBOTS_TXT = path.join(PUBLIC_DIR, 'robots.txt');
const CATALOG_JS = path.join(PUBLIC_DIR, 'js/catalog.js');
const MAIN_JS = path.join(PUBLIC_DIR, 'js/main.js');
const SCHEDULER_JS = path.join(__dirname, '../agents/scheduler.js');
const RESTORE_SH = path.join(__dirname, '../scripts/restore.sh');
const API_JS = path.join(__dirname, '../routes/api.js');

// ─── 1. SEO meta tags — wave 27 ───────────────────────────────────────────────

describe('SEO meta tags — wave 27', () => {
  it('index.html has og:image:alt', () => {
    const src = fs.readFileSync(INDEX_HTML, 'utf8');
    expect(src).toContain('og:image:alt');
  });

  it('catalog.html has og:image:alt', () => {
    const src = fs.readFileSync(CATALOG_HTML, 'utf8');
    expect(src).toContain('og:image:alt');
  });

  it('model.html has og:image:alt', () => {
    const src = fs.readFileSync(MODEL_HTML, 'utf8');
    expect(src).toContain('og:image:alt');
  });

  it('model.html has pre-JS application/ld+json script tag', () => {
    const src = fs.readFileSync(MODEL_HTML, 'utf8');
    expect(src).toContain('application/ld+json');
  });

  it('model.html has pre-JS Person schema placeholder', () => {
    const src = fs.readFileSync(MODEL_HTML, 'utf8');
    // The static placeholder script block must exist before JS hydrates it
    expect(src).toContain('"@type": "Person"');
  });
});

// ─── 2. DB backup system — wave 28 ───────────────────────────────────────────

describe('DB backup system', () => {
  it('scheduler.js has taskDatabaseBackup function', () => {
    const src = fs.readFileSync(SCHEDULER_JS, 'utf8');
    expect(src).toContain('taskDatabaseBackup');
  });

  it('scheduler.js runs backup every 6 hours via setInterval', () => {
    const src = fs.readFileSync(SCHEDULER_JS, 'utf8');
    // setInterval(taskDatabaseBackup, 6 * 60 * 60 * 1000) is the expected pattern
    expect(src).toMatch(/setInterval\s*\(\s*taskDatabaseBackup\s*,\s*6\s*\*\s*60\s*\*\s*60\s*\*\s*1000\s*\)/);
  });

  it('scripts/restore.sh exists and has correct shebang', () => {
    const src = fs.readFileSync(RESTORE_SH, 'utf8');
    expect(src.startsWith('#!/bin/bash')).toBe(true);
  });
});

// ─── 3. CB_DATA constants — wave 28 ──────────────────────────────────────────

describe('keyboards/constants.js CB_DATA', () => {
  it('exports CB_DATA object', () => {
    const { CB_DATA } = require('../keyboards/constants');
    expect(CB_DATA).toBeDefined();
    expect(typeof CB_DATA).toBe('object');
  });

  it('CB_DATA has MAIN_MENU key', () => {
    const { CB_DATA } = require('../keyboards/constants');
    expect(CB_DATA.MAIN_MENU).toBe('main_menu');
  });

  it('CB_DATA has ADMIN_MENU key', () => {
    const { CB_DATA } = require('../keyboards/constants');
    expect(CB_DATA.ADMIN_MENU).toBe('admin_menu');
  });

  it('CB_DATA has MY_ORDERS key', () => {
    const { CB_DATA } = require('../keyboards/constants');
    expect(CB_DATA.MY_ORDERS).toBe('my_orders');
  });

  it('CB_DATA has BK_START key', () => {
    const { CB_DATA } = require('../keyboards/constants');
    expect(CB_DATA.BK_START).toBe('bk_start');
  });
});

// ─── 4. Sitemap auto-regeneration — wave 28 ──────────────────────────────────

describe('Sitemap auto-regeneration', () => {
  it('routes/api.js calls generateSitemap after model toggle', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    // PATCH /models/:id (availability toggle) must fire generateSitemap
    expect(src).toContain('generateSitemap');
    // Errors are suppressed with .catch so it never crashes the handler
    expect(src).toMatch(/generateSitemap\(\)\.catch/);
  });

  it('routes/api.js generateSitemap catch logs [Sitemap] prefix', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    expect(src).toMatch(/generateSitemap.*catch.*Sitemap/s);
  });

  it('robots.txt has sitemap-models.xml directive', () => {
    const src = fs.readFileSync(ROBOTS_TXT, 'utf8');
    expect(src).toContain('sitemap-models.xml');
  });
});

// ─── 5. Frontend accessibility — wave 26 ─────────────────────────────────────

describe('Frontend accessibility fixes', () => {
  it('catalog.js sets aria-busy attribute', () => {
    const src = fs.readFileSync(CATALOG_JS, 'utf8');
    expect(src).toContain('aria-busy');
  });

  it('catalog.js resets aria-busy to false after skeleton hides', () => {
    const src = fs.readFileSync(CATALOG_JS, 'utf8');
    expect(src).toMatch(/aria-busy.*false/);
  });

  it('main.js lightbox sets dynamic alt text', () => {
    const src = fs.readFileSync(MAIN_JS, 'utf8');
    expect(src).toMatch(/\.alt\s*=/);
  });
});
