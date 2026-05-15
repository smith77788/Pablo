'use strict';
/**
 * Wave102 tests: memory cleanup Maps, TelegramChannelAgent factory,
 * cycle.py Phase 24, client keyboard rows, api.js error handling,
 * bot.js size check, multiple Map cleanups.
 */

const fs = require('fs');
const path = require('path');

const botSrc = fs.readFileSync(path.join(__dirname, '..', 'bot.js'), 'utf8');
const channelSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'factory', 'agents', 'channel_content.py'), 'utf8');
const cycleSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'factory', 'cycle.py'), 'utf8');
const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'routes', 'api.js'), 'utf8');

// ─── 1. catalogSortPrefs memory cleanup (3 tests) ────────────────────────────

describe('catalogSortPrefs memory cleanup', () => {
  test('bot.js defines catalogSortPrefs Map', () => {
    expect(botSrc).toContain('catalogSortPrefs');
    expect(botSrc).toContain('new Map()');
  });

  test('bot.js contains setInterval for catalogSortPrefs cleanup', () => {
    expect(botSrc).toContain('catalogSortPrefs.clear()');
  });

  test('catalogSortPrefs cleanup uses 12-hour interval', () => {
    // The cleanup should reference 12 * 60 * 60 * 1000 near catalogSortPrefs.clear
    const clearIdx = botSrc.indexOf('catalogSortPrefs.clear()');
    const context = botSrc.slice(Math.max(0, clearIdx - 300), clearIdx + 100);
    expect(context).toMatch(/12\s*\*\s*60\s*\*\s*60\s*\*\s*1000/);
  });
});

// ─── 2. TelegramChannelAgent in factory (4 tests) ────────────────────────────

describe('TelegramChannelAgent in factory/agents/channel_content.py', () => {
  test('factory/agents/channel_content.py exists and is readable', () => {
    expect(channelSrc.length).toBeGreaterThan(100);
  });

  test('channel_content.py contains TelegramChannelAgent class', () => {
    expect(channelSrc).toContain('TelegramChannelAgent');
  });

  test('channel_content.py contains generate_model_spotlight method', () => {
    expect(channelSrc).toContain('generate_model_spotlight');
  });

  test('channel_content.py contains generate_promo_post method', () => {
    expect(channelSrc).toContain('generate_promo_post');
  });
});

// ─── 3. cycle.py Phase 24 uses TelegramChannelAgent (2 tests) ────────────────

describe('cycle.py Phase 24 uses TelegramChannelAgent', () => {
  test('factory/cycle.py imports or references TelegramChannelAgent', () => {
    expect(cycleSrc).toContain('TelegramChannelAgent');
  });

  test('factory/cycle.py calls generate_model_spotlight or generate_promo_post', () => {
    const hasSpotlight = cycleSrc.includes('generate_model_spotlight');
    const hasPromo = cycleSrc.includes('generate_promo_post');
    expect(hasSpotlight || hasPromo).toBe(true);
  });
});

// ─── 4. Client keyboard 7 rows max (3 tests) ─────────────────────────────────

describe('buildClientKeyboard rows limit', () => {
  test('buildClientKeyboard function exists in bot.js', () => {
    expect(botSrc).toContain('async function buildClientKeyboard()');
  });

  test('buildClientKeyboard does not push more than 8 rows total', () => {
    const fnStart = botSrc.indexOf('async function buildClientKeyboard()');
    const fnEnd = botSrc.indexOf('\nreturn { inline_keyboard: rows }', fnStart) + 50;
    const fnBody = botSrc.slice(fnStart, fnEnd);
    // Count rows.push calls inside the function
    const pushMatches = fnBody.match(/rows\.push/g) || [];
    expect(pushMatches.length).toBeLessThanOrEqual(8);
  });

  test('buildClientKeyboard returns inline_keyboard object', () => {
    const fnStart = botSrc.indexOf('async function buildClientKeyboard()');
    const fnEnd = botSrc.indexOf('\n}', fnStart) + 2;
    const fnBody = botSrc.slice(fnStart, fnEnd);
    expect(fnBody).toContain('inline_keyboard');
    expect(fnBody).toContain('return');
  });
});

// ─── 5. API routes error handling (3 tests) ──────────────────────────────────

describe('routes/api.js error handling coverage', () => {
  test('routes/api.js has at least 150 async route handlers or functions', () => {
    const asyncCount = (apiSrc.match(/async\b/g) || []).length;
    expect(asyncCount).toBeGreaterThanOrEqual(150);
  });

  test('routes/api.js forwards errors with next(e) at least 50 times', () => {
    const nextECount = (apiSrc.match(/next\(e\)/g) || []).length;
    expect(nextECount).toBeGreaterThanOrEqual(50);
  });

  test('routes/api.js has at least 100 try/catch blocks', () => {
    const tryCatchCount = (apiSrc.match(/\btry\s*\{/g) || []).length;
    expect(tryCatchCount).toBeGreaterThanOrEqual(100);
  });
});

// ─── 6. bot.js size check (2 tests) ──────────────────────────────────────────

describe('bot.js size and imports', () => {
  test('bot.js line count is less than 15000 (technical debt threshold)', () => {
    const lines = botSrc.split('\n').length;
    expect(lines).toBeLessThan(15000);
  });

  test('bot.js imports STATUS_LABELS and EVENT_TYPES from utils/constants', () => {
    expect(botSrc).toContain('STATUS_LABELS');
    expect(botSrc).toContain('EVENT_TYPES');
    expect(botSrc).toContain('utils/constants');
  });
});

// ─── 7. Memory Maps have cleanup (2 tests) ───────────────────────────────────

describe('Memory Maps cleanup intervals', () => {
  test('bot.js contains searchFilters Map with cleanup', () => {
    expect(botSrc).toContain('searchFilters');
    expect(botSrc).toContain('searchFilters.clear()');
  });

  test('bot.js contains at least 3 setInterval cleanup operations', () => {
    const intervals = (botSrc.match(/setInterval/g) || []).length;
    expect(intervals).toBeGreaterThanOrEqual(3);
  });
});
