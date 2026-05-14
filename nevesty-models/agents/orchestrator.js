/** 🧠 Orchestrator — Prefrontal Cortex | Главный мозг: агрегирует, решает, подтверждает */
const { logAgent, tgSend, dbAll, dbRun } = require('./lib/base');

// Импортируем всех агентов
const agents = [
  require('./01-ux-architect'),
  require('./02-booking-completeness'),
  require('./03-model-showcase'),
  require('./04-order-lifecycle'),
  require('./05-client-experience'),
  require('./06-admin-experience'),
  require('./07-message-threading'),
  require('./08-notification-engine'),
  require('./09-security-guard'),
  require('./10-keyboard-optimizer'),
  require('./11-db-optimizer'),
  require('./12-session-manager'),
  require('./13-input-validator'),
  require('./14-markdown-safety'),
  require('./15-error-recovery'),
  require('./16-photo-handler'),
  require('./17-search-enhancer'),
  require('./18-response-formatter'),
  require('./19-pagination-checker'),
  require('./20-state-machine'),
  require('./21-admin-protection'),
  require('./22-sql-safety'),
  require('./23-deeplink-handler'),
  require('./24-performance-tuner'),
  require('./25-consistency-checker'),
];

// SEV emoji values from base.js
const SEV_EMO = { CRITICAL:'🔴', HIGH:'🟠', MEDIUM:'🟡', LOW:'🟢', INFO:'⚪', OK:'✅' };
const SEV_WEIGHT = { '🔴':100, '🟠':50, '🟡':20, '🟢':5, '⚪':1, '✅':0 };

async function runOrchestrator() {
  const startTime = Date.now();
  console.log('\n🧠 ORCHESTRATOR — запуск полной проверки организма...\n');
  await tgSend(`🧠 *Orchestrator* запустил полную проверку системы\n_25 агентов-органов активированы_`);

  const allFindings = [];
  const agentReports = [];
  let criticalCount = 0, highCount = 0, okCount = 0;

  // Запускаем всех агентов последовательно (каждый логирует сам)
  for (const AgentClass of agents) {
    const agent = new AgentClass();
    try {
      await agent.run();
      const findings = agent.findings || [];
      allFindings.push(...findings.map(f => ({ ...f, agentName: agent.name })));
      const crit = findings.filter(f => f.sev === SEV_EMO.CRITICAL).length;
      const high = findings.filter(f => f.sev === SEV_EMO.HIGH).length;
      const ok   = findings.filter(f => f.sev === SEV_EMO.OK).length;
      criticalCount += crit; highCount += high; okCount += ok;
      agentReports.push({ name: agent.name, emoji: agent.emoji, findings, crit, high, ok });
    } catch (err) {
      console.error(`❌ Agent ${agent.name} crashed:`, err.message);
      agentReports.push({ name: agent.name, emoji: '❌', findings: [], crit:0, high:0, ok:0, error: err.message });
    }
  }

  // Подсчёт health score
  const totalWeight = allFindings.reduce((s, f) => s + (SEV_WEIGHT[f.sev] || 0), 0);
  const maxWeight   = allFindings.length * SEV_WEIGHT.CRITICAL;
  const healthScore = maxWeight > 0 ? Math.max(0, Math.round((1 - totalWeight / maxWeight) * 100)) : 100;

  // Топ критических проблем
  const critical = allFindings.filter(f => f.sev === 'CRITICAL' || f.sev === 'HIGH')
    .slice(0, 5)
    .map(f => `• [${f.sev}] ${f.agentName}: ${f.msg}`);

  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);

  // Логируем итог в DB
  await logAgent('Orchestrator',
    `🧠 Проверка завершена: ${agents.length} агентов, ` +
    `CRITICAL×${criticalCount} HIGH×${highCount} OK×${okCount}, ` +
    `Health Score: ${healthScore}%`
  );

  // Финальный отчёт в Telegram
  const icon = healthScore >= 80 ? '💚' : healthScore >= 60 ? '🟡' : '🔴';
  const report = [
    `🧠 *Orchestrator — Итоговый отчёт*`,
    ``,
    `${icon} *Health Score: ${healthScore}%*`,
    `🔴 CRITICAL: ${criticalCount} | 🟠 HIGH: ${highCount} | ✅ OK: ${okCount}`,
    `⏱ Время: ${elapsed}с | Агентов: ${agents.length}`,
    ``,
    critical.length ? `*Топ проблем:*\n${critical.join('\n')}` : '✅ Критических проблем не найдено',
  ].join('\n');

  await tgSend(report);

  console.log('\n' + '═'.repeat(60));
  console.log(`🧠 ORCHESTRATOR ЗАВЕРШИЛ РАБОТУ`);
  console.log(`   Health Score: ${healthScore}%`);
  console.log(`   CRITICAL: ${criticalCount} | HIGH: ${highCount} | OK: ${okCount}`);
  console.log(`   Время: ${elapsed}с`);
  console.log('═'.repeat(60) + '\n');

  return { healthScore, criticalCount, highCount, okCount, allFindings };
}

if (require.main === module) {
  runOrchestrator()
    .then(r => { console.log('Done. Score:', r.healthScore); process.exit(0); })
    .catch(e => { console.error(e); process.exit(1); });
}
module.exports = { runOrchestrator };
