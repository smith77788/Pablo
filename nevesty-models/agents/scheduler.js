/** 🕐 Scheduler — 24/7 organism runner. Checks every 6 hours automatically. */
require('dotenv').config({ path: require('path').join(__dirname, '../.env') });

const { tgSend } = require('./lib/base');
const { runOrchestrator } = require('./orchestrator');

const INTERVAL_MS = 6 * 60 * 60 * 1000; // 6 hours

function timestamp() {
  return new Date().toLocaleString('ru', { timeZone: 'Europe/Moscow' });
}

function log(msg) {
  console.log(`[${timestamp()}] ${msg}`);
}

async function runCheck() {
  log('🟢 Запуск планового organism-check...');
  await tgSend(`🕐 Плановая проверка организма\n⏰ ${timestamp()}`);

  let result;
  try {
    result = await runOrchestrator();
  } catch (err) {
    log(`❌ Ошибка во время проверки: ${err.message}`);
    console.error(err);
    await tgSend(`⚠️ Scheduler: ошибка organism-check\n${err.message}`);
    return;
  }

  const { healthScore, criticalCount, highCount, mediumCount, okCount } = result;
  const icon = healthScore >= 80 ? '💚' : healthScore >= 60 ? '🟡' : '🔴';

  log(`✅ Проверка завершена. Health=${healthScore}% 🔴${criticalCount} 🟠${highCount} 🟡${mediumCount} ✅${okCount}`);
  await tgSend(
    `${icon} Плановая проверка завершена\n` +
    `Health Score: ${healthScore}%\n` +
    `🔴 ${criticalCount}  🟠 ${highCount}  🟡 ${mediumCount}  ✅ ${okCount}\n` +
    `⏰ ${timestamp()}\n` +
    `⏭ Следующая через 6 часов`
  );
}

async function start() {
  log('🚀 Scheduler запущен. Интервал: каждые 6 часов.');
  await tgSend(`🚀 Nevesty Scheduler запущен\nOrganism будет проверяться каждые 6 часов\n⏰ ${timestamp()}`);

  // Run immediately on start
  await runCheck();

  // Then repeat every 6 hours
  setInterval(async () => {
    try {
      await runCheck();
    } catch (err) {
      log(`❌ Необработанная ошибка в setInterval: ${err.message}`);
      console.error(err);
    }
  }, INTERVAL_MS);
}

start().catch(err => {
  log(`❌ Fatal error in scheduler start: ${err.message}`);
  console.error(err);
  process.exit(1);
});
