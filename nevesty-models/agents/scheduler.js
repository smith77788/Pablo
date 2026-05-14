/**
 * 🧬 Living Organism Scheduler
 * Runs continuously, auto-detects issues, auto-fixes what it can,
 * reports results to Telegram admins.
 */
require('dotenv').config({ path: require('path').join(__dirname, '../.env') });

const { runOrchestrator } = require('./orchestrator');
const AutoFixer = require('./auto-fixer');
const { tgSend, logAgent } = require('./lib/base');

// Run immediately, then every 6 hours
const INTERVAL_MS = 6 * 60 * 60 * 1000;

// Track consecutive failures to avoid spam
let lastHealthScore = null;
let failureCount = 0;

async function runCycle() {
  const ts = new Date().toLocaleString('ru', { timeZone: 'Europe/Moscow' });
  console.log(`\n[${ts}] 🧬 Organism cycle starting...`);

  try {
    // Step 1: Run auto-fixer FIRST (fix what we already know about)
    const fixer = new AutoFixer();
    await fixer.run({ silent: true });
    const fixed = fixer.fixed || [];

    // Step 2: Run full organism check
    const result = await runOrchestrator();
    const { healthScore, criticalCount, highCount } = result;

    // Step 3: If health degraded significantly, run fixer again
    if (lastHealthScore !== null && healthScore < lastHealthScore - 10) {
      await tgSend(
        `⚠️ Health score упал: ${lastHealthScore}% → ${healthScore}%\n` +
        `🔴 ${criticalCount} критических, 🟠 ${highCount} высоких\n` +
        `Запускаю авто-исправление...`
      );
      const fixer2 = new AutoFixer();
      await fixer2.run({ silent: true });
    }

    // Step 4: Notify about significant events
    if (fixed.length > 0) {
      await tgSend(
        `🔧 Авто-исправлено: ${fixed.length} проблем\n` +
        fixed.slice(0, 5).map(f => `• ${f}`).join('\n')
      );
    }

    lastHealthScore = healthScore;
    failureCount = 0;

    await logAgent('Scheduler', `Цикл завершён: Score=${healthScore}% fixed=${fixed.length}`);
    console.log(`[Scheduler] Cycle done. Score=${healthScore}% fixed=${fixed.length}`);

  } catch (err) {
    failureCount++;
    console.error(`[Scheduler] Cycle error (${failureCount}):`, err.message);
    if (failureCount <= 3) {
      await tgSend(`🚨 Organism scheduler error: ${err.message}`).catch(() => {});
    }
  }
}

// Run immediately
console.log('🧬 Living Organism Scheduler started');
tgSend('🟢 Organism scheduler запущен. Первая проверка начинается...').catch(() => {});

runCycle().then(() => {
  // Schedule recurring runs
  setInterval(runCycle, INTERVAL_MS);
  console.log(`[Scheduler] Next run in ${INTERVAL_MS / 3600000}h`);
});
