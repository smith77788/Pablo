'use strict';
/**
 * Wave104 tests: broadcast preview, contact page dynamic settings,
 * gzip compression, static caching, factory notifier, KB_MAIN_ADMIN sub-menus,
 * strings.js coverage.
 */

const fs = require('fs');
const path = require('path');

const botSrc = fs.readFileSync(path.join(__dirname, '..', 'bot.js'), 'utf8');
const serverSrc = fs.readFileSync(path.join(__dirname, '..', 'server.js'), 'utf8');
const contactHtml = fs.readFileSync(path.join(__dirname, '..', 'public', 'contact.html'), 'utf8');
const strSrc = fs.readFileSync(path.join(__dirname, '..', 'strings.js'), 'utf8');
const notifierSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'factory', 'notifier.py'), 'utf8');

// ─── 1. Broadcast preview step (4 tests) ─────────────────────────────────────

describe('Broadcast preview step', () => {
  test('bot.js contains broadcast preview state', () => {
    expect(botSrc).toMatch(/broadcast.*preview|preview.*broadcast|adm_broadcast_preview/i);
  });

  test('bot.js contains preview confirmation button before sending', () => {
    expect(botSrc).toMatch(/adm_broadcast_confirm/);
  });

  test('bot.js shows delivery stats after broadcast (delivered/failed count)', () => {
    // After broadcast completes, bot reports delivered and failed counts
    expect(botSrc).toMatch(/delivered[\s\S]{0,200}failed|failed[\s\S]{0,200}delivered/i);
  });

  test('bot.js has broadcast with photo support (sendPhoto in broadcast context)', () => {
    // broadcastPhotoId / sendBroadcastWithPhoto ensures photos can be broadcast
    expect(botSrc).toMatch(/broadcastPhotoId|sendBroadcastWithPhoto|broadcast.*photo/i);
  });
});

// ─── 2. Contact page dynamic settings (3 tests) ───────────────────────────────

describe('Contact page dynamic settings', () => {
  test('public/contact.html exists and is readable', () => {
    expect(contactHtml.length).toBeGreaterThan(0);
  });

  test('public/contact.html contains fetch to /api/settings/public', () => {
    expect(contactHtml).toContain('/api/settings/public');
  });

  test('public/contact.html contains WhatsApp button element (id with whatsapp)', () => {
    expect(contactHtml).toMatch(/id=["']whatsapp|class=["'][^"']*whatsapp/i);
  });
});

// ─── 3. gzip compression in server.js (2 tests) ───────────────────────────────

describe('gzip compression in server.js', () => {
  test('server.js requires compression module', () => {
    expect(serverSrc).toContain("require('compression')");
  });

  test('server.js calls app.use(compression...)', () => {
    expect(serverSrc).toMatch(/app\.use\s*\(\s*compression/);
  });
});

// ─── 4. Static caching headers (2 tests) ─────────────────────────────────────

describe('Static caching headers in server.js', () => {
  test('server.js has maxAge or Cache-Control for static files', () => {
    expect(serverSrc).toMatch(/maxAge|Cache-Control/);
  });

  test('server.js has separate caching for /uploads or /js paths', () => {
    // Separate express.static for /uploads with its own maxAge
    expect(serverSrc).toMatch(/['"]\/uploads['"]\s*[\s\S]{0,200}maxAge|maxAge[\s\S]{0,200}['"]\/uploads['"]/);
  });
});

// ─── 5. Factory notifier module (4 tests) ────────────────────────────────────

describe('Factory notifier module', () => {
  test('factory/notifier.py exists and is readable', () => {
    expect(notifierSrc.length).toBeGreaterThan(0);
  });

  test('factory/notifier.py contains TOKEN = os.getenv()', () => {
    expect(notifierSrc).toMatch(/TOKEN\s*=\s*os\.getenv/);
  });

  test('notify_cycle_complete function handles empty results gracefully (uses .get())', () => {
    // Uses dict.get() to safely access potentially missing keys
    // Match until next top-level def or end of file
    const fnMatch = notifierSrc.match(/def notify_cycle_complete[\s\S]+?(?=\ndef )|def notify_cycle_complete[\s\S]+$/);
    expect(fnMatch).not.toBeNull();
    expect(fnMatch[0]).toContain('.get(');
  });

  test('send_telegram handles network errors (try/except block)', () => {
    const fnMatch = notifierSrc.match(/def send_telegram[\s\S]+?(?=\ndef |\Z)/);
    expect(fnMatch).not.toBeNull();
    expect(fnMatch[0]).toMatch(/try:|except/);
  });
});

// ─── 6. Admin sub-menus in KB_MAIN_ADMIN (2 tests) ───────────────────────────

describe('Admin sub-menus in KB_MAIN_ADMIN', () => {
  test('bot.js KB_MAIN_ADMIN has analytics, marketing, team, and factory sub-menu buttons', () => {
    expect(botSrc).toContain('adm_menu_analytics');
    expect(botSrc).toContain('adm_menu_marketing');
    expect(botSrc).toContain('adm_menu_team');
    expect(botSrc).toContain('adm_menu_factory');
  });

  test('KB_MAIN_ADMIN inline_keyboard does not have more than 6 rows', () => {
    // Extract the KB_MAIN_ADMIN function body and count top-level array entries
    const kbMatch = botSrc.match(/const KB_MAIN_ADMIN\s*=[\s\S]+?inline_keyboard:\s*\[([\s\S]+?)\],\s*\}/);
    expect(kbMatch).not.toBeNull();
    // Count rows: each row is a [ ... ] array inside the inline_keyboard array
    const rows = kbMatch[1].match(/\[[\s\S]*?\{[\s\S]*?\}\s*\]/g) || [];
    expect(rows.length).toBeLessThanOrEqual(6);
  });
});

// ─── 7. strings.js coverage (2 tests) ────────────────────────────────────────

describe('strings.js coverage', () => {
  test('strings.js has STRINGS.errorAccessDeniedShort', () => {
    expect(strSrc).toContain('errorAccessDeniedShort');
  });

  test('bot.js uses STRINGS constants at least 100 times', () => {
    const stringsCount = (botSrc.match(/STRINGS\./g) || []).length;
    expect(stringsCount).toBeGreaterThan(100);
  });
});
