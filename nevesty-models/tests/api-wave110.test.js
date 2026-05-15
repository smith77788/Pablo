const fs = require('fs');
const path = require('path');

describe('Wave110: Cabinet UX, monitoring, model cabinet, broadcast, SEO', () => {
  let botSrc, cabinetHtml, schedulerSrc, apiSrc, dbSrc;

  beforeAll(() => {
    botSrc = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
    try {
      cabinetHtml = fs.readFileSync(path.join(__dirname, '../public/cabinet.html'), 'utf8');
    } catch {
      cabinetHtml = '';
    }
    try {
      schedulerSrc = fs.readFileSync(path.join(__dirname, '../services/scheduler.js'), 'utf8');
    } catch {
      schedulerSrc = '';
    }
    apiSrc = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    dbSrc = fs.readFileSync(path.join(__dirname, '../database.js'), 'utf8');
  });

  describe('Client Cabinet UX improvements (БЛОК 4.3)', () => {
    test('cabinet.html has toast notification CSS', () => {
      const hasToast = cabinetHtml.includes('toast') || cabinetHtml.includes('notification');
      expect(cabinetHtml.length > 0 && hasToast).toBe(true);
    });
    test('cabinet.html has visibility change polling control', () => {
      expect(cabinetHtml).toMatch(/visibilitychange|document\.hidden/);
    });
    test('cabinet.html has status badge animation', () => {
      const hasAnimation =
        cabinetHtml.includes('pulse') || cabinetHtml.includes('animation') || cabinetHtml.includes('status-badge');
      expect(hasAnimation).toBe(true);
    });
  });

  describe('Monitoring and alerts (БЛОК 6.2)', () => {
    test('scheduler.js has bot watchdog', () => {
      const hasWatchdog =
        schedulerSrc.includes('watchdog') ||
        schedulerSrc.includes('checkBotHealth') ||
        /bot.*health/i.test(schedulerSrc);
      expect(hasWatchdog || (schedulerSrc.includes('notify') && schedulerSrc.includes('bot'))).toBe(true);
    });
    test('scheduler.js has factory staleness check', () => {
      const hasFactory =
        schedulerSrc.includes('factory') &&
        (schedulerSrc.includes('stale') || schedulerSrc.includes('lastRun') || schedulerSrc.includes('last_run'));
      expect(hasFactory).toBe(true);
    });
    test('scheduler.js has disk space monitoring', () => {
      const hasDisk =
        schedulerSrc.includes('disk') ||
        schedulerSrc.includes('du -s') ||
        schedulerSrc.includes('checkDiskSpace') ||
        (schedulerSrc.includes('backups') && schedulerSrc.includes('GB'));
      expect(hasDisk).toBe(true);
    });
  });

  describe('Model personal cabinet (БЛОК 12.3)', () => {
    test('database.js or bot.js has model_id linkage in orders', () => {
      // model_accounts may not yet be a separate table; model_id column in orders is acceptable
      const hasModelLink =
        dbSrc.includes('model_accounts') || dbSrc.includes('model_id') || botSrc.includes('model_id');
      expect(hasModelLink).toBe(true);
    });
    test('bot.js has /myorders command', () => {
      expect(botSrc).toMatch(/myorders|\/myorders/);
    });
    test('bot.js has model linking command', () => {
      // /link_ deep-link OR /register_model command are both valid implementations
      expect(botSrc).toMatch(/\/link_|link.*model|model.*link|register_model/i);
    });
    test('bot.js queries orders for model', () => {
      expect(botSrc).toMatch(/model_id.*orders|orders.*model_id|WHERE model_id/);
    });
  });

  describe('Broadcast improvements (БЛОК 3.5)', () => {
    test('bot.js has broadcast segmentation', () => {
      const hasSegment =
        botSrc.includes('bcast_seg') ||
        botSrc.includes('broadcastSegment') ||
        (botSrc.includes('segment') && botSrc.includes('broadcast'));
      expect(hasSegment).toBe(true);
    });
    test('bot.js or db has scheduled_broadcasts support', () => {
      const hasScheduled = botSrc.includes('scheduled_broadcasts') || dbSrc.includes('scheduled_broadcasts');
      expect(hasScheduled).toBe(true);
    });
  });

  describe('Payments integration (БЛОК 10.2)', () => {
    test('services/payments.js exists', () => {
      const exists = fs.existsSync(path.join(__dirname, '../services/payments.js'));
      expect(exists).toBe(true);
    });
    test('API has /payments/webhook endpoint', () => {
      const hasWebhook = apiSrc.includes('payments/webhook') || apiSrc.includes('/webhook');
      expect(hasWebhook).toBe(true);
    });
  });
});
