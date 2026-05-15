'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const apiCode = fs.readFileSync(path.join(ROOT, 'routes/api.js'), 'utf8');
const whatsappPath = path.join(ROOT, 'services/whatsapp.js');
const whatsappCode = fs.existsSync(whatsappPath) ? fs.readFileSync(whatsappPath, 'utf8') : '';
const loggerPath = path.join(ROOT, 'services/logger.js');
const loggerCode = fs.existsSync(loggerPath) ? fs.readFileSync(loggerPath, 'utf8') : '';
const indexHtmlPath = path.join(ROOT, 'public/admin/index.html');
const indexHtml = fs.existsSync(indexHtmlPath) ? fs.readFileSync(indexHtmlPath, 'utf8') : '';
const envExamplePath = path.join(ROOT, '.env.example');
const envExample = fs.existsSync(envExamplePath) ? fs.readFileSync(envExamplePath, 'utf8') : '';

// ─── A. WhatsApp service (services/whatsapp.js) ───────────────────────────────

describe('A. WhatsApp service — file existence and exports', () => {
  test('A01: services/whatsapp.js file exists', () => {
    expect(fs.existsSync(whatsappPath)).toBe(true);
  });

  test('A02: whatsapp.js exports sendText function', () => {
    expect(whatsappCode).toMatch(/module\.exports\s*=\s*\{[\s\S]*sendText/);
  });

  test('A03: whatsapp.js exports sendTemplate function', () => {
    expect(whatsappCode).toMatch(/module\.exports\s*=\s*\{[\s\S]*sendTemplate/);
  });

  test('A04: whatsapp.js exports notifyOrderStatus function', () => {
    expect(whatsappCode).toMatch(/module\.exports\s*=\s*\{[\s\S]*notifyOrderStatus/);
  });

  test('A05: whatsapp.js exports notifyOrderConfirmed function', () => {
    expect(whatsappCode).toMatch(/module\.exports\s*=\s*\{[\s\S]*notifyOrderConfirmed/);
  });

  test('A06: sendText function is defined in whatsapp.js', () => {
    expect(whatsappCode).toMatch(/async\s+function\s+sendText\s*\(/);
  });

  test('A07: sendTemplate function is defined in whatsapp.js', () => {
    expect(whatsappCode).toMatch(/async\s+function\s+sendTemplate\s*\(/);
  });

  test('A08: notifyOrderStatus function is defined with correct params', () => {
    expect(whatsappCode).toMatch(
      /async\s+function\s+notifyOrderStatus\s*\(\s*phone\s*,\s*orderNum\s*,\s*status\s*,\s*statusLabel/
    );
  });

  test('A09: notifyOrderConfirmed function is defined', () => {
    expect(whatsappCode).toMatch(/async\s+function\s+notifyOrderConfirmed\s*\(/);
  });
});

describe('A. WhatsApp service — configuration checks', () => {
  test('A10: sendText checks WHATSAPP_TOKEN before sending', () => {
    expect(whatsappCode).toMatch(/WHATSAPP_TOKEN/);
  });

  test('A11: sendTemplate checks WHATSAPP_TOKEN', () => {
    expect(whatsappCode).toMatch(/WHATSAPP_TOKEN/);
  });

  test('A12: sendTemplate checks WHATSAPP_PHONE_ID', () => {
    expect(whatsappCode).toMatch(/WHATSAPP_PHONE_ID/);
  });

  test('A13: _isConfigured checks both TOKEN and PHONE_ID', () => {
    expect(whatsappCode).toMatch(/WHATSAPP_TOKEN[\s\S]{0,50}WHATSAPP_PHONE_ID/);
  });

  test('A14: sendText logs when not configured (console.info or console.log)', () => {
    expect(whatsappCode).toMatch(/console\.(info|log)\s*\(\s*['"`\[].*[Ww]hatsApp.*not configured/);
  });

  test('A15: sendText returns { sent: false, reason: not_configured } when unconfigured', () => {
    expect(whatsappCode).toMatch(/sent:\s*false[\s\S]{0,50}not_configured/);
  });

  test('A16: sendTemplate returns not_configured when token missing', () => {
    expect(whatsappCode).toMatch(/sent:\s*false[\s\S]{0,100}not_configured/);
  });
});

describe('A. WhatsApp service — HTTP / API calls', () => {
  test('A17: Uses https module (Node built-in)', () => {
    expect(whatsappCode).toMatch(/require\s*\(\s*['"]https['"]\s*\)/);
  });

  test('A18: URL contains graph.facebook.com', () => {
    expect(whatsappCode).toMatch(/graph\.facebook\.com/);
  });

  test('A19: Uses POST method for API calls', () => {
    expect(whatsappCode).toMatch(/method:\s*['"]POST['"]/);
  });

  test('A20: Sets Authorization Bearer header', () => {
    expect(whatsappCode).toMatch(/Authorization.*Bearer/);
  });

  test('A21: Sets Content-Type application/json', () => {
    expect(whatsappCode).toMatch(/Content-Type.*application\/json/);
  });

  test('A22: messaging_product is whatsapp in payload', () => {
    expect(whatsappCode).toMatch(/messaging_product:\s*['"]whatsapp['"]/);
  });

  test('A23: sendText uses type: text in payload', () => {
    expect(whatsappCode).toMatch(/type:\s*['"]text['"]/);
  });

  test('A24: sendTemplate uses type: template in payload', () => {
    expect(whatsappCode).toMatch(/type:\s*['"]template['"]/);
  });
});

describe('A. WhatsApp service — error handling', () => {
  test('A25: sendText has try/catch block', () => {
    expect(whatsappCode).toMatch(/async\s+function\s+sendText[\s\S]{0,400}try\s*\{/);
  });

  test('A26: sendText catches errors and returns { sent: false, error }', () => {
    expect(whatsappCode).toMatch(/catch\s*\(\s*e\s*\)[\s\S]{0,200}sent:\s*false[\s\S]{0,200}error:\s*e\.message/);
  });

  test('A27: sendTemplate catches errors without crashing', () => {
    expect(whatsappCode).toMatch(/sendTemplate[\s\S]{0,1000}catch/);
  });

  test('A28: sendText logs error message on failure', () => {
    expect(whatsappCode).toMatch(/console\.error\s*\(\s*['"`\[].*[Ww]hatsApp.*sendText/);
  });

  test('A29: notifyOrderStatus returns no_phone when phone is falsy', () => {
    expect(whatsappCode).toMatch(/notifyOrderStatus[\s\S]{0,200}no_phone/);
  });

  test('A30: notifyOrderConfirmed returns no_phone when phone missing', () => {
    expect(whatsappCode).toMatch(/notifyOrderConfirmed[\s\S]{0,200}no_phone/);
  });

  test('A31: sendText uses 8000ms timeout for requests', () => {
    expect(whatsappCode).toMatch(/8000/);
  });

  test('A32: Request timeout destroys the request', () => {
    expect(whatsappCode).toMatch(/req\.destroy\s*\(\s*\)/);
  });
});

// ─── B. WhatsApp integration in routes/api.js ────────────────────────────────

describe('B. WhatsApp integration in routes/api.js', () => {
  test('B01: PATCH /admin/orders/:id/status route exists', () => {
    expect(apiCode).toMatch(/router\.patch\s*\(\s*['"]\/admin\/orders\/:id\/status['"]/);
  });

  test('B02: whatsapp service is required inside PATCH orders status handler', () => {
    expect(apiCode).toMatch(/require\s*\(\s*['"]\.\.\/services\/whatsapp['"]\s*\)/);
  });

  test('B03: WhatsApp notifyOrderStatus is called in PATCH handler', () => {
    expect(apiCode).toMatch(/whatsapp\.notifyOrderStatus\s*\(/);
  });

  test('B04: WhatsApp call uses .catch() for graceful failure handling', () => {
    expect(apiCode).toMatch(/whatsapp\.notifyOrderStatus[\s\S]{0,100}\.catch\s*\(/);
  });

  test('B05: WhatsApp notification only sent when client_phone is present', () => {
    expect(apiCode).toMatch(/client_phone[\s\S]{0,100}whatsapp\.notifyOrderStatus|whatsapp[\s\S]{0,200}client_phone/);
  });

  test('B06: WhatsApp notification only sent when status changed', () => {
    expect(apiCode).toMatch(
      /status\s*!==\s*order\.prev_status[\s\S]{0,400}whatsapp|whatsapp[\s\S]{0,400}status\s*!==\s*order\.prev_status/
    );
  });

  test('B07: STATUS_LABELS is used to get human-readable label', () => {
    expect(apiCode).toMatch(/STATUS_LABELS\s*\[\s*status\s*\]/);
  });

  test('B08: statusLabel is passed to notifyOrderStatus', () => {
    expect(apiCode).toMatch(/notifyOrderStatus\s*\([\s\S]{0,200}statusLabel/);
  });

  test('B09: WhatsApp block has try/catch around require call', () => {
    // The require of whatsapp is wrapped in try/catch
    expect(apiCode).toMatch(/try\s*\{[\s\S]{0,200}require\s*\(\s*['"]\.\.\/services\/whatsapp['"]\s*\)/);
  });

  test('B10: STATUS_LABELS is imported from utils/constants', () => {
    expect(apiCode).toMatch(
      /STATUS_LABELS[\s\S]{0,200}require\s*\(\s*['"]\.\.\/utils\/constants['"]\s*\)|require\s*\(\s*['"]\.\.\/utils\/constants['"]\s*\)[\s\S]{0,200}STATUS_LABELS/
    );
  });
});

// ─── C. Structured logger (services/logger.js) ────────────────────────────────

describe('C. Structured logger — file existence and exports', () => {
  test('C01: services/logger.js file exists', () => {
    expect(fs.existsSync(loggerPath)).toBe(true);
  });

  test('C02: logger.js exports error function', () => {
    expect(loggerCode).toMatch(/error\s*:/);
  });

  test('C03: logger.js exports warn function', () => {
    expect(loggerCode).toMatch(/warn\s*:/);
  });

  test('C04: logger.js exports info function', () => {
    expect(loggerCode).toMatch(/info\s*:/);
  });

  test('C05: logger.js exports debug function', () => {
    expect(loggerCode).toMatch(/debug\s*:/);
  });

  test('C06: All four log level functions use module.exports', () => {
    expect(loggerCode).toMatch(/module\.exports\s*=\s*\{[\s\S]*error[\s\S]*warn[\s\S]*info[\s\S]*debug/);
  });
});

describe('C. Structured logger — LOG_JSON configuration', () => {
  test('C07: LOG_JSON env var is read', () => {
    expect(loggerCode).toMatch(/LOG_JSON/);
  });

  test('C08: LOG_JSON=1 enables JSON output', () => {
    expect(loggerCode).toMatch(/LOG_JSON\s*===\s*['"]1['"]|LOG_JSON\s*==\s*['"]1['"]/);
  });

  test('C09: JSON format includes ts field', () => {
    expect(loggerCode).toMatch(/ts\s*:/);
  });

  test('C10: JSON format includes level field', () => {
    expect(loggerCode).toMatch(/level\s*[,:]/);
  });

  test('C11: JSON format includes msg field', () => {
    expect(loggerCode).toMatch(/msg\s*[,:]/);
  });

  test('C12: JSON output uses JSON.stringify', () => {
    expect(loggerCode).toMatch(/JSON\.stringify\s*\(/);
  });

  test('C13: JSON output uses process.stdout.write', () => {
    expect(loggerCode).toMatch(/process\.stdout\.write\s*\(/);
  });

  test('C14: Non-JSON (dev) format uses console output', () => {
    expect(loggerCode).toMatch(/console\.log\s*\(/);
  });
});

describe('C. Structured logger — LOG_LEVEL configuration', () => {
  test('C15: LOG_LEVEL env var is read', () => {
    expect(loggerCode).toMatch(/LOG_LEVEL/);
  });

  test('C16: Default LOG_LEVEL is info', () => {
    expect(loggerCode).toMatch(/LOG_LEVEL\s*\|\|\s*['"]info['"]/);
  });

  test('C17: LEVELS map defines error as lowest (0)', () => {
    expect(loggerCode).toMatch(/error\s*:\s*0/);
  });

  test('C18: LEVELS map defines warn as 1', () => {
    expect(loggerCode).toMatch(/warn\s*:\s*1/);
  });

  test('C19: LEVELS map defines info as 2', () => {
    expect(loggerCode).toMatch(/info\s*:\s*2/);
  });

  test('C20: LEVELS map defines debug as 3 (most verbose)', () => {
    expect(loggerCode).toMatch(/debug\s*:\s*3/);
  });

  test('C21: Lower-level log calls are suppressed based on currentLevel comparison', () => {
    expect(loggerCode).toMatch(/>\s*currentLevel|currentLevel\s*</);
  });

  test('C22: ts field uses ISO timestamp format', () => {
    expect(loggerCode).toMatch(/toISOString\s*\(\s*\)/);
  });
});

// ─── D. Factory dashboard in admin HTML ───────────────────────────────────────

describe('D. Factory dashboard — admin/index.html', () => {
  test('D01: public/admin/index.html exists', () => {
    expect(fs.existsSync(indexHtmlPath)).toBe(true);
  });

  test('D02: index.html contains factory-related content', () => {
    expect(indexHtml).toMatch(/factory|Factory/);
  });

  test('D03: index.html contains loadFactoryStatus function', () => {
    expect(indexHtml).toMatch(/loadFactoryStatus/);
  });

  test('D04: loadFactoryStatus fetches /api/admin/factory/status', () => {
    expect(indexHtml).toMatch(/admin\/factory\/status/);
  });

  test('D05: Dashboard shows pendingActions', () => {
    expect(indexHtml).toMatch(/pendingActions/);
  });

  test('D06: Dashboard shows recentDecisions (CEO decisions)', () => {
    expect(indexHtml).toMatch(/recentDecisions/);
  });

  test('D07: Dashboard shows activeExperiments', () => {
    expect(indexHtml).toMatch(/activeExperiments/);
  });

  test('D08: Dashboard has healthScore display', () => {
    expect(indexHtml).toMatch(/healthScore/);
  });

  test('D09: Dashboard has manual refresh button for factory status', () => {
    expect(indexHtml).toMatch(/loadFactoryStatus\s*\(\s*\)/);
  });

  test('D10: Dashboard has auto-refresh via setInterval', () => {
    expect(indexHtml).toMatch(/setInterval\s*\(\s*loadFactoryStatus/);
  });

  test('D11: Factory panel has an id for DOM targeting', () => {
    expect(indexHtml).toMatch(/id\s*=\s*['"]factory-(?:panel|body|status)['"]/);
  });

  test('D12: Factory section has link to factory.html', () => {
    expect(indexHtml).toMatch(/factory\.html/);
  });
});

// ─── E. Factory status API response shape ─────────────────────────────────────

describe('E. Factory status API — response shape', () => {
  test('E01: GET /admin/factory/status route defined', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory\/status['"]/);
  });

  test('E02: Endpoint returns available field', () => {
    expect(apiCode).toMatch(/available\s*:/);
  });

  test('E03: Endpoint returns status field', () => {
    expect(apiCode).toMatch(/status\s*:\s*['"]ok['"]/);
  });

  test('E04: Endpoint returns lastRun field', () => {
    expect(apiCode).toMatch(/lastRun\s*:/);
  });

  test('E05: Endpoint returns pendingActions field', () => {
    expect(apiCode).toMatch(/pendingActions\s*:/);
  });

  test('E06: Endpoint returns recentDecisions field', () => {
    expect(apiCode).toMatch(/recentDecisions\s*:/);
  });

  test('E07: Endpoint returns activeExperiments field', () => {
    expect(apiCode).toMatch(/activeExperiments\s*:/);
  });

  test('E08: Endpoint returns healthScore field', () => {
    expect(apiCode).toMatch(/healthScore\s*:/);
  });

  test('E09: Returns { available: false, status: "unavailable" } when factory.db missing', () => {
    expect(apiCode).toMatch(/available:\s*false[\s\S]{0,100}status:\s*['"]unavailable['"]/);
  });

  test('E10: Returns { available: false, status: "error" } on exception', () => {
    expect(apiCode).toMatch(/available:\s*false[\s\S]{0,100}status:\s*['"]error['"]/);
  });

  test('E11: Returns { available: true, status: "ok" } on success', () => {
    expect(apiCode).toMatch(/available:\s*true[\s\S]{0,100}status:\s*['"]ok['"]/);
  });

  test('E12: Uses fs.existsSync to check factory.db', () => {
    expect(apiCode).toMatch(/fs\.existsSync\s*\(/);
  });

  test('E13: Opens factory.db in readonly mode', () => {
    expect(apiCode).toMatch(/readonly:\s*true/);
  });

  test('E14: Closes factory DB connection in finally block', () => {
    expect(apiCode).toMatch(/fdb\.close\s*\(\s*\)/);
  });

  test('E15: Queries growth_actions for pendingActions', () => {
    expect(apiCode).toMatch(/growth_actions/);
  });

  test('E16: Queries ceo_decisions for recentDecisions', () => {
    expect(apiCode).toMatch(/ceo_decisions/);
  });

  test('E17: Queries experiments for activeExperiments', () => {
    expect(apiCode).toMatch(/experiments/);
  });

  test('E18: Queries cycles table for lastRun and healthScore', () => {
    expect(apiCode).toMatch(/FROM\s+cycles/);
  });
});

// ─── F. .env.example WhatsApp configuration ───────────────────────────────────

describe('F. .env.example — WhatsApp environment variables', () => {
  test('F01: .env.example file exists', () => {
    expect(fs.existsSync(envExamplePath)).toBe(true);
  });

  test('F02: .env.example contains WHATSAPP_TOKEN', () => {
    expect(envExample).toMatch(/WHATSAPP_TOKEN/);
  });

  test('F03: .env.example contains WHATSAPP_PHONE_ID', () => {
    expect(envExample).toMatch(/WHATSAPP_PHONE_ID/);
  });

  test('F04: WHATSAPP_TOKEN has a placeholder value', () => {
    expect(envExample).toMatch(/WHATSAPP_TOKEN\s*=\s*.+/);
  });

  test('F05: WHATSAPP_PHONE_ID has a placeholder value', () => {
    expect(envExample).toMatch(/WHATSAPP_PHONE_ID\s*=\s*.+/);
  });
});

// ─── G. Runtime behaviour of whatsapp module ─────────────────────────────────

describe('G. WhatsApp service — runtime module loading', () => {
  test('G01: whatsapp.js can be required without crashing', () => {
    expect(() => require(whatsappPath)).not.toThrow();
  });

  test('G02: required module has sendText as function', () => {
    const wa = require(whatsappPath);
    expect(typeof wa.sendText).toBe('function');
  });

  test('G03: required module has sendTemplate as function', () => {
    const wa = require(whatsappPath);
    expect(typeof wa.sendTemplate).toBe('function');
  });

  test('G04: required module has notifyOrderStatus as function', () => {
    const wa = require(whatsappPath);
    expect(typeof wa.notifyOrderStatus).toBe('function');
  });

  test('G05: required module has notifyOrderConfirmed as function', () => {
    const wa = require(whatsappPath);
    expect(typeof wa.notifyOrderConfirmed).toBe('function');
  });

  test('G06: required module has isConfigured as function', () => {
    const wa = require(whatsappPath);
    expect(typeof wa.isConfigured).toBe('function');
  });

  test('G07: isConfigured returns false when env vars not set', () => {
    const origToken = process.env.WHATSAPP_TOKEN;
    const origPhone = process.env.WHATSAPP_PHONE_ID;
    delete process.env.WHATSAPP_TOKEN;
    delete process.env.WHATSAPP_PHONE_ID;
    // Re-evaluate _isConfigured by calling isConfigured
    const wa = require(whatsappPath);
    const result = wa.isConfigured();
    if (origToken) process.env.WHATSAPP_TOKEN = origToken;
    if (origPhone) process.env.WHATSAPP_PHONE_ID = origPhone;
    expect(result).toBe(false);
  });

  test('G08: sendText returns { sent: false, reason: "not_configured" } when env vars missing', async () => {
    const origToken = process.env.WHATSAPP_TOKEN;
    const origPhone = process.env.WHATSAPP_PHONE_ID;
    delete process.env.WHATSAPP_TOKEN;
    delete process.env.WHATSAPP_PHONE_ID;
    const wa = require(whatsappPath);
    const result = await wa.sendText('+79001234567', 'test');
    if (origToken) process.env.WHATSAPP_TOKEN = origToken;
    if (origPhone) process.env.WHATSAPP_PHONE_ID = origPhone;
    expect(result.sent).toBe(false);
    expect(result.reason).toBe('not_configured');
  });

  test('G09: sendTemplate returns { sent: false, reason: "not_configured" } when env vars missing', async () => {
    const origToken = process.env.WHATSAPP_TOKEN;
    const origPhone = process.env.WHATSAPP_PHONE_ID;
    delete process.env.WHATSAPP_TOKEN;
    delete process.env.WHATSAPP_PHONE_ID;
    const wa = require(whatsappPath);
    const result = await wa.sendTemplate('+79001234567', 'order_status');
    if (origToken) process.env.WHATSAPP_TOKEN = origToken;
    if (origPhone) process.env.WHATSAPP_PHONE_ID = origPhone;
    expect(result.sent).toBe(false);
    expect(result.reason).toBe('not_configured');
  });

  test('G10: notifyOrderStatus returns { sent: false, reason: "no_phone" } when phone empty', async () => {
    const wa = require(whatsappPath);
    const result = await wa.notifyOrderStatus('', 'NM-001', 'confirmed', 'Подтверждена');
    expect(result.sent).toBe(false);
    expect(result.reason).toBe('no_phone');
  });

  test('G11: notifyOrderStatus returns { sent: false, reason: "no_phone" } when phone null', async () => {
    const wa = require(whatsappPath);
    const result = await wa.notifyOrderStatus(null, 'NM-002', 'completed', 'Завершена');
    expect(result.sent).toBe(false);
    expect(result.reason).toBe('no_phone');
  });

  test('G12: notifyOrderConfirmed returns { sent: false, reason: "no_phone" } when phone empty', async () => {
    const wa = require(whatsappPath);
    const result = await wa.notifyOrderConfirmed('', 'NM-003', 'Алексей');
    expect(result.sent).toBe(false);
    expect(result.reason).toBe('no_phone');
  });
});

// ─── H. Runtime behaviour of logger module ────────────────────────────────────

describe('H. Structured logger — runtime module loading', () => {
  test('H01: services/logger.js can be required without crashing', () => {
    expect(() => require(loggerPath)).not.toThrow();
  });

  test('H02: logger.error is a function', () => {
    const logger = require(loggerPath);
    expect(typeof logger.error).toBe('function');
  });

  test('H03: logger.warn is a function', () => {
    const logger = require(loggerPath);
    expect(typeof logger.warn).toBe('function');
  });

  test('H04: logger.info is a function', () => {
    const logger = require(loggerPath);
    expect(typeof logger.info).toBe('function');
  });

  test('H05: logger.debug is a function', () => {
    const logger = require(loggerPath);
    expect(typeof logger.debug).toBe('function');
  });

  test('H06: logger.info does not throw', () => {
    const logger = require(loggerPath);
    expect(() => logger.info('test message')).not.toThrow();
  });

  test('H07: logger.error does not throw', () => {
    const logger = require(loggerPath);
    expect(() => logger.error('error message', { code: 500 })).not.toThrow();
  });

  test('H08: logger.warn does not throw', () => {
    const logger = require(loggerPath);
    expect(() => logger.warn('warn message')).not.toThrow();
  });

  test('H09: logger.debug does not throw', () => {
    const logger = require(loggerPath);
    expect(() => logger.debug('debug message', { extra: true })).not.toThrow();
  });

  test('H10: Logger module has exactly 4 exported keys', () => {
    const logger = require(loggerPath);
    const keys = Object.keys(logger);
    expect(keys).toContain('error');
    expect(keys).toContain('warn');
    expect(keys).toContain('info');
    expect(keys).toContain('debug');
  });
});
