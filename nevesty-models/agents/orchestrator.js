/** 🧠 Orchestrator — Prefrontal Cortex | Главный мозг: агрегирует, решает, подтверждает */
const { logAgent, tgSend, tgSendGetId, tgEditMessage, progressBar, dbAll, dbRun } = require('./lib/base');

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
  const totalAgents = agents.length;
  const BATCH = 5;
  const totalBatches = Math.ceil(totalAgents / BATCH);

  // Отправляем начальный прогресс-бар (редактируем одно сообщение)
  const buildProgressMsg = (done, cur, crit, high, med, ok) => {
    const pct = Math.round((done / totalAgents) * 100);
    const bar = progressBar(done, totalAgents, 20);
    const batchNum = Math.min(Math.ceil(done / BATCH) + 1, totalBatches);
    return [
      `🧠 Проверка организма...`,
      ``,
      `[${bar}] ${pct}%`,
      `Батч ${batchNum}/${totalBatches}${cur ? ` · ${cur}` : ''}`,
      `Агентов: ${done}/${totalAgents}`,
      ``,
      `🔴 ${crit}  🟠 ${high}  🟡 ${med}  ✅ ${ok}`,
    ].join('\n');
  };

  const progressRef = await tgSendGetId(buildProgressMsg(0, '...', 0, 0, 0, 0));
  let doneCount = 0;

  // Запускаем агентов параллельно батчами по 5 — быстро и без перегрузки DB
  for (let i = 0; i < agents.length; i += BATCH) {
    const batch = agents.slice(i, i + BATCH);
    const batchNames = batch.map(A => new A().name).join(', ');
    console.log(`  Батч ${Math.floor(i/BATCH)+1}: ${batchNames}`);

    const results = await Promise.allSettled(batch.map(AgentClass => {
      const agent = new AgentClass();
      return agent.run({ silent: true }).then(() => agent);
    }));

    for (const r of results) {
      if (r.status !== 'fulfilled') { console.error('Agent crashed:', r.reason?.message); continue; }
      const agent = r.value;
      const findings = agent.findings || [];
      allFindings.push(...findings.map(f => ({ ...f, agentName: agent.name, agentEmoji: agent.emoji })));
      const crit = findings.filter(f => f.sev === SEV_EMO.CRITICAL).length;
      const high = findings.filter(f => f.sev === SEV_EMO.HIGH).length;
      const med  = findings.filter(f => f.sev === SEV_EMO.MEDIUM).length;
      const ok   = findings.filter(f => f.sev === SEV_EMO.OK).length;
      criticalCount += crit; highCount += high; mediumCount += med; okCount += ok;
      doneCount++;
      if (crit + high + med > 0) {
        agentSummaries.push({ name: agent.name, emoji: agent.emoji, crit, high, med,
          issues: findings.filter(f => [SEV_EMO.CRITICAL, SEV_EMO.HIGH, SEV_EMO.MEDIUM].includes(f.sev)) });
      }
    }

    // Обновляем прогресс-бар после каждого батча
    if (progressRef) {
      const nextBatch = agents[i + BATCH];
      const nextName = nextBatch ? ` → ${new nextBatch().name}` : ' ✓';
      await tgEditMessage(progressRef.chatId, progressRef.messageId,
        buildProgressMsg(doneCount, nextName, criticalCount, highCount, mediumCount, okCount));
    }

    // Небольшая пауза между батчами для плавности
    if (i + BATCH < agents.length) await new Promise(r => setTimeout(r, 400));
  }

  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
  const totalWeight = allFindings.reduce((s, f) => s + (SEV_WEIGHT[f.sev] || 0), 0);
  const maxWeight   = allFindings.length * 100;
  const healthScore = maxWeight > 0 ? Math.max(0, Math.round((1 - totalWeight / maxWeight) * 100)) : 100;
  const icon = healthScore >= 80 ? '💚' : healthScore >= 60 ? '🟡' : '🔴';

  // Обновляем прогресс-бар: финальное состояние
  if (progressRef) {
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      `🧠 Проверка завершена ${icon}\n\n[████████████████████] 100%\nВсе ${totalAgents} агентов проверены за ${elapsed}с\n\n🔴 ${criticalCount}  🟠 ${highCount}  🟡 ${mediumCount}  ✅ ${okCount}\nHealth Score: ${healthScore}%`
    );
  }

  await logAgent('Orchestrator',
    `🧠 Проверка завершена: ${agents.length} агентов, ` +
    `🔴${criticalCount} 🟠${highCount} 🟡${mediumCount} ✅${okCount}, Health Score: ${healthScore}%`
  );

  // ── Отчёт (plain text — никогда не ломается Markdown-парсером) ─────────
  const ts = new Date().toLocaleString('ru', { timeZone: 'Europe/Moscow' });
  const header = [
    `🧠 Отчёт организма — ${ts}`,
    ``,
    `${icon} Health Score: ${healthScore}%`,
    `🔴 CRITICAL: ${criticalCount}  🟠 HIGH: ${highCount}  🟡 MEDIUM: ${mediumCount}  ✅ OK: ${okCount}`,
    `⏱ ${elapsed}с | ${agents.length} агентов`,
    ``,
  ].join('\n');

  let details = '';
  if (agentSummaries.length === 0) {
    details = '✅ Всё в порядке — проблем не найдено!\n';
  } else {
    for (const a of agentSummaries) {
      details += `\n${a.emoji} ${a.name}`;
      if (a.crit) details += ` 🔴${a.crit}`;
      if (a.high) details += ` 🟠${a.high}`;
      if (a.med)  details += ` 🟡${a.med}`;
      details += '\n';
      a.issues.forEach(f => { details += `  ${f.sev} ${f.msg}\n`; });
    }
  }

  const keyboard = {
    inline_keyboard: [
      [{ text: '🔧 Исправить всё и перепроверить', callback_data: 'adm_fix_organism' }],
      [{ text: '🔄 Перепроверить', callback_data: 'adm_run_organism' },
       { text: '📡 Фид агентов',   callback_data: 'agent_feed_0'     }],
    ]
  };

  // Отправляем без parse_mode — plain text всегда доходит
  const fullMsg = header + details;
  const chunks = splitMsg(fullMsg, 4000);
  await tgSend(chunks[0], { reply_markup: keyboard });
  for (let i = 1; i < chunks.length; i++) {
    await tgSend(`📋 Продолжение (${i+1}/${chunks.length}):\n` + chunks[i]);
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
