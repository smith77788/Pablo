#!/usr/bin/env node
/** 🌿 Organism Runner — запускает весь живой организм агентов */
const { runOrchestrator } = require('./orchestrator');
const BugHunter = require('./bug-hunter');
const { tgSend, logAgent } = require('./lib/base');

async function runOrganism() {
  console.log('\n' + '═'.repeat(60));
  console.log('🌿 NEVESTY MODELS — ЖИВОЙ ОРГАНИЗМ АГЕНТОВ');
  console.log('═'.repeat(60) + '\n');

  const startTime = Date.now();

  // Фаза 1: Bug Hunter проверяет агентов и код
  console.log('🐛 Фаза 1: Bug Hunter сканирует систему...');
  const hunter = new BugHunter();
  await hunter.run();

  // Фаза 2: Orchestrator запускает все 25 агентов
  console.log('\n🧠 Фаза 2: Orchestrator запускает 25 агентов...');
  const result = await runOrchestrator();

  const totalTime = ((Date.now() - startTime) / 1000).toFixed(1);

  // Итоговый вердикт
  let verdict, emoji;
  if (result.criticalCount > 0) {
    verdict = 'ТРЕБУЕТСЯ СРОЧНОЕ ВНИМАНИЕ'; emoji = '🚨';
  } else if (result.highCount > 3) {
    verdict = 'ЕСТЬ СЕРЬЁЗНЫЕ ПРОБЛЕМЫ'; emoji = '🔴';
  } else if (result.healthScore >= 80) {
    verdict = 'ОРГАНИЗМ ЗДОРОВ'; emoji = '💚';
  } else {
    verdict = 'ОРГАНИЗМ СТАБИЛЕН'; emoji = '🟡';
  }

  const summary = [
    `${emoji} *Организм завершил цикл*`,
    ``,
    `*Вердикт: ${verdict}*`,
    `Health Score: ${result.healthScore}%`,
    `Общее время: ${totalTime}с`,
    ``,
    `_Следующая проверка через 30 минут_`,
  ].join('\n');

  await tgSend(summary);
  await logAgent('Organism Runner', `🌿 Цикл завершён: ${verdict}, ${totalTime}с`);

  console.log('\n' + '═'.repeat(60));
  console.log(`${emoji} ВЕРДИКТ: ${verdict}`);
  console.log(`   Health Score: ${result.healthScore}%`);
  console.log(`   Общее время: ${totalTime}с`);
  console.log('═'.repeat(60) + '\n');
}

runOrganism()
  .then(() => process.exit(0))
  .catch(err => {
    console.error('ORGANISM CRASH:', err);
    tgSend(`🚨 Organism runner crashed: ${err.message}`).finally(() => process.exit(1));
  });
