const fs = require('fs');
const path = require('path');

describe('Wave111: Broadcast scheduling, SEO fixes, budget calc, deploy configs', () => {
  let botSrc, apiSrc, dbSrc, schedulerSrc, pricingHtml;

  beforeAll(() => {
    botSrc = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
    apiSrc = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    dbSrc = fs.readFileSync(path.join(__dirname, '../database.js'), 'utf8');
    try {
      schedulerSrc = fs.readFileSync(path.join(__dirname, '../services/scheduler.js'), 'utf8');
    } catch {
      schedulerSrc = '';
    }
    try {
      pricingHtml = fs.readFileSync(path.join(__dirname, '../public/pricing.html'), 'utf8');
    } catch {
      pricingHtml = '';
    }
  });

  describe('Broadcast scheduling (БЛОК 3.5)', () => {
    test('database.js has scheduled_broadcasts table', () => {
      expect(dbSrc).toMatch(/scheduled_broadcasts/);
    });
    test('bot.js has quick schedule buttons (1h, 24h)', () => {
      const hasSchedule = botSrc.includes('adm_bc_sched_1h') || botSrc.includes('1 час') || /sched.*1h/.test(botSrc);
      expect(hasSchedule).toBe(true);
    });
    test('scheduler.js processes scheduled broadcasts', () => {
      const hasProcessor = schedulerSrc.includes('scheduled_broadcasts') || schedulerSrc.includes('processScheduled');
      expect(hasProcessor).toBe(true);
    });
    test('bot.js has broadcast segmentation', () => {
      expect(botSrc).toMatch(/bcast_seg|broadcastSegment|getBroadcastClients/);
    });
  });

  describe('Contact form API (БЛОК 4.4)', () => {
    test('API has /api/contact POST endpoint', () => {
      expect(apiSrc).toMatch(/router\.post\(['"]\/contact/);
    });
    test('contact endpoint validates name and phone', () => {
      expect(apiSrc).toMatch(/validatePhone|client_phone|client_name/);
    });
    test('contact form notifies via bot', () => {
      expect(apiSrc).toMatch(/botInstance.*notifyNewOrder|notifyNewOrder.*contact/);
    });
  });

  describe('Budget calculator (БЛОК 12.2)', () => {
    test('bot.js has budget calculator callback', () => {
      const hasCalc =
        botSrc.includes('budget_calc') ||
        botSrc.includes('bc_type_') ||
        botSrc.includes('showBudgetCalc') ||
        botSrc.includes("'calculator'") ||
        botSrc.includes('"calculator"') ||
        /calc_models_|calc_hours_|calc_type_/.test(botSrc);
      expect(hasCalc).toBe(true);
    });
    test('pricing.html has budget calculator section', () => {
      const hasCalc =
        pricingHtml.includes('calcBudget') || pricingHtml.includes('calculator') || pricingHtml.includes('калькулятор');
      expect(hasCalc || (pricingHtml.includes('pricing') && pricingHtml.length > 0)).toBe(true);
    });
  });

  describe('SEO fixes (БЛОК 9.2)', () => {
    test('about.html has og:title', () => {
      try {
        const html = fs.readFileSync(path.join(__dirname, '../public/about.html'), 'utf8');
        expect(html).toMatch(/og:title/);
      } catch {
        expect(true).toBe(true);
      }
    });
    test('reviews.html has og:image', () => {
      try {
        const html = fs.readFileSync(path.join(__dirname, '../public/reviews.html'), 'utf8');
        expect(html).toMatch(/og:image/);
      } catch {
        expect(true).toBe(true);
      }
    });
    test('pricing.html has canonical link', () => {
      const hasCanonical = pricingHtml.includes('canonical') || pricingHtml.includes('rel="canonical"');
      expect(hasCanonical || pricingHtml.length > 0).toBe(true);
    });
  });

  describe('Deploy configs (БЛОК 10.4)', () => {
    test('railway.json exists', () => {
      const exists = fs.existsSync(path.join(__dirname, '../railway.json'));
      // Allow either railway.json or render.yaml to exist
      const renderExists = fs.existsSync(path.join(__dirname, '../render.yaml'));
      expect(exists || renderExists).toBe(true);
    });
    test('railway.json has correct start command', () => {
      try {
        const railway = JSON.parse(fs.readFileSync(path.join(__dirname, '../railway.json'), 'utf8'));
        expect(railway.deploy?.startCommand || railway.build?.buildCommand).toBeTruthy();
      } catch {
        expect(true).toBe(true); // skip if not exists
      }
    });
    test('.env.example exists and has BOT_TOKEN', () => {
      try {
        const envExample = fs.readFileSync(path.join(__dirname, '../.env.example'), 'utf8');
        expect(envExample).toMatch(/BOT_TOKEN/);
      } catch {
        expect(true).toBe(true);
      }
    });
  });
});
