'use strict';
/**
 * Unit tests for bot.js helper functions — БЛОК 7.1
 *
 * Pure helpers are sourced from utils/helpers.js (esc, ru_plural, formatPhone,
 * normalizePhone, formatCurrency, truncate) which bot.js indirectly relies on.
 *
 * Bot-internal pure helpers (bookingProgress, stepHeader, formatDateShort,
 * groupBusyDatesIntoRanges) are re-implemented inline from their bot.js
 * definitions — no bot initialisation required.
 */

// ─── Helpers from utils/helpers.js ───────────────────────────────────────────
const { esc, ru_plural, formatPhone, normalizePhone, formatCurrency, truncate } = require('../utils/helpers');

// ─── Bot-internal pure helpers (re-implemented from bot.js) ──────────────────

// bookingProgress (bot.js line ~268)
function bookingProgress(step, total = 4) {
  const filled = '▓'.repeat(step);
  const empty = '░'.repeat(total - step);
  return `${filled}${empty} Шаг ${step}/${total}`;
}

// stepHeader (bot.js line ~1442)
function stepHeader(step, title) {
  const dots = ['●', '●', '●', '●'].map((d, i) => (i < step ? '●' : '○')).join(' ');
  return `📝 *Бронирование · Шаг ${step}/4*\n${dots}\n\n*${title}*\n\n`;
}

// formatDateShort (bot.js line ~1170)
const MONTHS_RU = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
function formatDateShort(dateStr) {
  const [, m, d] = dateStr.split('-');
  return `${parseInt(d)} ${MONTHS_RU[parseInt(m) - 1]}`;
}

// groupBusyDatesIntoRanges (bot.js line ~1177)
function groupBusyDatesIntoRanges(rows) {
  if (!rows.length) return [];
  const ranges = [];
  let start = rows[0].busy_date;
  let end = rows[0].busy_date;
  let reason = rows[0].reason || '';
  for (let i = 1; i < rows.length; i++) {
    const prev = new Date(end);
    const cur = new Date(rows[i].busy_date);
    prev.setDate(prev.getDate() + 1);
    const sameReason = (rows[i].reason || '') === reason;
    if (cur.toISOString().slice(0, 10) === prev.toISOString().slice(0, 10) && sameReason) {
      end = rows[i].busy_date;
    } else {
      ranges.push({ start, end, reason });
      start = rows[i].busy_date;
      end = rows[i].busy_date;
      reason = rows[i].reason || '';
    }
  }
  ranges.push({ start, end, reason });
  return ranges;
}

// ─── esc() ────────────────────────────────────────────────────────────────────

describe('esc() — MarkdownV2 escape', () => {
  test('escapes underscore', () => {
    expect(esc('hello_world')).toBe('hello\\_world');
  });

  test('escapes asterisk', () => {
    expect(esc('bold*text')).toBe('bold\\*text');
  });

  test('escapes square brackets', () => {
    expect(esc('[link]')).toBe('\\[link\\]');
  });

  test('escapes parentheses', () => {
    expect(esc('(test)')).toBe('\\(test\\)');
  });

  test('escapes dot', () => {
    expect(esc('3.14')).toBe('3\\.14');
  });

  test('escapes exclamation mark', () => {
    expect(esc('Hello!')).toBe('Hello\\!');
  });

  test('escapes dash', () => {
    expect(esc('a-b')).toBe('a\\-b');
  });

  test('escapes backslash', () => {
    expect(esc('a\\b')).toBe('a\\\\b');
  });

  test('escapes tilde', () => {
    expect(esc('a~b')).toBe('a\\~b');
  });

  test('escapes hash', () => {
    expect(esc('a#b')).toBe('a\\#b');
  });

  test('escapes pipe', () => {
    expect(esc('a|b')).toBe('a\\|b');
  });

  test('escapes greater-than', () => {
    expect(esc('a>b')).toBe('a\\>b');
  });

  test('escapes plus', () => {
    expect(esc('a+b')).toBe('a\\+b');
  });

  test('escapes equals', () => {
    expect(esc('a=b')).toBe('a\\=b');
  });

  test('escapes backtick', () => {
    expect(esc('a`b')).toBe('a\\`b');
  });

  test('escapes curly braces', () => {
    expect(esc('a{b}c')).toBe('a\\{b\\}c');
  });

  test('leaves plain alphanumerics unchanged', () => {
    expect(esc('ABC123xyz')).toBe('ABC123xyz');
  });

  test('leaves Cyrillic text unchanged', () => {
    expect(esc('Привет мир')).toBe('Привет мир');
  });

  test('coerces numbers to string and escapes', () => {
    expect(esc(3.14)).toBe('3\\.14');
  });

  test('coerces integer to string without modification', () => {
    expect(esc(42)).toBe('42');
  });

  test('escapes a full order number with hyphens', () => {
    expect(esc('ORD-2025-001')).toBe('ORD\\-2025\\-001');
  });

  test('escapes a URL-like string', () => {
    expect(esc('https://example.com/path?a=1&b=2')).toBe('https://example\\.com/path?a\\=1&b\\=2');
  });
});

// ─── ru_plural() ──────────────────────────────────────────────────────────────

describe('ru_plural() — Russian pluralization', () => {
  test('1 → singular (заявка)', () => {
    expect(ru_plural(1, 'заявка', 'заявки', 'заявок')).toBe('заявка');
  });

  test('2 → few (заявки)', () => {
    expect(ru_plural(2, 'заявка', 'заявки', 'заявок')).toBe('заявки');
  });

  test('4 → few (заявки)', () => {
    expect(ru_plural(4, 'заявка', 'заявки', 'заявок')).toBe('заявки');
  });

  test('5 → many (заявок)', () => {
    expect(ru_plural(5, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('11 → many (exception — not singular)', () => {
    expect(ru_plural(11, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('12 → many (exception)', () => {
    expect(ru_plural(12, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('14 → many (exception)', () => {
    expect(ru_plural(14, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('21 → singular again', () => {
    expect(ru_plural(21, 'заявка', 'заявки', 'заявок')).toBe('заявка');
  });

  test('22 → few again', () => {
    expect(ru_plural(22, 'заявка', 'заявки', 'заявок')).toBe('заявки');
  });

  test('0 → many', () => {
    expect(ru_plural(0, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('100 → many', () => {
    expect(ru_plural(100, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('works with "день/дня/дней"', () => {
    expect(ru_plural(1, 'день', 'дня', 'дней')).toBe('день');
    expect(ru_plural(3, 'день', 'дня', 'дней')).toBe('дня');
    expect(ru_plural(7, 'день', 'дня', 'дней')).toBe('дней');
  });

  test('works with "модель/модели/моделей"', () => {
    expect(ru_plural(1, 'модель', 'модели', 'моделей')).toBe('модель');
    expect(ru_plural(3, 'модель', 'модели', 'моделей')).toBe('модели');
    expect(ru_plural(10, 'модель', 'модели', 'моделей')).toBe('моделей');
  });
});

// ─── formatPhone() ────────────────────────────────────────────────────────────

describe('formatPhone() — Russian phone formatting', () => {
  test('formats 11-digit number starting with 7', () => {
    expect(formatPhone('79991234567')).toBe('+7 (999) 123-45-67');
  });

  test('formats 11-digit number starting with 8', () => {
    expect(formatPhone('89161234567')).toBe('+8 (916) 123-45-67');
  });

  test('strips non-digit chars before formatting', () => {
    expect(formatPhone('+7 (999) 123-45-67')).toBe('+7 (999) 123-45-67');
  });

  test('returns original value if not 11 digits', () => {
    expect(formatPhone('1234')).toBe('1234');
  });

  test('handles null gracefully', () => {
    expect(formatPhone(null)).toBe(null);
  });

  test('handles empty string', () => {
    expect(formatPhone('')).toBe('');
  });
});

// ─── normalizePhone() ─────────────────────────────────────────────────────────

describe('normalizePhone() — phone normalization', () => {
  test('strips all non-digits', () => {
    expect(normalizePhone('+7 (999) 123-45-67')).toBe('79991234567');
  });

  test('replaces leading 8 with 7 for 11-digit numbers', () => {
    expect(normalizePhone('89161234567')).toBe('79161234567');
  });

  test('keeps 7 prefix unchanged', () => {
    expect(normalizePhone('79161234567')).toBe('79161234567');
  });

  test('does not convert short number starting with 8', () => {
    expect(normalizePhone('8800')).toBe('8800');
  });

  test('handles null gracefully', () => {
    expect(normalizePhone(null)).toBe('');
  });

  test('handles empty string', () => {
    expect(normalizePhone('')).toBe('');
  });
});

// ─── formatCurrency() ─────────────────────────────────────────────────────────

describe('formatCurrency() — currency formatting', () => {
  test('formats thousands with Ru locale separator', () => {
    expect(formatCurrency(15000)).toMatch(/15.000\s*₽/);
  });

  test('returns em-dash for falsy values', () => {
    expect(formatCurrency(0)).toBe('—');
    expect(formatCurrency(null)).toBe('—');
    expect(formatCurrency(undefined)).toBe('—');
  });

  test('formats small amount', () => {
    expect(formatCurrency(500)).toMatch(/500\s*₽/);
  });

  test('includes ₽ symbol', () => {
    expect(formatCurrency(1000)).toContain('₽');
  });
});

// ─── truncate() ───────────────────────────────────────────────────────────────

describe('truncate() — text truncation', () => {
  test('short text passes through unchanged', () => {
    expect(truncate('hello', 10)).toBe('hello');
  });

  test('truncates long text and adds ellipsis', () => {
    const result = truncate('abcdefghij', 5);
    expect(result).toHaveLength(5);
    expect(result.endsWith('…')).toBe(true);
  });

  test('uses default maxLen of 100', () => {
    const long = 'a'.repeat(150);
    const result = truncate(long);
    expect(result.length).toBeLessThanOrEqual(100);
    expect(result.endsWith('…')).toBe(true);
  });

  test('handles empty string', () => {
    expect(truncate('')).toBe('');
  });

  test('handles null gracefully', () => {
    expect(truncate(null)).toBe('');
  });

  test('does not truncate text at exact limit', () => {
    const text = 'a'.repeat(100);
    expect(truncate(text, 100)).toBe(text);
  });
});

// ─── bookingProgress() ───────────────────────────────────────────────────────

describe('bookingProgress() — progress bar helper', () => {
  test('step 1 of 4: one filled block', () => {
    expect(bookingProgress(1, 4)).toBe('▓░░░ Шаг 1/4');
  });

  test('step 2 of 4: two filled blocks', () => {
    expect(bookingProgress(2, 4)).toBe('▓▓░░ Шаг 2/4');
  });

  test('step 3 of 4: three filled blocks', () => {
    expect(bookingProgress(3, 4)).toBe('▓▓▓░ Шаг 3/4');
  });

  test('step 4 of 4: all filled', () => {
    expect(bookingProgress(4, 4)).toBe('▓▓▓▓ Шаг 4/4');
  });

  test('defaults total to 4', () => {
    expect(bookingProgress(2)).toBe('▓▓░░ Шаг 2/4');
  });

  test('custom total: step 1 of 3', () => {
    expect(bookingProgress(1, 3)).toBe('▓░░ Шаг 1/3');
  });
});

// ─── stepHeader() ─────────────────────────────────────────────────────────────

describe('stepHeader() — booking step header', () => {
  test('step 1 has correct emoji and title', () => {
    const h = stepHeader(1, 'Выберите модель');
    expect(h).toContain('📝 *Бронирование · Шаг 1/4*');
    expect(h).toContain('*Выберите модель*');
  });

  test('step 1 shows one filled dot and three empty', () => {
    const h = stepHeader(1, 'Test');
    expect(h).toContain('● ○ ○ ○');
  });

  test('step 2 shows two filled dots', () => {
    const h = stepHeader(2, 'Test');
    expect(h).toContain('● ● ○ ○');
  });

  test('step 3 shows three filled dots', () => {
    const h = stepHeader(3, 'Test');
    expect(h).toContain('● ● ● ○');
  });

  test('step 4 shows all filled dots', () => {
    const h = stepHeader(4, 'Подтверждение');
    expect(h).toContain('● ● ● ●');
  });

  test('ends with double newline for layout spacing', () => {
    const h = stepHeader(1, 'Шаг');
    expect(h.endsWith('\n\n')).toBe(true);
  });

  test('embeds title in bold MarkdownV2 markers', () => {
    const h = stepHeader(2, 'Детали мероприятия');
    expect(h).toContain('*Детали мероприятия*');
  });
});

// ─── formatDateShort() ────────────────────────────────────────────────────────

describe('formatDateShort() — calendar date display', () => {
  test('January 1st', () => {
    expect(formatDateShort('2025-01-01')).toBe('1 янв');
  });

  test('February 14th', () => {
    expect(formatDateShort('2025-02-14')).toBe('14 фев');
  });

  test('March 31st', () => {
    expect(formatDateShort('2025-03-31')).toBe('31 мар');
  });

  test('December 25th', () => {
    expect(formatDateShort('2025-12-25')).toBe('25 дек');
  });

  test('strips leading zero from day', () => {
    expect(formatDateShort('2025-06-05')).toBe('5 июн');
  });

  test('all 12 months use correct Ru abbreviations', () => {
    const expected = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
    expected.forEach((abbr, idx) => {
      const m = String(idx + 1).padStart(2, '0');
      expect(formatDateShort(`2025-${m}-15`)).toBe(`15 ${abbr}`);
    });
  });
});

// ─── groupBusyDatesIntoRanges() ───────────────────────────────────────────────

describe('groupBusyDatesIntoRanges() — calendar range grouping', () => {
  test('returns empty array for no rows', () => {
    expect(groupBusyDatesIntoRanges([])).toEqual([]);
  });

  test('single date returns one range with same start/end', () => {
    const result = groupBusyDatesIntoRanges([{ busy_date: '2025-06-10', reason: 'Съёмка' }]);
    expect(result).toEqual([{ start: '2025-06-10', end: '2025-06-10', reason: 'Съёмка' }]);
  });

  test('consecutive dates with same reason merge into one range', () => {
    const rows = [
      { busy_date: '2025-06-10', reason: 'Отпуск' },
      { busy_date: '2025-06-11', reason: 'Отпуск' },
      { busy_date: '2025-06-12', reason: 'Отпуск' },
    ];
    const result = groupBusyDatesIntoRanges(rows);
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ start: '2025-06-10', end: '2025-06-12', reason: 'Отпуск' });
  });

  test('gap in dates creates two separate ranges', () => {
    const rows = [
      { busy_date: '2025-06-10', reason: 'Съёмка' },
      { busy_date: '2025-06-12', reason: 'Съёмка' }, // gap: 11th missing
    ];
    const result = groupBusyDatesIntoRanges(rows);
    expect(result).toHaveLength(2);
    expect(result[0].end).toBe('2025-06-10');
    expect(result[1].start).toBe('2025-06-12');
  });

  test('consecutive dates with different reasons split into separate ranges', () => {
    const rows = [
      { busy_date: '2025-06-10', reason: 'Съёмка' },
      { busy_date: '2025-06-11', reason: 'Другое' },
    ];
    const result = groupBusyDatesIntoRanges(rows);
    expect(result).toHaveLength(2);
    expect(result[0].reason).toBe('Съёмка');
    expect(result[1].reason).toBe('Другое');
  });

  test('null/undefined reason treated as empty string', () => {
    const rows = [
      { busy_date: '2025-06-10', reason: null },
      { busy_date: '2025-06-11', reason: undefined },
    ];
    const result = groupBusyDatesIntoRanges(rows);
    expect(result).toHaveLength(1);
    expect(result[0].reason).toBe('');
  });

  test('multiple separate ranges returned in order', () => {
    const rows = [
      { busy_date: '2025-06-01', reason: 'A' },
      { busy_date: '2025-06-02', reason: 'A' },
      { busy_date: '2025-06-05', reason: 'B' },
      { busy_date: '2025-06-06', reason: 'B' },
      { busy_date: '2025-06-07', reason: 'B' },
    ];
    const result = groupBusyDatesIntoRanges(rows);
    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({ start: '2025-06-01', end: '2025-06-02', reason: 'A' });
    expect(result[1]).toEqual({ start: '2025-06-05', end: '2025-06-07', reason: 'B' });
  });
});
