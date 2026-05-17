'use strict';
/**
 * Wave103 tests: strings.js i18n API, analytics trackEvents, DB indexes, orchestrator completeness
 *  1. strings.js multilingual API (БЛОК 8.3)
 *  2. Analytics tracking events in booking.js and catalog.js (БЛОК 9.3)
 *  3. Database composite indexes (idx_models_archived_available, idx_orders_client_chat_status)
 *  4. Orchestrator completeness (PricingNegotiator, VisualConceptor, all departments)
 */

const fs = require('fs');
const path = require('path');

// ─── 1. strings.js multilingual API ─────────────────────────────────────────

const { t, I18N, DEFAULT_LANG } = require('../strings');

describe('strings.js multilingual API', () => {
  test('DEFAULT_LANG is ru', () => expect(DEFAULT_LANG).toBe('ru'));

  test('t() returns string for known key', () => expect(t('booking_start')).toBe(I18N.ru.booking_start));

  test('t() falls back to ru for unknown lang', () => expect(t('booking_start', 'jp')).toBe(I18N.ru.booking_start));

  test('t() returns key for unknown key', () => expect(t('nonexistent_key_xyz')).toBe('nonexistent_key_xyz'));

  test('t() does placeholder substitution', () => {
    const result = t('booking_success_pending', 'ru', { number: '42' });
    expect(result).toContain('42');
  });

  test('I18N has ru and en', () => {
    expect(I18N).toHaveProperty('ru');
    expect(I18N).toHaveProperty('en');
  });

  test('ru has >= 100 keys', () => expect(Object.keys(I18N.ru).length).toBeGreaterThanOrEqual(100));

  test('en has same keys as ru', () => {
    const ruKeys = Object.keys(I18N.ru).sort();
    const enKeys = Object.keys(I18N.en).sort();
    expect(enKeys).toEqual(ruKeys);
  });
});

// ─── 2. Analytics tracking events in booking.js and catalog.js ──────────────

describe('Analytics tracking events in frontend JS', () => {
  test('catalog.js contains catalog_view trackEvent call', () => {
    const src = fs.readFileSync(path.join(__dirname, '../public/js/catalog.js'), 'utf8');
    expect(src).toContain("trackEvent('catalog_view'");
  });

  test('booking.js contains booking_start trackEvent', () => {
    const src = fs.readFileSync(path.join(__dirname, '../public/js/booking.js'), 'utf8');
    expect(src).toContain("trackEvent('booking_start'");
  });

  test('booking.js contains booking_complete trackEvent', () => {
    const src = fs.readFileSync(path.join(__dirname, '../public/js/booking.js'), 'utf8');
    expect(src).toContain("trackEvent('booking_complete'");
  });

  test('analytics calls are guarded with typeof check', () => {
    const src = fs.readFileSync(path.join(__dirname, '../public/js/catalog.js'), 'utf8');
    expect(src).toContain("typeof trackEvent === 'function'");
  });
});

// ─── 3. Database composite indexes ──────────────────────────────────────────

describe('Database composite indexes', () => {
  test('database.js contains idx_models_archived_available', () => {
    const src = fs.readFileSync(path.join(__dirname, '../database.js'), 'utf8');
    expect(src).toContain('idx_models_archived_available');
  });

  test('database.js contains idx_orders_client_chat_status', () => {
    const src = fs.readFileSync(path.join(__dirname, '../database.js'), 'utf8');
    expect(src).toContain('idx_orders_client_chat_status');
  });
});

// ─── 4. Orchestrator completeness ────────────────────────────────────────────

describe('Orchestrator completeness', () => {
  test('orchestrator requires PricingNegotiator', () => {
    const src = fs.readFileSync(path.join(__dirname, '../agents/orchestrator.js'), 'utf8');
    expect(src).toContain('PricingNegotiator');
  });

  test('orchestrator requires VisualConceptor', () => {
    const src = fs.readFileSync(path.join(__dirname, '../agents/orchestrator.js'), 'utf8');
    expect(src).toContain('VisualConceptor');
  });

  test('orchestrator requires all departments', () => {
    const src = fs.readFileSync(path.join(__dirname, '../agents/orchestrator.js'), 'utf8');
    expect(src).toContain("require('./departments/sales')");
    expect(src).toContain("require('./departments/creative')");
    expect(src).toContain("require('./departments/finance')");
    expect(src).toContain("require('./departments/research')");
    expect(src).toContain("require('./departments/customer-success')");
  });
});
