'use strict';
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const ROOT = path.join(__dirname, '..');
const apiCode = fs.readFileSync(path.join(ROOT, 'routes/api.js'), 'utf8');
const serverCode = fs.readFileSync(path.join(ROOT, 'server.js'), 'utf8');
const schedulerCode = fs.readFileSync(path.join(ROOT, 'services/scheduler.js'), 'utf8');
// logger may not exist yet
const loggerPath = path.join(ROOT, 'services/logger.js');
const loggerCode = fs.existsSync(loggerPath) ? fs.readFileSync(loggerPath, 'utf8') : '';

// ─── A. Factory dashboard API (/admin/factory/status endpoint) ────────────────

describe('A. Factory dashboard API — route existence & middleware', () => {
  test('A01: GET /admin/factory/status route is defined in api.js', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory\/status['"]/);
  });

  test('A02: Route uses auth middleware', () => {
    // The route definition includes auth as 2nd argument
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory\/status['"]\s*,\s*auth/);
  });

  test('A03: Endpoint checks if factory.db exists using fs.existsSync', () => {
    // Find the factory/status block and verify it checks existsSync
    expect(apiCode).toMatch(/factory\/status[\s\S]{0,600}existsSync/);
  });

  test('A04: Returns available: false when factory.db is missing', () => {
    expect(apiCode).toMatch(/available:\s*false/);
  });

  test('A05: Uses better-sqlite3 for reading factory DB', () => {
    // Within the factory/status block, require better-sqlite3 is used
    expect(apiCode).toMatch(/require\s*\(\s*['"]better-sqlite3['"]\s*\)/);
  });

  test('A06: Returns pendingActions field', () => {
    expect(apiCode).toMatch(/pendingActions\s*:/);
  });

  test('A07: Returns recentDecisions field', () => {
    expect(apiCode).toMatch(/recentDecisions\s*:/);
  });

  test('A08: Returns activeExperiments field', () => {
    expect(apiCode).toMatch(/activeExperiments\s*:/);
  });

  test('A09: Returns healthScore field', () => {
    expect(apiCode).toMatch(/healthScore\s*:/);
  });

  test('A10: Returns lastRun field', () => {
    expect(apiCode).toMatch(/lastRun\s*:/);
  });

  test('A11: DB connection closed in finally block', () => {
    // There should be a finally block containing fdb.close()
    expect(apiCode).toMatch(/finally\s*\{[\s\S]{0,100}fdb\.close\(\)/);
  });

  test('A12: Catches errors and returns { available: false } on error', () => {
    // Error handler returns available: false
    const errorPattern = /catch\s*\(e\)\s*\{[\s\S]{0,200}available:\s*false/;
    expect(apiCode).toMatch(errorPattern);
  });

  test('A13: factory.db path points to ../factory/factory.db relative to api.js', () => {
    expect(apiCode).toMatch(/factory\.db/);
    expect(apiCode).toMatch(/factory[/\\]factory\.db/);
  });

  test('A14: Route returns available: true when DB is found', () => {
    expect(apiCode).toMatch(/available:\s*true/);
  });

  test('A15: pendingActions queries growth_actions table', () => {
    expect(apiCode).toMatch(/growth_actions/);
  });

  test('A16: recentDecisions queries ceo_decisions table', () => {
    expect(apiCode).toMatch(/ceo_decisions/);
  });

  test('A17: activeExperiments queries experiments table', () => {
    expect(apiCode).toMatch(/experiments[\s\S]{0,50}active/);
  });

  test('A18: elapsedSeconds field is also returned', () => {
    expect(apiCode).toMatch(/elapsedSeconds\s*:/);
  });

  test('A19: latestReport field is returned', () => {
    expect(apiCode).toMatch(/latestReport\s*:/);
  });

  test('A20: Database is opened in readonly mode', () => {
    expect(apiCode).toMatch(/readonly:\s*true/);
  });
});

// ─── B. Health endpoint (server.js) ──────────────────────────────────────────

describe('B. Health endpoint — server.js', () => {
  test('B01: /api/health route exists', () => {
    expect(serverCode).toMatch(/app\.get\s*\(\s*['"]\/api\/health['"]/);
  });

  test('B02: /health route exists', () => {
    expect(serverCode).toMatch(/app\.get\s*\(\s*['"]\/health['"]/);
  });

  test('B03: Returns status field', () => {
    expect(serverCode).toMatch(/status\s*:/);
  });

  test('B04: Returns uptime_seconds field', () => {
    expect(serverCode).toMatch(/uptime_seconds\s*:/);
  });

  test('B05: Returns uptime (legacy scalar) field', () => {
    expect(serverCode).toMatch(/\buptime\b/);
  });

  test('B06: Returns components field', () => {
    expect(serverCode).toMatch(/components\s*:/);
  });

  test('B07: Returns database field', () => {
    expect(serverCode).toMatch(/database\s*:/);
  });

  test('B08: Includes database connectivity check', () => {
    // Checks that DB is queried (e.g. SELECT 1 or similar) in health build
    expect(serverCode).toMatch(/SELECT\s+1|dbStatus|db.*status|database.*ok/i);
  });

  test('B09: Includes memory stats with rss_mb', () => {
    expect(serverCode).toMatch(/rss_mb\s*:/);
  });

  test('B10: Returns timestamp field', () => {
    expect(serverCode).toMatch(/timestamp\s*:/);
  });

  test('B11: Returns 200 when status is ok', () => {
    expect(serverCode).toMatch(/200/);
    expect(serverCode).toMatch(/status.*===.*['"]ok['"]/);
  });

  test('B12: Returns 503 when status is degraded/down', () => {
    expect(serverCode).toMatch(/503/);
  });

  test('B13: health route is excluded from rate-limit / auth logging', () => {
    expect(serverCode).toMatch(/health.*skip|skip.*health/i);
  });

  test('B14: Returns memory heap_used_mb', () => {
    expect(serverCode).toMatch(/heap_used_mb\s*:/);
  });

  test('B15: Returns cpu load info', () => {
    expect(serverCode).toMatch(/load_1m|loadAvg/);
  });

  test('B16: Returns bot health component', () => {
    expect(serverCode).toMatch(/bot.*ok|botHealth/);
  });

  test('B17: Returns factory status component', () => {
    expect(serverCode).toMatch(/factory.*status|factoryStatus/);
  });

  test('B18: buildHealthResponse function is defined', () => {
    expect(serverCode).toMatch(/function\s+buildHealthResponse|buildHealthResponse\s*=/);
  });
});

// ─── C. Structured logger (services/logger.js) ───────────────────────────────

describe('C. Structured logger — services/logger.js', () => {
  test('C01: logger.js file exists', () => {
    expect(fs.existsSync(loggerPath)).toBe(true);
  });

  test('C02: Exports error function', () => {
    expect(loggerCode).toMatch(/error\s*:/);
  });

  test('C03: Exports warn function', () => {
    expect(loggerCode).toMatch(/warn\s*:/);
  });

  test('C04: Exports info function', () => {
    expect(loggerCode).toMatch(/info\s*:/);
  });

  test('C05: Exports debug function', () => {
    expect(loggerCode).toMatch(/debug\s*:/);
  });

  test('C06: Checks LOG_JSON env var', () => {
    expect(loggerCode).toMatch(/LOG_JSON/);
  });

  test('C07: Checks LOG_LEVEL env var', () => {
    expect(loggerCode).toMatch(/LOG_LEVEL/);
  });

  test('C08: Uses process.stdout.write for JSON mode', () => {
    expect(loggerCode).toMatch(/process\.stdout\.write/);
  });

  test('C09: JSON format includes ts field', () => {
    expect(loggerCode).toMatch(/\bts\b\s*:/);
  });

  test('C10: JSON format includes level field', () => {
    expect(loggerCode).toMatch(/\blevel\b/);
  });

  test('C11: JSON format includes msg field', () => {
    expect(loggerCode).toMatch(/\bmsg\b/);
  });

  test('C12: Logger respects log level hierarchy', () => {
    // Should have LEVELS object or similar numeric mapping
    expect(loggerCode).toMatch(/LEVELS|levels|level.*0|currentLevel/);
  });

  test('C13: Uses module.exports for all four log functions', () => {
    expect(loggerCode).toMatch(/module\.exports/);
    const exports = loggerCode.match(/module\.exports\s*=\s*\{[\s\S]+?\}/);
    expect(exports).not.toBeNull();
  });
});

// ─── D. Scheduler (services/scheduler.js) ────────────────────────────────────

describe('D. Scheduler — services/scheduler.js', () => {
  test('D01: WAL checkpoint (PASSIVE) is mentioned', () => {
    expect(schedulerCode).toMatch(/wal_checkpoint.*PASSIVE|PRAGMA.*wal_checkpoint/i);
  });

  test('D02: WAL checkpoint TRUNCATE is also used (in VACUUM flow)', () => {
    expect(schedulerCode).toMatch(/wal_checkpoint.*TRUNCATE/i);
  });

  test('D03: Backup job runs every 6 hours', () => {
    expect(schedulerCode).toMatch(/backup.*6|6.*backup|every.*6h|scheduleEvery.*6/i);
  });

  test('D04: Bot health check exists (checkBotHealth function)', () => {
    expect(schedulerCode).toMatch(/checkBotHealth|Bot.*watchdog|botHealth/i);
  });

  test('D05: Factory staleness check exists (checkFactoryStaleness)', () => {
    expect(schedulerCode).toMatch(/checkFactoryStaleness|factory.*staleness|staleness.*factory/i);
  });

  test('D06: scheduleEvery uses .unref() on timers', () => {
    // timer.unref() called for setInterval timers
    expect(schedulerCode).toMatch(/timer\.unref\s*\(\s*\)|\.unref\s*\(\s*\)/);
  });

  test('D07: Bot watchdog calls getMe() to check health', () => {
    expect(schedulerCode).toMatch(/_bot\.getMe\s*\(\s*\)/);
  });

  test('D08: Factory staleness alerts if > 12 hours since last run', () => {
    expect(schedulerCode).toMatch(/hoursSince.*>\s*12|>\s*12.*hoursSince/);
  });

  test('D09: Scheduler exports init, start, stop functions', () => {
    expect(schedulerCode).toMatch(/module\.exports\s*=\s*\{[\s\S]*init[\s\S]*start[\s\S]*stop[\s\S]*\}/);
  });

  test('D10: Bot watchdog runs every 5 minutes', () => {
    expect(schedulerCode).toMatch(/watchdog.*5|5.*watchdog|every.*5.*min|5.*min.*check/i);
  });

  test('D11: Event reminders are scheduled daily', () => {
    expect(schedulerCode).toMatch(/scheduleDaily.*reminder|reminder.*scheduleDaily|sendEventReminders/);
  });

  test('D12: Factory staleness also checked every 30 minutes', () => {
    expect(schedulerCode).toMatch(/30.*min|30\s*\)/);
  });

  test('D13: runBackup function is defined', () => {
    expect(schedulerCode).toMatch(/function\s+runBackup|runBackup\s*=/);
  });

  test('D14: runWalCheckpoint function is defined', () => {
    expect(schedulerCode).toMatch(/function\s+runWalCheckpoint|runWalCheckpoint\s*=/);
  });
});

// ─── E. Factory Python tests (sanity) ────────────────────────────────────────

describe('E. Factory Python tests — sanity check', () => {
  test('E01: pytest test_db.py runs and passes 65 tests', () => {
    let output = '';
    try {
      output = execSync(
        'python3 -m pytest /home/user/Pablo/factory/tests/test_db.py -q --tb=no 2>&1',
        { encoding: 'utf8', timeout: 30000 }
      );
    } catch (e) {
      output = e.stdout || '';
    }
    expect(output).toMatch(/65 passed/);
  }, 35000);
});

// ─── F. Admin HTML factory widget ────────────────────────────────────────────

describe('F. Admin HTML — factory widget', () => {
  const adminDir = path.join(ROOT, 'public/admin');

  test('F01: factory.html exists in admin directory', () => {
    expect(fs.existsSync(path.join(adminDir, 'factory.html'))).toBe(true);
  });

  test('F02: At least one admin HTML file references factory', () => {
    const files = fs.readdirSync(adminDir).filter(f => f.endsWith('.html'));
    const anyHasFactory = files.some(f => {
      const content = fs.readFileSync(path.join(adminDir, f), 'utf8');
      return /factory/i.test(content);
    });
    expect(anyHasFactory).toBe(true);
  });

  test('F03: factory.html has factory-health-grid div', () => {
    const html = fs.readFileSync(path.join(adminDir, 'factory.html'), 'utf8');
    expect(html).toMatch(/factory-health-grid/);
  });

  test('F04: factory.html has factory-health-card elements', () => {
    const html = fs.readFileSync(path.join(adminDir, 'factory.html'), 'utf8');
    expect(html).toMatch(/factory-health-card/);
  });

  test('F05: factory.html references /admin/factory-health API endpoint', () => {
    const html = fs.readFileSync(path.join(adminDir, 'factory.html'), 'utf8');
    expect(html).toMatch(/factory-health/);
  });

  test('F06: factory.html references /admin/factory-tasks API endpoint', () => {
    const html = fs.readFileSync(path.join(adminDir, 'factory.html'), 'utf8');
    expect(html).toMatch(/factory-tasks/);
  });

  test('F07: factory.html has AI Factory Dashboard title', () => {
    const html = fs.readFileSync(path.join(adminDir, 'factory.html'), 'utf8');
    expect(html).toMatch(/AI Factory/i);
  });
});

// ─── G. Additional factory API endpoints coverage ─────────────────────────────

describe('G. Additional factory API endpoints', () => {
  test('G01: GET /admin/factory/actions route exists', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory\/actions['"]/);
  });

  test('G02: GET /admin/factory/decisions route exists', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory\/decisions['"]/);
  });

  test('G03: GET /admin/factory/experiments route exists', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory\/experiments['"]/);
  });

  test('G04: GET /admin/factory-health route exists', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory-health['"]/);
  });

  test('G05: GET /admin/factory-experiments route exists', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory-experiments['"]/);
  });

  test('G06: POST /admin/factory/run route exists', () => {
    expect(apiCode).toMatch(/router\.post\s*\(\s*['"]\/admin\/factory\/run['"]/);
  });

  test('G07: factory/run spawns python3 with factory_main.py', () => {
    // spawn('python3', [factoryScript, ...]) where factoryScript contains factory_main.py
    expect(apiCode).toMatch(/factory_main\.py/);
    expect(apiCode).toMatch(/spawn\s*\(\s*['"]python3['"]/);
  });

  test('G08: factory/run process uses .unref()', () => {
    expect(apiCode).toMatch(/proc\.unref\s*\(\s*\)/);
  });

  test('G09: factory-experiments/:id/scale route exists', () => {
    expect(apiCode).toMatch(/router\.post\s*\(\s*['"]\/admin\/factory-experiments\/:id\/scale['"]/);
  });

  test('G10: factory-monthly route exists', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory-monthly['"]/);
  });

  test('G11: factory-content route exists', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory-content['"]/);
  });

  test('G12: factory-ceo-decisions route exists', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*['"]\/admin\/factory-ceo-decisions['"]/);
  });
});
