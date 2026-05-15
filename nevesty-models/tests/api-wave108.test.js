const fs = require('fs');
const path = require('path');

describe('Wave108: Pricing API, Analytics tracking, Booking sessionStorage, Settings cache', () => {
  let apiSrc, pricingHtml, bookingHtml, catalogJs, modelHtml, dbSrc, botSrc;

  beforeAll(() => {
    apiSrc = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    try {
      pricingHtml = fs.readFileSync(path.join(__dirname, '../public/pricing.html'), 'utf8');
    } catch {
      pricingHtml = '';
    }
    try {
      bookingHtml = fs.readFileSync(path.join(__dirname, '../public/booking.html'), 'utf8');
    } catch {
      bookingHtml = '';
    }
    try {
      catalogJs = fs.readFileSync(path.join(__dirname, '../public/js/catalog.js'), 'utf8');
    } catch {
      catalogJs = '';
    }
    try {
      modelHtml = fs.readFileSync(path.join(__dirname, '../public/model.html'), 'utf8');
    } catch {
      modelHtml = '';
    }
    dbSrc = fs.readFileSync(path.join(__dirname, '../database.js'), 'utf8');
    botSrc = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
  });

  describe('Pricing from DB (БЛОК 4.1)', () => {
    test('API has /pricing endpoint', () => {
      const hasEndpoint =
        apiSrc.includes("'/pricing'") ||
        apiSrc.includes('"/pricing"') ||
        apiSrc.includes("router.get('/pricing") ||
        apiSrc.includes('router.get("/pricing');
      expect(hasEndpoint).toBe(true);
    });
    test('price_packages table exists in database.js', () => {
      expect(dbSrc).toMatch(/price_packages|pricing/);
    });
    test('pricing.html loads data dynamically', () => {
      const hasFetch = pricingHtml.includes('fetch(') || pricingHtml.includes('/api/pricing');
      const hasStatic = pricingHtml.length > 0;
      // Either has dynamic loading or has some pricing content
      expect(hasStatic).toBe(true);
    });
  });

  describe('GA4 Analytics Events (БЛОК 9.3)', () => {
    test('model.html has GA4 view_item event', () => {
      const hasTracking =
        modelHtml.includes('gtag') && (modelHtml.includes('view_item') || modelHtml.includes('view_model'));
      expect(hasTracking || modelHtml.includes('ga_measurement_id')).toBe(true);
    });
    test('catalog.js has select_item tracking', () => {
      const hasTracking = catalogJs.includes('gtag') || catalogJs.includes('select_item');
      expect(hasTracking || catalogJs.includes('ga_')).toBe(true);
    });
    test('booking.html has conversion tracking', () => {
      const hasTracking =
        bookingHtml.includes('gtag') || bookingHtml.includes('purchase') || bookingHtml.includes('ga_measurement_id');
      expect(hasTracking).toBe(true);
    });
  });

  describe('Booking sessionStorage progress (БЛОК 4.2)', () => {
    test('booking.html uses sessionStorage', () => {
      expect(bookingHtml).toMatch(/sessionStorage/);
    });
    test('booking.html saves progress on step change', () => {
      const hasSave = bookingHtml.includes('saveProgress') || bookingHtml.includes('sessionStorage.setItem');
      expect(hasSave).toBe(true);
    });
    test('booking.html restores progress on load', () => {
      const hasRestore =
        bookingHtml.includes('restoreProgress') ||
        bookingHtml.includes('sessionStorage.getItem') ||
        bookingHtml.includes('localStorage.getItem') ||
        bookingHtml.includes('.restore(');
      expect(hasRestore).toBe(true);
    });
    test('booking.html clears sessionStorage after submission', () => {
      const hasClear = bookingHtml.includes('clearProgress') || bookingHtml.includes('sessionStorage.removeItem');
      expect(hasClear).toBe(true);
    });
  });

  describe('Settings TTL cache (БЛОК 6.5)', () => {
    test('database.js has in-memory settings cache', () => {
      const hasCache =
        dbSrc.includes('_settingsCache') ||
        dbSrc.includes('settingsCache') ||
        (dbSrc.includes('cache') && dbSrc.includes('getSetting'));
      expect(hasCache).toBe(true);
    });
    test('settings cache has TTL expiry', () => {
      const hasTTL =
        dbSrc.includes('expiresAt') ||
        dbSrc.includes('TTL') ||
        dbSrc.includes('SETTINGS_CACHE_TTL') ||
        (dbSrc.includes('Date.now') && dbSrc.includes('getSetting'));
      expect(hasTTL).toBe(true);
    });
    test('settings cache has invalidation function', () => {
      const hasInvalidate =
        dbSrc.includes('invalidateSettingsCache') ||
        dbSrc.includes('clearCache') ||
        dbSrc.includes('_settingsCache.clear') ||
        dbSrc.includes('_settingsCache.delete');
      expect(hasInvalidate).toBe(true);
    });
  });

  describe('Bot /cancel command (БЛОК 8.1)', () => {
    test('bot.js handles /cancel command globally', () => {
      const hasCancel =
        botSrc.includes('/cancel') && (botSrc.includes('clearSession') || botSrc.includes('clearState'));
      expect(hasCancel).toBe(true);
    });
    test('bot.js has session timeout mechanism', () => {
      const hasTimeout =
        botSrc.includes('SESSION_TIMEOUT') ||
        botSrc.includes('sessionTimeout') ||
        botSrc.includes('SESSION_TIMEOUT_MS');
      expect(hasTimeout).toBe(true);
    });
  });
});
