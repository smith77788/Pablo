'use strict';
const path = require('path');
const fs = require('fs');

describe('Wave 38: ExperimentTracker', () => {
  test('ExperimentTracker class exists in ceo.js', () => {
    const src = fs.readFileSync(path.join(__dirname, '../agents/departments/ceo.js'), 'utf8');
    expect(src).toContain('class ExperimentTracker');
  });
  test('loadActiveExperiment reads from ceo_last_experiment', () => {
    const src = fs.readFileSync(path.join(__dirname, '../agents/departments/ceo.js'), 'utf8');
    expect(src).toContain("'ceo_last_experiment'");
  });
  test('archiveExperiment saves to ceo_experiment_history', () => {
    const src = fs.readFileSync(path.join(__dirname, '../agents/departments/ceo.js'), 'utf8');
    expect(src).toContain("'ceo_experiment_history'");
  });
  test('CEO writes experiments to ab_experiments table', () => {
    const src = fs.readFileSync(path.join(__dirname, '../agents/departments/ceo.js'), 'utf8');
    expect(src).toContain('ab_experiments');
  });
  test('ExperimentTracker integrated as step in StrategicCEO.analyze', () => {
    const src = fs.readFileSync(path.join(__dirname, '../agents/departments/ceo.js'), 'utf8');
    expect(src).toContain('new ExperimentTracker()');
  });
});

describe('Wave 38: CB_DATA expansion', () => {
  const CB_DATA = require('../keyboards/constants').CB_DATA;
  test('CB_DATA exists', () => expect(CB_DATA).toBeTruthy());
  test('CB_DATA has AI_MATCH', () => expect(CB_DATA.AI_MATCH).toBe('ai_match'));
  test('CB_DATA has ADM_ANALYTICS', () => expect(CB_DATA.ADM_ANALYTICS).toBeDefined());
  test('CB_DATA has ADM_ORDERS_TODAY', () => expect(CB_DATA.ADM_ORDERS_TODAY).toBeDefined());
  test('CB_DATA has ADM_BULK_NEW_TO_REVIEW', () => expect(CB_DATA.ADM_BULK_NEW_TO_REVIEW).toBeDefined());
  test('CB_DATA has at least 23 keys', () => expect(Object.keys(CB_DATA).length).toBeGreaterThanOrEqual(23));
});

describe('Wave 38: Language toggle in bot settings', () => {
  test('bot.js has adm_toggle_bot_lang button', () => {
    const src = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
    expect(src).toContain("'adm_toggle_bot_lang'");
  });
  test('bot.js handles adm_toggle_bot_lang callback', () => {
    const src = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
    expect(src).toContain("featureKey === 'bot_lang'");
  });
  test('bot.js toggles bot_language setting between ru and en', () => {
    const src = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
    expect(src).toContain("'bot_language'");
    expect(src).toContain("newLang = current === 'en' ? 'ru' : 'en'");
  });
});

describe('Wave 37 security: XSS toast fix', () => {
  test('admin.js toast uses textContent not innerHTML', () => {
    const src = fs.readFileSync(path.join(__dirname, '../public/js/admin.js'), 'utf8');
    // Should not have the vulnerable innerHTML toast pattern
    expect(src).not.toContain('t.innerHTML = `<span style="font-weight:700"');
  });
  test('admin.js uses createElement for toast DOM', () => {
    const src = fs.readFileSync(path.join(__dirname, '../public/js/admin.js'), 'utf8');
    expect(src).toContain('document.createElement');
    expect(src).toContain('.textContent =');
  });
});

describe('Wave 37 security: avatar upload extension check', () => {
  test('routes/api.js avatar upload checks ALLOWED_IMG_EXTS', () => {
    const src = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    // The avatar fileFilter should check ALLOWED_IMG_EXTS
    const avatarSection = src.slice(src.indexOf('uploadAvatar'), src.indexOf('uploadAvatar') + 500);
    expect(avatarSection).toContain('ALLOWED_IMG_EXTS');
  });
});
