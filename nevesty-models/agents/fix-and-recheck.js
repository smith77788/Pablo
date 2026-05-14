#!/usr/bin/env node
/** 🔧→🔄 Fix & Recheck — Auto Fixer + полная перепроверка */
const AutoFixer           = require('./auto-fixer');
const { runOrchestrator } = require('./orchestrator');
const BugHunter           = require('./bug-hunter');
const { tgSend, logAgent } = require('./lib/base');

// Проблемы, которые НЕ могут быть исправлены автоматически (требуют ручной правки кода)
const MANUAL_REVIEW_PATTERNS = [
  'SQL injection', 'template literal', 'parse_mode', 'MarkdownV2', 'isAdmin',
  'LIMIT', 'SELECT *', 'N+1', 'INDEX', 'индекс', 'рекурс',
];

function isAutoFixable(msg) {
  return !MANUAL_REVIEW_PATTERNS.some(p => msg.toLowerCase().includes(p.toLowerCase()));
}

async function fixAndRecheck() {
  const t0 = Date.now();
  console.log('\n🔧 FIX & RECHECK\n' + '═'.repeat(50));

  await tgSend('🔧 *Запущено авто-исправление*\n_Шаг 1/3: Auto Fixer..._', { parse_mode: 'Markdown' });

  // ── Шаг 1: Auto Fixer (DB-уровень) ───────────────────────────────────────
  const fixer = new AutoFixer();
  await fixer.run({ silent: true });
  const fixed = fixer.fixed;
  const fixerIssues = fixer.findings.filter(f => !['✅', '⚪'].includes(f.sev));

  // ── Шаг 2: Bug Hunter ─────────────────────────────────────────────────────
  await tgSend('🐛 *Шаг 2/3: Bug Hunter проверяет код...*', { parse_mode: 'Markdown' });
  const hunter = new BugHunter();
  await hunter.run({ silent: true });

  // ── Шаг 3: Полная перепроверка ────────────────────────────────────────────
  await tgSend('🧠 *Шаг 3/3: Полная перепроверка 25 агентами...*', { parse_mode: 'Markdown' });
  const result = await runOrchestrator();

  // ── Итоговый отчёт об исправлениях ───────────────────────────────────────
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);

  // Из результатов нового прогона — разделяем на авто-исправимые и ручные
  const remaining = result.agentSummaries || [];
  const autoFixable = [];
  const manualNeeded = [];

  for (const agent of remaining) {
    for (const f of (agent.issues || [])) {
      if (isAutoFixable(f.msg)) autoFixable.push(`${f.sev} [${agent.name}] ${f.msg}`);
      else manualNeeded.push(`${f.sev} [${agent.name}] ${f.msg}`);
    }
  }

  let summary = `🔧 *Итог авто-исправления*\n\n`;

  if (fixed.length > 0) {
    summary += `*✅ Исправлено автоматически (${fixed.length}):*\n`;
    fixed.forEach(f => { summary += `• ${f}\n`; });
    summary += '\n';
  } else {
    summary += `_Автоматических исправлений не потребовалось_\n\n`;
  }

  if (manualNeeded.length > 0) {
    summary += `*⚠️ Требуют ручной правки кода (${manualNeeded.length}):*\n`;
    manualNeeded.slice(0, 10).forEach(m => { summary += `${m}\n`; });
    if (manualNeeded.length > 10) summary += `_...и ещё ${manualNeeded.length - 10} проблем_\n`;
    summary += '\n';
    summary += `_Эти проблемы требуют изменений в bot\\.js/api\\.js — автомат их не трогает_\n`;
  } else if (remaining.length === 0) {
    summary += `*🎉 Все проблемы устранены!*\n`;
  }

  summary += `\n⏱ Общее время: ${elapsed}с`;

  await tgSend(summary, {
    parse_mode: 'Markdown',
    reply_markup: {
      inline_keyboard: [
        [{ text: '🔄 Перепроверить снова', callback_data: 'adm_run_organism' }],
        [{ text: '← Панель организма',     callback_data: 'adm_organism'      }],
      ]
    }
  });

  await logAgent('Fix & Recheck',
    `🔧 Завершено: исправлено ${fixed.length}, требует ручной правки ${manualNeeded.length}, Health ${result.healthScore}%, ${elapsed}с`
  );

  console.log(`\n✅ DONE: fixed=${fixed.length} manual=${manualNeeded.length} score=${result.healthScore}% (${elapsed}с)\n`);
}

fixAndRecheck()
  .then(() => process.exit(0))
  .catch(err => {
    console.error('CRASH:', err);
    tgSend(`🚨 Fix & Recheck crashed: ${err.message}`).finally(() => process.exit(1));
  });
