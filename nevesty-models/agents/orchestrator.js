/** 🧠 Orchestrator — Prefrontal Cortex | Главный мозг: агрегирует, решает, подтверждает */
const { logAgent, tgSend, dbAll, dbRun } = require('./lib/base');

const agents = [
  require('./01-ux-architect'),       require('./02-booking-completeness'),
  require('./03-model-showcase'),      require('./04-order-lifecycle'),
  require('./05-client-experience'),   require('./06-admin-experience'),
  require('./07-message-threading'),   require('./08-notification-engine'),
  require('./09-security-guard'),      require('./10-keyboard-optimizer'),
  require('./11-db-optimizer'),        require('./12-session-manager'),
  require('./13-input-validator'),     require('./14-markdown-safety'),
  require('./15-error-recovery'),      require('./16-photo-handler'),
  require('./17-search-enhancer'),     require('./18-response-formatter'),
  require('./19-pagination-checker'),  require('./20-state-machine'),
  require('./21-admin-protection'),    require('./22-sql-safety'),
  require('./23-deeplink-handler'),    require('./24-performance-tuner'),
  require('./25-consistency-checker'),
];

const SEV_EMO = { CRITICAL:'🔴', HIGH:'🟠', MEDIUM:'🟡', LOW:'🟢', INFO:'⚪', OK:'✅' };
const SEV_WEIGHT = { '🔴':100, '🟠':50, '🟡':20, '🟢':5, '⚪':1, '✅':0 };

// Split long message into chunks ≤4096 chars for Telegram
function splitMsg(text, limit = 4000) {
  if (text.length <= limit) return [text];
  const chunks = [];
  let cur = '';
  for (const line of text.split('\n')) {
    if ((cur + '\n' + line).length > limit) { chunks.push(cur); cur = line; }
    else { cur = cur ? cur + '\n' + line : line; }
  }
  if (cur) chunks.push(cur);
  return chunks;
}

async function runOrchestrator() {
  const startTime = Date.now();
  console.log('\n🧠 ORCHESTRATOR — полная проверка организма...\n');

  const allFindings = [];
  let criticalCount = 0, highCount = 0, mediumCount = 0, okCount = 0;
  const agentSummaries = [];

  // Запускаем агентов ТИХО — они не шлют индивидуальные TG-уведомления
  for (const AgentClass of agents) {
    const agent = new AgentClass();
    try {
      await agent.run({ silent: true });
      const findings = agent.findings || [];
      allFindings.push(...findings.map(f => ({ ...f, agentName: agent.name, agentEmoji: agent.emoji })));
      const crit = findings.filter(f => f.sev === SEV_EMO.CRITICAL).length;
      const high = findings.filter(f => f.sev === SEV_EMO.HIGH).length;
      const med  = findings.filter(f => f.sev === SEV_EMO.MEDIUM).length;
      const ok   = findings.filter(f => f.sev === SEV_EMO.OK).length;
      criticalCount += crit; highCount += high; mediumCount += med; okCount += ok;
      if (crit + high + med > 0) {
        agentSummaries.push({ name: agent.name, emoji: agent.emoji, crit, high, med,
          issues: findings.filter(f => [SEV_EMO.CRITICAL, SEV_EMO.HIGH, SEV_EMO.MEDIUM].includes(f.sev)) });
      }
    } catch (err) {
      console.error(`❌ ${agent.name} crashed:`, err.message);
    }
  }

  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
  const totalWeight = allFindings.reduce((s, f) => s + (SEV_WEIGHT[f.sev] || 0), 0);
  const maxWeight   = allFindings.length * 100;
  const healthScore = maxWeight > 0 ? Math.max(0, Math.round((1 - totalWeight / maxWeight) * 100)) : 100;
  const icon = healthScore >= 80 ? '💚' : healthScore >= 60 ? '🟡' : '🔴';

  await logAgent('Orchestrator',
    `🧠 Проверка завершена: ${agents.length} агентов, ` +
    `🔴${criticalCount} 🟠${highCount} 🟡${mediumCount} ✅${okCount}, Health Score: ${healthScore}%`
  );

  // ── Заголовок отчёта ────────────────────────────────────────────────────
  const header = [
    `🧠 *Отчёт организма — ${new Date().toLocaleString('ru')}*`,
    ``,
    `${icon} *Health Score: ${healthScore}%*`,
    `🔴 CRITICAL: ${criticalCount}  🟠 HIGH: ${highCount}  🟡 MEDIUM: ${mediumCount}  ✅ OK: ${okCount}`,
    `⏱ ${elapsed}с | ${agents.length} агентов`,
    ``,
  ].join('\n');

  // ── Детальный список проблем по агентам ────────────────────────────────
  let details = '';
  if (agentSummaries.length === 0) {
    details = '✅ *Всё в порядке — проблем не найдено!*\n';
  } else {
    for (const a of agentSummaries) {
      details += `\n${a.emoji} *${a.name}*`;
      if (a.crit) details += ` 🔴${a.crit}`;
      if (a.high) details += ` 🟠${a.high}`;
      if (a.med)  details += ` 🟡${a.med}`;
      details += '\n';
      a.issues.forEach(f => { details += `${f.sev} ${f.msg}\n`; });
    }
  }

  const keyboard = {
    inline_keyboard: [
      [{ text: '🔧 Исправить всё и перепроверить', callback_data: 'adm_fix_organism' }],
      [{ text: '🔄 Перепроверить', callback_data: 'adm_run_organism' },
       { text: '📡 Фид агентов',   callback_data: 'agent_feed_0'     }],
    ]
  };

  // Отправляем заголовок + кнопки
  await tgSend(header + details.slice(0, 3500), { parse_mode: 'Markdown', reply_markup: keyboard });

  // Если детали длинные — шлём продолжение отдельными сообщениями
  if (details.length > 3500) {
    const rest = splitMsg(details.slice(3500));
    for (const chunk of rest) {
      await tgSend(`📋 *Продолжение отчёта:*\n${chunk}`, { parse_mode: 'Markdown' });
    }
  }

  console.log(`\n🧠 Готово: Score=${healthScore}% 🔴${criticalCount} 🟠${highCount} 🟡${mediumCount} ✅${okCount} (${elapsed}с)\n`);

  return { healthScore, criticalCount, highCount, mediumCount, okCount, allFindings, agentSummaries };
}

if (require.main === module) {
  runOrchestrator()
    .then(r => { console.log('Score:', r.healthScore); process.exit(0); })
    .catch(e => { console.error(e); process.exit(1); });
}
module.exports = { runOrchestrator };
