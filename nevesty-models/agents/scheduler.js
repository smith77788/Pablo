/**
 * 🧬 Living Organism Scheduler
 * Runs continuously, auto-detects issues, auto-fixes what it can,
 * reports results to Telegram admins.
 */
require('dotenv').config({ path: require('path').join(__dirname, '../.env') });

// Try smart orchestrator first, fall back to regular orchestrator
let runCheck;
try {
  runCheck = require('./smart-orchestrator').runSmartOrchestrator;
} catch {
  runCheck = require('./orchestrator').runOrchestrator;
}

const AutoFixer = require('./auto-fixer');
const { tgSend, logAgent, dbAll } = require('./lib/base');

// Run immediately, then every 30 minutes
const INTERVAL_MS = 30 * 60 * 1000;

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

    // Step 2: Run full organism check (smart or regular)
    const result = await runCheck();
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

    // Step 5: Report top-3 agent discussions from this cycle
    try {
      const discussions = await dbAll(
        'SELECT * FROM agent_discussions ORDER BY created_at DESC LIMIT 3'
      );
      if (discussions.length > 0) {
        let dMsg = `💬 Обсуждения агентов (топ-3):\n`;
        discussions.forEach(d => {
          const to = d.to_agent ? ` → ${d.to_agent}` : ' → all';
          const snippet = (d.message || '').slice(0, 120);
          dMsg += `\n🤖 ${d.from_agent}${to}:\n"${snippet}${snippet.length < (d.message||'').length ? '…' : ''}"\n`;
        });
        await tgSend(dMsg).catch(() => {});
      }
    } catch (e) { console.warn('[Scheduler] discussions report skipped:', e.message); }

    // Step 6: Report auto-fixed findings
    try {
      const autoFixed = await dbAll(
        "SELECT * FROM agent_findings WHERE status='fixed' ORDER BY fixed_at DESC, created_at DESC LIMIT 5"
      );
      if (autoFixed.length > 0) {
        let fMsg = `✅ Авто-исправленные находки (${autoFixed.length}):\n`;
        autoFixed.forEach(f => {
          fMsg += `• [${f.severity || 'info'}] ${(f.message || '').slice(0, 100)}\n`;
        });
        await tgSend(fMsg).catch(() => {});
      }
    } catch (e) { console.warn('[Scheduler] findings report skipped:', e.message); }

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
