/**
 * 🧬 Living Organism Scheduler — 24/7 autonomous operation
 *
 * Каждые 15 минут запускает полный цикл:
 * 1. AutoFixer — немедленные тех. исправления (сессии, индексы, orphans)
 * 2. SmartOrchestrator — 28 агентов анализируют и исправляют
 * 3. Детальный audit-log каждого действия
 *
 * Администратор может посмотреть что делал организм за ЛЮБОЙ период через
 * Telegram → 💬 Обсуждения или 📡 Фид агентов.
 */
'use strict';
require('dotenv').config({ path: require('path').join(__dirname, '../.env') });

let runCheck;
try {
  runCheck = require('./smart-orchestrator').runSmartOrchestrator;
} catch {
  runCheck = require('./orchestrator').runOrchestrator;
}

const AutoFixer = require('./auto-fixer');
const { tgSend, logAgent, dbAll, dbRun, dbGet } = require('./lib/base');

const INTERVAL_MS = 15 * 60 * 1000;   // 15 минут
const CYCLE_LOG_LIMIT = 10000;         // храним до 10000 записей в agent_logs

let cycleNumber   = 0;
let lastScore     = null;
let totalFixed    = 0;
let totalCycles   = 0;
let startupTime   = Date.now();
let cycleRunning  = false;  // circuit breaker: skip if prev cycle still running

// ─── Детальная запись каждого цикла ──────────────────────────────────────────

async function logCycleStart(cycleNum) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  await dbRun(
    `INSERT INTO agent_discussions (from_agent, to_agent, topic, message) VALUES (?,?,?,?)`,
    ['Scheduler', 'all', `Цикл #${cycleNum} старт`,
     `🧬 Цикл #${cycleNum} начат в ${ts}. Аптайм: ${formatUptime(Date.now() - startupTime)}.`]
  ).catch(() => {});
}

async function logCycleEnd(cycleNum, score, fixed, elapsed) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  await dbRun(
    `INSERT INTO agent_discussions (from_agent, to_agent, topic, message) VALUES (?,?,?,?)`,
    ['Scheduler', 'all', `Цикл #${cycleNum} завершён`,
     `✅ Цикл #${cycleNum} завершён в ${ts}. Score=${score}%, исправлено=${fixed}, время=${elapsed}с. Итого за сессию: циклов=${totalCycles}, исправлений=${totalFixed}.`]
  ).catch(() => {});
}

function formatUptime(ms) {
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  return h > 0 ? `${h}ч ${m}м` : `${m}м`;
}

// ─── Очистка старых логов (чтобы БД не пухла) ────────────────────────────────

async function pruneOldLogs() {
  try {
    // Оставляем только 10000 последних записей в agent_logs
    const count = await dbGet('SELECT COUNT(*) as n FROM agent_logs');
    if (count?.n > CYCLE_LOG_LIMIT) {
      await dbRun(
        `DELETE FROM agent_logs WHERE id IN (
           SELECT id FROM agent_logs ORDER BY created_at ASC LIMIT ?
         )`,
        [count.n - CYCLE_LOG_LIMIT]
      );
    }
    // Оставляем 30 дней обсуждений
    await dbRun(
      `DELETE FROM agent_discussions WHERE created_at < datetime('now', '-30 days')`
    ).catch(() => {});
    // Оставляем 7 дней открытых findings (закрытые — навсегда)
    await dbRun(
      `DELETE FROM agent_findings WHERE status='open' AND created_at < datetime('now', '-7 days')`
    ).catch(() => {});
  } catch {}
}

// ─── Основной цикл ───────────────────────────────────────────────────────────

async function runCycle() {
  if (cycleRunning) {
    const ts = new Date().toLocaleString('ru', { timeZone: 'Europe/Moscow' });
    console.log(`[${ts}] ⏭ Цикл пропущен — предыдущий ещё выполняется`);
    return;
  }
  cycleRunning = true;
  cycleNumber++;
  totalCycles++;
  const t0 = Date.now();
  const ts = new Date().toLocaleString('ru', { timeZone: 'Europe/Moscow' });
  console.log(`\n[${ts}] 🧬 Цикл #${cycleNumber} начат...`);

  await logCycleStart(cycleNumber);

  try {
    // ── Шаг 1: AutoFixer (быстрые тех. исправления) ──────────────────────────
    const fixer = new AutoFixer();
    await fixer.run({ silent: true });
    const autoFixed = fixer.fixed || [];
    if (autoFixed.length > 0) {
      totalFixed += autoFixed.length;
      await tgSend(
        `🔧 AutoFixer [цикл #${cycleNumber}]:\n` +
        autoFixed.slice(0, 5).map(f => `• ${f}`).join('\n')
      ).catch(() => {});
    }

    // ── Шаг 2: Smart Orchestrator (28 агентов) ───────────────────────────────
    const result = await runCheck();
    const { healthScore = 100, criticalCount = 0, highCount = 0 } = result;
    const orchFixed = (result.fixResults || []).filter(r => r.outcome === 'fixed').length;
    totalFixed += orchFixed;

    // ── Шаг 3: Алерт если health score деградировал ──────────────────────────
    if (lastScore !== null && healthScore < lastScore - 15) {
      await tgSend(
        `⚠️ Health Score деградировал: ${lastScore}% → ${healthScore}%\n` +
        `🔴 ${criticalCount} крит, 🟠 ${highCount} высоких\n` +
        `Цикл #${cycleNumber} — проверьте бот.`
      ).catch(() => {});
    }

    // ── Шаг 4: Очистка старых логов ──────────────────────────────────────────
    await pruneOldLogs();

    const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    lastScore = healthScore;

    await logCycleEnd(cycleNumber, healthScore, autoFixed.length + orchFixed, elapsed);
    await logAgent('Scheduler',
      `Цикл #${cycleNumber}: Score=${healthScore}% autoFixed=${autoFixed.length} orchFixed=${orchFixed} ${elapsed}с`
    );
    console.log(`[Scheduler] Цикл #${cycleNumber} done. Score=${healthScore}% fixed=${autoFixed.length + orchFixed} (${elapsed}с)`);

  } catch (err) {
    const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    console.error(`[Scheduler] Цикл #${cycleNumber} ERROR (${elapsed}с):`, err.message);
    await dbRun(
      `INSERT INTO agent_discussions (from_agent, to_agent, topic, message) VALUES (?,?,?,?)`,
      ['Scheduler', 'all', `❌ Цикл #${cycleNumber} ошибка`,
       `Ошибка в цикле #${cycleNumber}: ${err.message}. Следующая попытка через ${INTERVAL_MS/60000} мин.`]
    ).catch(() => {});
    // Не спамим в Telegram при повторных ошибках
    if (cycleNumber <= 3 || cycleNumber % 10 === 0) {
      await tgSend(`🚨 Organism error [цикл #${cycleNumber}]: ${err.message}`).catch(() => {});
    }
  } finally {
    cycleRunning = false;
  }
}

// ─── Запуск ──────────────────────────────────────────────────────────────────

console.log('🧬 Living Organism Scheduler запущен (28 агентов, каждые 15 мин)');

const uptimeStr = () => formatUptime(Date.now() - startupTime);

tgSend(
  `🟢 Organism запущен\n` +
  `28 агентов | цикл каждые 15 мин\n\n` +
  `Агенты:\n` +
  `• 25 аналитиков (UX, Security, DB, Booking...)\n` +
  `• 💰 Sales Analyst — конверсия и продажи\n` +
  `• 📝 Content Manager — контент бота\n` +
  `• 📊 Activity Logger — аудит каждого действия\n\n` +
  `Первая проверка начинается...`
).catch(() => {});

// Первый цикл — сразу
runCycle().then(() => {
  // Повторяем каждые 15 минут
  setInterval(runCycle, INTERVAL_MS);
  console.log(`[Scheduler] Следующий цикл через ${INTERVAL_MS / 60000} мин`);
});
