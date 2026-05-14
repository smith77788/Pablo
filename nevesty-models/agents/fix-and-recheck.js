#!/usr/bin/env node
/** 🔧→🔄 Fix & Recheck — запускает Auto Fixer, затем полный organism check */
const AutoFixer       = require('./auto-fixer');
const { runOrchestrator } = require('./orchestrator');
const BugHunter       = require('./bug-hunter');
const { tgSend, logAgent } = require('./lib/base');

async function fixAndRecheck() {
  const t0 = Date.now();
  console.log('\n' + '═'.repeat(60));
  console.log('🔧 FIX & RECHECK — запуск автоисправления + повторная проверка');
  console.log('═'.repeat(60) + '\n');

  await tgSend('🔧 *Авто-исправление запущено*\n\n_Агенты исправляют найденные проблемы..._', {
    parse_mode: 'Markdown'
  });

  // Фаза 1: Auto Fixer
  console.log('🔧 Фаза 1: Auto Fixer...');
  const fixer = new AutoFixer();
  await fixer.run();
  const fixedCount = fixer.fixed.length;
  const fixSummary = fixedCount > 0
    ? `Исправлено: ${fixedCount} проблем\n` + fixer.fixed.map(f => `• ${f}`).join('\n')
    : 'Автоматических исправлений не потребовалось';

  await tgSend(`✅ *Auto Fixer завершил*\n\n${fixSummary}`, { parse_mode: 'Markdown' });

  // Фаза 2: Bug Hunter
  console.log('\n🐛 Фаза 2: Bug Hunter...');
  const hunter = new BugHunter();
  await hunter.run();

  // Фаза 3: Полная перепроверка Orchestrator
  console.log('\n🧠 Фаза 3: Полная перепроверка (Orchestrator + 25 агентов)...');
  await tgSend('🔄 *Запускаю полную перепроверку...*\n_25 агентов анализируют систему_', {
    parse_mode: 'Markdown'
  });

  const result = await runOrchestrator();

  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  await logAgent('Fix & Recheck', `🔧→🔄 Завершено: исправлено ${fixedCount}, Health Score: ${result.healthScore}%, время ${elapsed}с`);

  console.log('\n' + '═'.repeat(60));
  console.log(`✅ FIX & RECHECK ЗАВЕРШЁН`);
  console.log(`   Исправлено: ${fixedCount} проблем`);
  console.log(`   Health Score: ${result.healthScore}%`);
  console.log(`   Общее время: ${elapsed}с`);
  console.log('═'.repeat(60) + '\n');
}

fixAndRecheck()
  .then(() => process.exit(0))
  .catch(err => {
    console.error('FIX & RECHECK CRASH:', err);
    tgSend(`🚨 Fix & Recheck crashed: ${err.message}`).finally(() => process.exit(1));
  });
