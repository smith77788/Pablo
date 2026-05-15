'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');
const stringsCode = fs.readFileSync(path.join(ROOT, 'strings.js'), 'utf8');
const settingsHtml = fs.readFileSync(path.join(ROOT, 'public', 'admin', 'settings.html'), 'utf8');

const contentDeptPath = path.join('/home/user/Pablo/factory/agents/content_dept.py');
const contentDeptCode = fs.existsSync(contentDeptPath) ? fs.readFileSync(contentDeptPath, 'utf8') : '';

// ─── T1: About/Pricing banner photos (bot.js) ────────────────────────────────

describe('T1: About/Pricing banner photos (bot.js)', () => {
  test('T01: showAboutUs reads about_photo_url setting', () => {
    // showAboutUs must call getSetting with 'about_photo_url'
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]about_photo_url['"]\s*\)/);
  });

  test('T02: showAboutUs tries bot.sendPhoto when about_photo_url is set', () => {
    // After fetching aboutPhoto, the function calls bot.sendPhoto
    // Both getSetting('about_photo_url') and bot.sendPhoto appear close together
    // in the showAboutUs function body
    const fnMatch = botCode.match(/async function showAboutUs[\s\S]{0,2000}?bot\.sendPhoto/);
    expect(fnMatch).not.toBeNull();
  });

  test('T03: showPricing reads pricing_photo_url setting', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]pricing_photo_url['"]\s*\)/);
  });

  test('T04: adm_set_about_photo maps to about_photo_url in text setting handlers', () => {
    // The handler maps adm_set_about_photo -> about_photo_url
    expect(botCode).toMatch(/adm_set_about_photo\s*:\s*\[\s*['"]about_photo_url['"]/);
  });

  test('T05: adm_set_pricing_photo maps to pricing_photo_url in text setting handlers', () => {
    // The handler maps adm_set_pricing_photo -> pricing_photo_url
    expect(botCode).toMatch(/adm_set_pricing_photo\s*:\s*\[\s*['"]pricing_photo_url['"]/);
  });

  test('T06: UI settings section has adm_set_about_photo button', () => {
    // Inline keyboard button with callback_data 'adm_set_about_photo'
    expect(botCode).toMatch(/callback_data\s*:\s*['"]adm_set_about_photo['"]/);
  });
});

// ─── T2: Strings.js expansion ────────────────────────────────────────────────

describe('T2: Strings.js expansion', () => {
  test('T07: strings.js has wishlistTitle key', () => {
    expect(stringsCode).toMatch(/wishlistTitle\s*:/);
  });

  test('T08: strings.js has reviewsTitle key', () => {
    expect(stringsCode).toMatch(/reviewsTitle\s*:/);
  });

  test('T09: strings.js has faqTitle key', () => {
    expect(stringsCode).toMatch(/faqTitle\s*:/);
  });

  test('T10: strings.js has errorTooManyRequests key', () => {
    expect(stringsCode).toMatch(/errorTooManyRequests\s*:/);
  });

  test('T11: strings.js has ordersEmpty key', () => {
    expect(stringsCode).toMatch(/ordersEmpty\s*:/);
  });

  test('T12: strings.js exports at least 200 keys (module.exports has 200+ properties)', () => {
    // Count property lines in the module.exports object block
    // Each key is a line like "  someKey: '...',"
    const stringsObj = require(path.join(ROOT, 'strings.js'));
    const keyCount = Object.keys(stringsObj).length;
    expect(keyCount).toBeGreaterThanOrEqual(200);
  });
});

// ─── T3: Admin settings.html completeness ────────────────────────────────────

describe('T3: Admin settings.html completeness', () => {
  test('T13: settings.html has booking_thanks_text textarea', () => {
    expect(settingsHtml).toMatch(/id\s*=\s*["']booking_thanks_text["']/);
  });

  test('T14: settings.html has notif_new_order toggle', () => {
    expect(settingsHtml).toMatch(/id\s*=\s*["']notif_new_order["']/);
  });

  test('T15: settings.html has link to /admin/social.html in social section', () => {
    expect(settingsHtml).toMatch(/href\s*=\s*["']\/admin\/social\.html["']/);
  });

  test('T16: settings.html includes saveBooking function', () => {
    expect(settingsHtml).toMatch(/function saveBooking\s*\(/);
  });
});

// ─── T4: Factory WeeklySummaryAgent fields ───────────────────────────────────

describe('T4: Factory WeeklySummaryAgent fields (factory/agents/content_dept.py)', () => {
  beforeAll(() => {
    if (!contentDeptCode) {
      console.warn('content_dept.py not found — T4 tests will check against empty string');
    }
  });

  test('T17: WeeklySummaryAgent.generate_summary collects orders_week', () => {
    expect(contentDeptCode).toMatch(/orders_week/);
  });

  test('T18: WeeklySummaryAgent.generate_summary collects revenue_week', () => {
    expect(contentDeptCode).toMatch(/revenue_week/);
  });

  test('T19: WeeklySummaryAgent.format_telegram_message uses Markdown formatting', () => {
    // The format_telegram_message method should use bold Markdown markers (*...*)
    const fnBlock = contentDeptCode.match(/def format_telegram_message[\s\S]{0,1000}?(?=\n    def |\nclass )/);
    if (fnBlock) {
      // Expects asterisks used for bold in Telegram Markdown
      expect(fnBlock[0]).toMatch(/\*/);
    } else {
      // Fallback: check the whole file contains Markdown bold markers near telegram output
      expect(contentDeptCode).toMatch(/format_telegram_message[\s\S]{0,500}\*/);
    }
  });
});
