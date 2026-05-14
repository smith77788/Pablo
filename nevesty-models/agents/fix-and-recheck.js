#!/usr/bin/env node
/** 🔧→🔄 Fix & Recheck — Auto Fixer + полная перепроверка */
const AutoFixer           = require('./auto-fixer');
const { runOrchestrator } = require('./orchestrator');
const BugHunter           = require('./bug-hunter');
const { tgSend, tgSendGetId, tgEditMessage, progressBar, logAgent } = require('./lib/base');

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

  // Единое прогресс-сообщение — будем его редактировать
  const buildMsg = (step, label, bar, extra = '') => [
    `🔧 Авто-исправление и перепроверка`,
    ``,
    `[${bar}]`,
    `Шаг ${step}/3: ${label}`,
    extra,
  ].filter(l => l !== undefined && l !== '').join('\n');

  const progressRef = await tgSendGetId(buildMsg(1, 'Auto Fixer запускается...', progressBar(0, 3)));

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // ── Шаг 1: Auto Fixer (DB-уровень) ───────────────────────────────────────
  const fixer = new AutoFixer();
  await fixer.run({ silent: true });
  const fixed = fixer.fixed;
  const fixerIssues = fixer.findings.filter(f => !['✅', '⚪'].includes(f.sev));

  if (progressRef) {
    const fixedLine = fixed.length ? `✅ Исправлено: ${fixed.length} проблем(ы)` : '✔ Авто-фикс: нечего исправлять';
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      buildMsg(1, 'Auto Fixer завершён', progressBar(1, 3), fixedLine));
  }
  await sleep(800);

  // ── Шаг 2: Bug Hunter ─────────────────────────────────────────────────────
  if (progressRef) {
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      buildMsg(2, 'Bug Hunter проверяет код...', progressBar(1, 3)));
  }

  const hunter = new BugHunter();
  await hunter.run({ silent: true });
  const hunterIssues = (hunter.findings || []).filter(f => !['✅', '⚪'].includes(f.sev));

  if (progressRef) {
    const hunterLine = hunterIssues.length ? `🐛 Найдено: ${hunterIssues.length} проблем(ы)` : '✔ Bug Hunter: ошибок нет';
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      buildMsg(2, 'Bug Hunter завершён', progressBar(2, 3), hunterLine));
  }
  await sleep(800);

  // ── Шаг 3: Полная перепроверка ────────────────────────────────────────────
  if (progressRef) {
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      buildMsg(3, 'Полная проверка 25 агентами...', progressBar(2, 3)));
  }

  const result = await runOrchestrator();

  // ── Финальный прогресс-бар ────────────────────────────────────────────────
  if (progressRef) {
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      buildMsg(3, 'Всё завершено!', progressBar(3, 3)));
  }

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
