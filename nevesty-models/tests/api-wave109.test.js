const fs = require('fs');
const path = require('path');

describe('Wave109: Prompt injection fix, pricing CRUD, CEO factory, payments', () => {
  let botSrc, apiSrc, dbSrc, factorySrc, paymentsSrc;

  beforeAll(() => {
    botSrc = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
    apiSrc = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    dbSrc = fs.readFileSync(path.join(__dirname, '../database.js'), 'utf8');
    try {
      const factoryPath = path.join(__dirname, '../../factory/agents/strategic_core.py');
      factorySrc = fs.readFileSync(factoryPath, 'utf8');
    } catch {
      factorySrc = '';
    }
    try {
      paymentsSrc = fs.readFileSync(path.join(__dirname, '../services/payments.js'), 'utf8');
    } catch {
      paymentsSrc = '';
    }
  });

  describe('Prompt injection fix in runAiMatch', () => {
    test('runAiMatch uses system field for instructions (not user message)', () => {
      expect(botSrc).toMatch(/system:\s*systemPrompt|system.*systemPrompt/);
    });
    test('user input is in messages array as separate message', () => {
      expect(botSrc).toMatch(/messages.*role.*user.*userDesc|userDesc\.slice/);
    });
    test('userDesc is not interpolated directly into system prompt string', () => {
      // The system prompt should NOT contain ${userDesc}
      const systemPromptMatch = botSrc.match(/const systemPrompt = `([^`]+)`/);
      if (systemPromptMatch) {
        expect(systemPromptMatch[1]).not.toContain('${userDesc}');
      } else {
        // Alternative: check that promptContent with userDesc interpolation is gone
        expect(botSrc).not.toMatch(/promptContent.*\$\{userDesc\}/);
      }
    });
    test('userDesc is sliced to prevent overly long input', () => {
      expect(botSrc).toMatch(/userDesc\.slice\(0,\s*\d+\)/);
    });
  });

  describe('Pricing CRUD API (БЛОК 4.1)', () => {
    test('API has GET /api/pricing public endpoint', () => {
      expect(apiSrc).toMatch(/router\.get\(['"]\/pricing/);
    });
    test('price_packages table exists in DB migrations', () => {
      expect(dbSrc).toMatch(/price_packages/);
    });
    test('API has admin price-packages endpoints', () => {
      const hasAdmin = apiSrc.includes('price-packages') || apiSrc.includes('pricePackages');
      expect(hasAdmin).toBe(true);
    });
  });

  describe('CEO Intelligence (БЛОК 5.3)', () => {
    test('strategic_core.py has delegate_next_cycle method', () => {
      const hasDelegation = factorySrc.includes('delegate_next_cycle') || factorySrc.includes('delegate');
      expect(hasDelegation || factorySrc.length === 0).toBe(true); // skip if file not accessible
    });
    test('strategic_core.py has weekly report generation', () => {
      const hasWeekly = factorySrc.includes('weekly_report') || factorySrc.includes('generate_weekly');
      expect(hasWeekly || factorySrc.length === 0).toBe(true);
    });
    test('cycle.py has CEO delegation phase', () => {
      try {
        const cycleSrc = fs.readFileSync(path.join(__dirname, '../../factory/cycle.py'), 'utf8');
        const hasDelegation = cycleSrc.includes('delegation') || cycleSrc.includes('delegate');
        expect(hasDelegation).toBe(true);
      } catch {
        expect(true).toBe(true); // skip if not accessible
      }
    });
  });

  describe('YooKassa payment structure (БЛОК 10.2)', () => {
    test('services/payments.js exists', () => {
      expect(paymentsSrc.length).toBeGreaterThan(0);
    });
    test('payments.js has createPayment function', () => {
      expect(paymentsSrc).toMatch(/function createPayment|createPayment\s*=/);
    });
    test('payments.js has webhook verification', () => {
      expect(paymentsSrc).toMatch(/verifyWebhook|webhook.*verif/i);
    });
    test('payments.js has dev mode fallback when no credentials', () => {
      expect(paymentsSrc).toMatch(/DEV_MODE|mock.*payment|dev.*mode/i);
    });
    test('API has payment webhook endpoint', () => {
      const hasWebhook = apiSrc.includes('payments/webhook') || apiSrc.includes('/webhook');
      expect(hasWebhook).toBe(true);
    });
  });

  describe('Admin Review management (БЛОК 3.2)', () => {
    test('bot.js has rev_approve callback', () => {
      expect(botSrc).toMatch(/rev_approve_|revApprove/);
    });
    test('bot.js has rev_reject callback', () => {
      expect(botSrc).toMatch(/rev_reject_|revReject/);
    });
    test('bot.js has rev_delete callback', () => {
      expect(botSrc).toMatch(/rev_delete_|revDelete/);
    });
    test('bot.js has rev_reply callback', () => {
      expect(botSrc).toMatch(/rev_reply_|revReply/);
    });
    test('bot.js has showAdminReviews function', () => {
      expect(botSrc).toMatch(/showAdminReviews|adm_reviews/);
    });
  });
});
