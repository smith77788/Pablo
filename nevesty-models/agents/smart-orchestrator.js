/**
 * 🧠 Smart Orchestrator — живой мозг агентства
 *
 * Не просто запускает агентов и докладывает.
 * ДУМАЕТ: анализирует, обсуждает с агентами, координирует исправления.
 *
 * Процесс:
 * 1. Фаза АНАЛИЗА     — 25 агентов параллельно батчами по 5
 * 2. Фаза ОБСУЖДЕНИЯ  — агенты предлагают стратегии исправления
 * 3. Фаза КОНСЕНСУСА  — ранжируем и выбираем лучший план
 * 4. Фаза ИСПРАВЛЕНИЯ — fix-агенты применяют патчи
 * 5. Фаза ВЕРИФИКАЦИИ — перепроверяем только затронутых агентов
 * 6. Фаза ОТЧЁТА      — Telegram-сводка с обучением
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../.env') });

const path   = require('path');
const sqlite = require('sqlite3').verbose();
const { spawn } = require('child_process');

const {
  logAgent, tgSend, tgSendGetId, tgEditMessage, progressBar,
  dbRun, dbAll, SEV,
} = require('./lib/base');

const DB_PATH = path.join(__dirname, '../data.db');

// ─── All 25 analysis agents ───────────────────────────────────────────────────
const AGENT_CLASSES = [
  require('./01-ux-architect'),        require('./02-booking-completeness'),
  require('./03-model-showcase'),       require('./04-order-lifecycle'),
  require('./05-client-experience'),    require('./06-admin-experience'),
  require('./07-message-threading'),    require('./08-notification-engine'),
  require('./09-security-guard'),       require('./10-keyboard-optimizer'),
  require('./11-db-optimizer'),         require('./12-session-manager'),
  require('./13-input-validator'),      require('./14-markdown-safety'),
  require('./15-error-recovery'),       require('./16-photo-handler'),
  require('./17-search-enhancer'),      require('./18-response-formatter'),
  require('./19-pagination-checker'),   require('./20-state-machine'),
  require('./21-admin-protection'),     require('./22-sql-safety'),
  require('./23-deeplink-handler'),     require('./24-performance-tuner'),
  require('./25-consistency-checker'),
];

const SEV_EMO    = { CRITICAL: '🔴', HIGH: '🟠', MEDIUM: '🟡', LOW: '🟢', INFO: '⚪', OK: '✅' };
const SEV_WEIGHT = { '🔴': 100, '🟠': 50, '🟡': 20, '🟢': 5, '⚪': 1, '✅': 0 };

// Severity levels that trigger Discussion + Fix phases
const ACTION_SEVS = new Set([SEV_EMO.CRITICAL, SEV_EMO.HIGH]);

// ─── Helpers ──────────────────────────────────────────────────────────────────

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

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ─── DB: ensure smart-orchestrator tables exist ───────────────────────────────

async function ensureTables() {
  // Both agent_findings and agent_discussions are created by database.js with canonical schemas.
  // Nothing to do here — kept as hook for future schema migrations.
}

// ─── Fixer loader ─────────────────────────────────────────────────────────────

const FIXER_MAP = {
  'bot.js':      './fixers/fixer-bot',
  'api.js':      './fixers/fixer-api',
  'database.js': './fixers/fixer-db',
  '.html':       './fixers/fixer-frontend',
  '.css':        './fixers/fixer-frontend',
};

function getFixer(fileHint) {
  if (!fileHint) return null;
  for (const [key, mod] of Object.entries(FIXER_MAP)) {
    if (fileHint.includes(key)) {
      try { return require(mod); } catch { return null; }
    }
  }
  return null;
}

// Guess which file a finding likely relates to from its message text
function guessFile(msg = '') {
  const m = msg.toLowerCase();
  if (m.includes('bot.js') || m.includes('callback') || m.includes('deep-link') ||
      m.includes('markdown') || m.includes('telegram')) return 'bot.js';
  if (m.includes('api.js') || m.includes('route') || m.includes('endpoint') ||
      m.includes('rest') || m.includes('http')) return 'api.js';
  if (m.includes('database') || m.includes('sql') || m.includes('query') ||
      m.includes('index') || m.includes('schema')) return 'database.js';
  if (m.includes('.html') || m.includes('.css') || m.includes('frontend') ||
      m.includes('mobile') || m.includes('aria') || m.includes('form')) return 'page.html';
  return null;
}

// ─── Strategy generation ──────────────────────────────────────────────────────

/**
 * Given a finding, produce a list of ranked fix strategies.
 * In a future version this calls an LLM; for now it uses rule-based heuristics.
 */
function proposeFixes(finding) {
  const msg  = finding.msg.toLowerCase();
  const sev  = finding.sev;
  const file = finding.fileHint || guessFile(finding.msg);

  const strategies = [];

  // ── Security patterns ─────────────────────────────────────────────────────
  if (msg.includes('sql injection') || msg.includes('template literal')) {
    strategies.push({
      label: 'parameterize-query',
      description: 'Replace string-concatenated SQL with parameterized placeholders (?)',
      confidence: 0.95,
      effort: 'low',
      file,
    });
  }
  if (msg.includes('xss') || msg.includes('sanitiz')) {
    strategies.push({
      label: 'sanitize-input',
      description: 'Add DOMPurify / escape-html before rendering user content',
      confidence: 0.9,
      effort: 'low',
      file,
    });
  }
  if (msg.includes('secret') || msg.includes('token') || msg.includes('password')) {
    strategies.push({
      label: 'move-to-env',
      description: 'Move hardcoded secret to .env and reference via process.env',
      confidence: 0.98,
      effort: 'low',
      file,
    });
  }

  // ── Performance patterns ──────────────────────────────────────────────────
  if (msg.includes('n+1') || msg.includes('loop') && msg.includes('query')) {
    strategies.push({
      label: 'batch-queries',
      description: 'Replace per-row queries with a single bulk SELECT/INSERT',
      confidence: 0.85,
      effort: 'medium',
      file,
    });
  }
  if (msg.includes('index') || msg.includes('индекс')) {
    strategies.push({
      label: 'add-db-index',
      description: 'CREATE INDEX on frequently queried columns',
      confidence: 0.9,
      effort: 'low',
      file,
    });
  }

  // ── Error handling ────────────────────────────────────────────────────────
  if (msg.includes('error') || msg.includes('exception') || msg.includes('crash')) {
    strategies.push({
      label: 'add-try-catch',
      description: 'Wrap async call in try/catch with proper error propagation',
      confidence: 0.8,
      effort: 'low',
      file,
    });
  }

  // ── Markdown / Telegram ───────────────────────────────────────────────────
  if (msg.includes('markdown') || msg.includes('parse_mode') || msg.includes('escape')) {
    strategies.push({
      label: 'escape-markdown',
      description: 'Apply escapeMarkdown() before all dynamic text in tgSend calls',
      confidence: 0.88,
      effort: 'low',
      file,
    });
  }

  // ── Race condition / concurrency ──────────────────────────────────────────
  if (msg.includes('race') || msg.includes('concurrent') || msg.includes('lock')) {
    strategies.push({
      label: 'add-mutex',
      description: 'Serialize access with async-mutex or SQLite busyTimeout',
      confidence: 0.75,
      effort: 'medium',
      file,
    });
  }

  // ── Fallback generic strategy ─────────────────────────────────────────────
  if (strategies.length === 0) {
    strategies.push({
      label: 'manual-review',
      description: `No automated strategy — manual code review required for: ${finding.msg.slice(0, 80)}`,
      confidence: 0.3,
      effort: 'high',
      file,
    });
  }

  // Sort by confidence descending
  return strategies.sort((a, b) => b.confidence - a.confidence);
}

/**
 * Pick the best strategy (highest confidence that is not manual-review, if possible).
 */
function bestStrategy(strategies) {
  const auto = strategies.filter(s => s.label !== 'manual-review');
  return auto.length > 0 ? auto[0] : strategies[0];
}

// ─── Discussion phase helpers ─────────────────────────────────────────────────

async function logDiscussion(cycleId, finding, strategies, chosen) {
  try {
    const agentName  = finding.agentName || 'Analyst';
    const fixerName  = chosen.file ? `Fixer[${chosen.file}]` : 'FixCoordinator';
    const topic      = `[${finding.sev}] ${finding.msg.slice(0, 80)}`;

    // Agent reports finding to Orchestrator
    await dbRun(
      `INSERT INTO agent_discussions (from_agent, to_agent, topic, message, ref_finding_id)
       VALUES (?, ?, ?, ?, ?)`,
      [
        agentName,
        'Orchestrator',
        topic,
        `Обнаружена проблема (${finding.sev}): ${finding.msg}. ` +
        `Файл: ${finding.fileHint || guessFile(finding.msg) || 'неизвестен'}. ` +
        `Предлагаю ${strategies.length} стратег${strategies.length === 1 ? 'ию' : 'ии'}: ` +
        strategies.map(s => `«${s.label}» (${(s.confidence * 100).toFixed(0)}%)`).join(', ') + '.',
        null,
      ]
    );

    // Orchestrator assigns the best strategy to fixer
    await dbRun(
      `INSERT INTO agent_discussions (from_agent, to_agent, topic, message, ref_finding_id)
       VALUES (?, ?, ?, ?, ?)`,
      [
        'Orchestrator',
        fixerName,
        topic,
        `Принято. Выбираю стратегию «${chosen.label}» ` +
        `(уверенность ${(chosen.confidence * 100).toFixed(0)}%, усилие: ${chosen.effort}). ` +
        `${chosen.description}. Берись за ${chosen.file || 'связанный файл'}.`,
        null,
      ]
    );
  } catch (e) {
    console.warn('[SmartOrch] logDiscussion failed:', e.message);
  }
}

async function logFinding(cycleId, finding, strategy, outcome) {
  try {
    const fixNote = strategy ? `[${strategy.label}] → ${outcome}` : outcome;
    await dbRun(
      `INSERT INTO agent_findings
         (agent_name, severity, message, file, auto_fixable, proposed_fix, status, fixed_by, fix_summary)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        finding.agentName || 'unknown',
        finding.sev,
        finding.msg,
        finding.fileHint || guessFile(finding.msg) || null,
        strategy && strategy.label !== 'manual-review' ? 1 : 0,
        strategy ? strategy.description : null,
        outcome === 'fixed' ? 'fixed' : 'open',
        outcome === 'fixed' ? 'SmartOrchestrator' : null,
        fixNote,
      ]
    );
  } catch (e) {
    console.warn('[SmartOrch] logFinding failed:', e.message);
  }
}

async function reportFixResult(finding, outcome, fixerName) {
  try {
    const agentName = finding.agentName || 'Analyst';
    const topic     = `[${finding.sev}] ${finding.msg.slice(0, 80)}`;
    const resultMsg = outcome === 'fixed'
      ? `Готово — патч применён, проблема устранена.`
      : outcome === 'partial'
        ? `Частично исправлено, требуется дополнительная ревизия.`
        : `Не удалось исправить автоматически — нужен ручной ревью.`;

    await dbRun(
      `INSERT INTO agent_discussions (from_agent, to_agent, topic, message, ref_finding_id)
       VALUES (?, ?, ?, ?, ?)`,
      [fixerName || 'FixCoordinator', agentName, topic, resultMsg, null]
    );
  } catch {}
}

// ─── Known-good cache ─────────────────────────────────────────────────────────

/** Load recent fixed patterns — skip findings we've already handled successfully. */
async function loadKnownGood() {
  try {
    const rows = await dbAll(
      `SELECT finding_msg FROM agent_findings
       WHERE fix_outcome = 'fixed' AND created_at > datetime('now', '-7 days')
       LIMIT 200`
    );
    return new Set(rows.map(r => r.finding_msg));
  } catch { return new Set(); }
}

// ─── PHASE 1 — ANALYSIS ───────────────────────────────────────────────────────

async function analyzePhase(progressRef, buildProgressMsg, totalAgents, BATCH) {
  const allFindings = [];
  const agentMap    = {};  // agentName → AgentClass
  let criticalCount = 0, highCount = 0, mediumCount = 0, okCount = 0;
  const agentSummaries = [];
  let doneCount = 0;

  console.log('\n📊 PHASE 1 — ANALYSIS\n' + '─'.repeat(40));

  for (let i = 0; i < AGENT_CLASSES.length; i += BATCH) {
    const batch      = AGENT_CLASSES.slice(i, i + BATCH);
    const batchNames = batch.map(A => new A().name).join(', ');
    console.log(`  Batch ${Math.floor(i / BATCH) + 1}: ${batchNames}`);

    const results = await Promise.allSettled(batch.map(AgentClass => {
      const agent = new AgentClass();
      agentMap[agent.name] = AgentClass;
      return agent.run({ silent: true }).then(() => agent);
    }));

    for (const r of results) {
      if (r.status !== 'fulfilled') {
        console.error('Agent crashed:', r.reason?.message);
        continue;
      }
      const agent    = r.value;
      const findings = agent.findings || [];

      // Enrich findings with agent metadata + file hint
      const enriched = findings.map(f => ({
        ...f,
        agentName:  agent.name,
        agentEmoji: agent.emoji,
        fileHint:   guessFile(f.msg),
      }));

      allFindings.push(...enriched);

      const crit = findings.filter(f => f.sev === SEV_EMO.CRITICAL).length;
      const high = findings.filter(f => f.sev === SEV_EMO.HIGH).length;
      const med  = findings.filter(f => f.sev === SEV_EMO.MEDIUM).length;
      const ok   = findings.filter(f => f.sev === SEV_EMO.OK).length;
      criticalCount += crit; highCount += high; mediumCount += med; okCount += ok;
      doneCount++;

      if (crit + high + med > 0) {
        agentSummaries.push({
          name: agent.name, emoji: agent.emoji, crit, high, med,
          issues: enriched.filter(f => [SEV_EMO.CRITICAL, SEV_EMO.HIGH, SEV_EMO.MEDIUM].includes(f.sev)),
          AgentClass: agentMap[agent.name],
        });
      }
    }

    // Update live progress bar
    if (progressRef) {
      const nextClass = AGENT_CLASSES[i + BATCH];
      const nextName  = nextClass ? ` → ${new nextClass().name}` : ' ✓';
      await tgEditMessage(
        progressRef.chatId, progressRef.messageId,
        buildProgressMsg(doneCount, nextName, criticalCount, highCount, mediumCount, okCount, 'ANALYSIS')
      );
    }

    if (i + BATCH < AGENT_CLASSES.length) await sleep(400);
  }

  return { allFindings, agentSummaries, agentMap, criticalCount, highCount, mediumCount, okCount };
}

// ─── PHASE 2 — DISCUSSION ─────────────────────────────────────────────────────

/**
 * For each CRITICAL/HIGH finding, run the discussion loop:
 *   1. Propose fix strategies
 *   2. Log to agent_discussions
 *   3. Return the winning strategy
 */
async function discussionPhase(cycleId, allFindings, knownGood) {
  console.log('\n💬 PHASE 2 — DISCUSSION\n' + '─'.repeat(40));

  const actionable = allFindings.filter(f => ACTION_SEVS.has(f.sev) && !knownGood.has(f.msg));
  console.log(`  Actionable findings: ${actionable.length}`);

  const discussionResults = [];

  for (const finding of actionable) {
    const strategies = proposeFixes(finding);
    const chosen     = bestStrategy(strategies);

    console.log(`  💬 [${finding.sev}] ${finding.agentName}: ${finding.msg.slice(0, 60)}...`);
    console.log(`     → Strategy: ${chosen.label} (confidence: ${(chosen.confidence * 100).toFixed(0)}%)`);

    await logDiscussion(cycleId, finding, strategies, chosen);

    discussionResults.push({ finding, strategies, chosen });
  }

  return discussionResults;
}

// ─── PHASE 3 — CONSENSUS ─────────────────────────────────────────────────────

/**
 * Rank discussion results and build a prioritised fix plan.
 * Grouping by file+strategy avoids duplicate fixes.
 */
function consensusPhase(discussionResults) {
  console.log('\n🗳️  PHASE 3 — CONSENSUS\n' + '─'.repeat(40));

  // Group by (fileHint, strategyLabel) to deduplicate
  const grouped = new Map();
  for (const d of discussionResults) {
    const key = `${d.finding.fileHint || 'unknown'}::${d.chosen.label}`;
    if (!grouped.has(key)) {
      grouped.set(key, {
        file:       d.finding.fileHint || guessFile(d.finding.msg),
        strategy:   d.chosen,
        findings:   [],
        totalScore: 0,
      });
    }
    const entry = grouped.get(key);
    entry.findings.push(d.finding);
    // Score = severity weight × confidence
    const sevScore = SEV_WEIGHT[d.finding.sev] || 0;
    entry.totalScore += sevScore * d.chosen.confidence;
  }

  // Sort by totalScore descending
  const fixPlan = [...grouped.values()]
    .sort((a, b) => b.totalScore - a.totalScore);

  fixPlan.forEach((p, idx) => {
    console.log(`  ${idx + 1}. [${p.file || 'unknown'}] ${p.strategy.label} ` +
                `(${p.findings.length} findings, score ${p.totalScore.toFixed(0)})`);
  });

  return fixPlan;
}

// ─── PHASE 4 — FIX ───────────────────────────────────────────────────────────

/**
 * Dispatch fix agents based on the fix plan.
 * Tries to load the appropriate fixer module; falls back to a child_process spawn
 * if the fixer doesn't exist yet (so it's safe to call even without fixer implementations).
 */
async function fixPhase(cycleId, fixPlan) {
  console.log('\n🔧 PHASE 4 — FIX\n' + '─'.repeat(40));

  const fixResults = [];

  for (const plan of fixPlan) {
    const fixer = getFixer(plan.file);

    if (fixer) {
      console.log(`  🔧 Fixer found for ${plan.file}: ${fixer.name}`);

      for (const finding of plan.findings) {
        let outcome = 'failed';
        let resultMsg = '';

        try {
          if (typeof fixer.canFix === 'function' && !fixer.canFix(finding)) {
            outcome   = 'skipped';
            resultMsg = 'canFix() returned false';
          } else if (typeof fixer.fix === 'function') {
            const res  = await fixer.fix(finding);
            outcome    = res?.ok ? 'fixed' : 'failed';
            resultMsg  = res?.msg || '';
          } else {
            outcome   = 'skipped';
            resultMsg = 'fixer has no fix() method';
          }
        } catch (e) {
          outcome   = 'failed';
          resultMsg = e.message;
        }

        console.log(`    ${outcome === 'fixed' ? '✅' : '⚠️'} ${finding.msg.slice(0, 60)} → ${outcome}`);
        await logFinding(cycleId, finding, plan.strategy, outcome);
        await reportFixResult(finding, outcome, fixer.name || 'Fixer');
        fixResults.push({ finding, strategy: plan.strategy, outcome, resultMsg });
      }
    } else {
      // No fixer module — record as pending for manual review
      console.log(`  ⏭  No fixer for ${plan.file || 'unknown'} (${plan.strategy.label}) — queuing for manual`);
      for (const finding of plan.findings) {
        const outcome = plan.strategy.label === 'manual-review' ? 'skipped' : 'pending';
        await logFinding(cycleId, finding, plan.strategy, outcome);
        fixResults.push({ finding, strategy: plan.strategy, outcome, resultMsg: 'no fixer module' });
      }
    }
  }

  const fixed   = fixResults.filter(r => r.outcome === 'fixed').length;
  const pending = fixResults.filter(r => r.outcome === 'pending').length;
  const skipped = fixResults.filter(r => r.outcome === 'skipped').length;
  console.log(`  Fix summary: ✅ ${fixed} fixed, ⏳ ${pending} pending, ⏭ ${skipped} skipped`);

  return fixResults;
}

// ─── PHASE 5 — VERIFICATION ──────────────────────────────────────────────────

/**
 * Re-run only the agents that had CRITICAL/HIGH findings.
 * Compare new findings against old ones — check if they were resolved.
 */
async function verifyPhase(agentSummaries, fixResults) {
  console.log('\n🔍 PHASE 5 — VERIFICATION\n' + '─'.repeat(40));

  // Collect unique AgentClasses that had actionable findings
  const toRecheck = new Map();  // agentName → AgentClass
  for (const summary of agentSummaries) {
    if (summary.crit + summary.high > 0) {
      toRecheck.set(summary.name, summary.AgentClass);
    }
  }

  if (toRecheck.size === 0) {
    console.log('  Nothing to re-check.');
    return { resolved: [], unresolved: [], newFindings: [] };
  }

  console.log(`  Re-checking ${toRecheck.size} agents: ${[...toRecheck.keys()].join(', ')}`);

  // Build set of original actionable finding messages
  const originalMsgs = new Set(
    fixResults.map(r => r.finding.msg)
  );

  const resolved    = [];
  const unresolved  = [];
  const newFindings = [];

  const recheckResults = await Promise.allSettled(
    [...toRecheck.values()].map(AgentClass => {
      const agent = new AgentClass();
      return agent.run({ silent: true }).then(() => agent);
    })
  );

  for (const r of recheckResults) {
    if (r.status !== 'fulfilled') continue;
    const agent = r.value;
    const newMsgs = new Set((agent.findings || []).map(f => f.msg));

    for (const origMsg of originalMsgs) {
      if (agent.findings.some(f => f.agentName === agent.name || true)) {
        // Check if the original finding for THIS agent is still present
        const stillThere = [...newMsgs].some(m => m === origMsg);
        if (stillThere) {
          unresolved.push({ agentName: agent.name, msg: origMsg });
        } else if (originalMsgs.has(origMsg)) {
          resolved.push({ agentName: agent.name, msg: origMsg });
        }
      }
    }

    // Net new problems introduced
    for (const f of (agent.findings || [])) {
      if (!originalMsgs.has(f.msg) && ACTION_SEVS.has(f.sev)) {
        newFindings.push({ ...f, agentName: agent.name });
      }
    }
  }

  console.log(`  ✅ Resolved: ${resolved.length}  ⚠️ Unresolved: ${unresolved.length}  🆕 New: ${newFindings.length}`);
  return { resolved, unresolved, newFindings };
}

// ─── PHASE 6 — REPORT ────────────────────────────────────────────────────────

async function reportPhase({
  cycleId, elapsed, healthScore, icon,
  criticalCount, highCount, mediumCount, okCount,
  agentSummaries, fixResults, verifyResult,
  totalAgents,
}) {
  const ts = new Date().toLocaleString('ru', { timeZone: 'Europe/Moscow' });
  const fixedCount    = fixResults.filter(r => r.outcome === 'fixed').length;
  const pendingCount  = fixResults.filter(r => r.outcome === 'pending').length;
  const resolvedCount = verifyResult.resolved.length;
  const newCount      = verifyResult.newFindings.length;

  const header = [
    `🧠 Smart Orchestrator — ${ts}`,
    `Цикл: ${cycleId}`,
    ``,
    `${icon} Health Score: ${healthScore}%`,
    `🔴 CRITICAL: ${criticalCount}  🟠 HIGH: ${highCount}  🟡 MEDIUM: ${mediumCount}  ✅ OK: ${okCount}`,
    `⏱ ${elapsed}с | ${totalAgents} агентов`,
    ``,
    `🔧 Исправлено автоматически: ${fixedCount}`,
    `⏳ Ожидает ручной правки: ${pendingCount}`,
    `✅ Подтверждено решённых: ${resolvedCount}`,
    newCount > 0 ? `🆕 Новых проблем после фикса: ${newCount}` : '',
    ``,
  ].filter(l => l !== '').join('\n');

  let details = '';
  if (agentSummaries.length === 0) {
    details = '✅ Всё в порядке — проблем не найдено!\n';
  } else {
    // Group fix results by finding for inline annotation
    const fixOutcomeByMsg = new Map(fixResults.map(r => [r.finding.msg, r.outcome]));

    for (const a of agentSummaries) {
      details += `\n${a.emoji} ${a.name}`;
      if (a.crit) details += ` 🔴${a.crit}`;
      if (a.high) details += ` 🟠${a.high}`;
      if (a.med)  details += ` 🟡${a.med}`;
      details += '\n';
      for (const f of a.issues) {
        const outcome = fixOutcomeByMsg.get(f.msg);
        const tag     = outcome === 'fixed'   ? ' ✅ fixed'
                      : outcome === 'pending' ? ' ⏳ pending'
                      : outcome === 'skipped' ? ' ⏭ manual'
                      : '';
        details += `  ${f.sev} ${f.msg}${tag}\n`;
      }
    }
  }

  if (verifyResult.newFindings.length > 0) {
    details += '\n🆕 Новые проблемы после исправлений:\n';
    verifyResult.newFindings.forEach(f => {
      details += `  ${f.sev} [${f.agentName}] ${f.msg}\n`;
    });
  }

  const keyboard = {
    inline_keyboard: [
      [{ text: '🧠 Smart Re-run',             callback_data: 'adm_smart_run'    }],
      [{ text: '🔧 Исправить всё и перепроверить', callback_data: 'adm_fix_organism' }],
      [{ text: '🔄 Обычная проверка',          callback_data: 'adm_run_organism' },
       { text: '📡 Фид агентов',               callback_data: 'agent_feed_0'    }],
    ],
  };

  const fullMsg = header + details;
  const chunks  = splitMsg(fullMsg, 4000);
  await tgSend(chunks[0], { reply_markup: keyboard });
  for (let i = 1; i < chunks.length; i++) {
    await tgSend(`📋 Продолжение (${i + 1}/${chunks.length}):\n` + chunks[i]);
  }

  console.log(`\n🧠 SmartOrch Done: Score=${healthScore}% 🔴${criticalCount} 🟠${highCount} 🟡${mediumCount} ✅${okCount} (${elapsed}с)\n`);
}

// ─── MAIN ─────────────────────────────────────────────────────────────────────

async function runSmartOrchestrator() {
  const startTime = Date.now();
  const cycleId   = `cycle-${Date.now()}`;
  const BATCH     = 5;
  const totalAgents = AGENT_CLASSES.length;
  const totalBatches = Math.ceil(totalAgents / BATCH);

  console.log('\n🧠 SMART ORCHESTRATOR — думаем вместе...\n');

  // ── Ensure DB tables ──────────────────────────────────────────────────────
  await ensureTables();

  // ── Load known-good cache ─────────────────────────────────────────────────
  const knownGood = await loadKnownGood();
  console.log(`📚 Known-good patterns loaded: ${knownGood.size}`);

  // ── Live progress bar helper ──────────────────────────────────────────────
  const buildProgressMsg = (done, cur, crit, high, med, ok, phase = 'ANALYSIS') => {
    const pct      = Math.round((done / totalAgents) * 100);
    const bar      = progressBar(done, totalAgents, 20);
    const batchNum = Math.min(Math.ceil(done / BATCH) + 1, totalBatches);
    return [
      `🧠 Smart Orchestrator — фаза ${phase}`,
      ``,
      `[${bar}] ${pct}%`,
      `Батч ${batchNum}/${totalBatches}${cur ? ` · ${cur}` : ''}`,
      `Агентов: ${done}/${totalAgents}`,
      ``,
      `🔴 ${crit}  🟠 ${high}  🟡 ${med}  ✅ ${ok}`,
    ].join('\n');
  };

  const progressRef = await tgSendGetId(buildProgressMsg(0, '...', 0, 0, 0, 0));

  // ── PHASE 1: Analysis ─────────────────────────────────────────────────────
  const {
    allFindings, agentSummaries, agentMap,
    criticalCount, highCount, mediumCount, okCount,
  } = await analyzePhase(progressRef, buildProgressMsg, totalAgents, BATCH);

  const elapsed1 = ((Date.now() - startTime) / 1000).toFixed(1);

  // Update progress bar to show Analysis complete
  if (progressRef) {
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      `🧠 Smart Orchestrator\n\n` +
      `✅ Анализ завершён за ${elapsed1}с\n` +
      `🔴 ${criticalCount}  🟠 ${highCount}  🟡 ${mediumCount}  ✅ ${okCount}\n\n` +
      `💬 Запускаю фазу обсуждения...`
    );
  }

  await logAgent('SmartOrchestrator',
    `🧠 Анализ: ${totalAgents} агентов, 🔴${criticalCount} 🟠${highCount} 🟡${mediumCount} ✅${okCount}, ${elapsed1}с`
  );

  // ── PHASE 2: Discussion ───────────────────────────────────────────────────
  const discussionResults = await discussionPhase(cycleId, allFindings, knownGood);

  if (progressRef) {
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      `🧠 Smart Orchestrator\n\n` +
      `✅ Обсуждение: ${discussionResults.length} стратегий выработано\n\n` +
      `🗳️ Определяю консенсус...`
    );
  }

  // ── PHASE 3: Consensus ────────────────────────────────────────────────────
  const fixPlan = consensusPhase(discussionResults);

  if (progressRef) {
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      `🧠 Smart Orchestrator\n\n` +
      `✅ Консенсус: ${fixPlan.length} групп для исправления\n\n` +
      `🔧 Применяю исправления...`
    );
  }

  // ── PHASE 4: Fix ──────────────────────────────────────────────────────────
  const fixResults = await fixPhase(cycleId, fixPlan);

  if (progressRef) {
    const fixedCount = fixResults.filter(r => r.outcome === 'fixed').length;
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      `🧠 Smart Orchestrator\n\n` +
      `✅ Исправлено: ${fixedCount} из ${fixResults.length}\n\n` +
      `🔍 Верификация...`
    );
  }

  // ── PHASE 5: Verification ─────────────────────────────────────────────────
  const verifyResult = await verifyPhase(agentSummaries, fixResults);

  // ── Update known-good after verification ──────────────────────────────────
  for (const r of verifyResult.resolved) {
    try {
      await dbRun(
        `UPDATE agent_findings SET fix_outcome = 'fixed', verified_at = CURRENT_TIMESTAMP
         WHERE finding_msg = ? AND fix_outcome = 'pending'`,
        [r.msg]
      );
    } catch {}
  }

  // ── Compute final health score ─────────────────────────────────────────────
  const elapsed     = ((Date.now() - startTime) / 1000).toFixed(1);
  const totalWeight = allFindings.reduce((s, f) => s + (SEV_WEIGHT[f.sev] || 0), 0);
  const maxWeight   = allFindings.length * 100;
  const healthScore = maxWeight > 0 ? Math.max(0, Math.round((1 - totalWeight / maxWeight) * 100)) : 100;
  const icon        = healthScore >= 80 ? '💚' : healthScore >= 60 ? '🟡' : '🔴';

  // Update final progress bar
  if (progressRef) {
    await tgEditMessage(progressRef.chatId, progressRef.messageId,
      `🧠 Smart Orchestrator завершён ${icon}\n\n` +
      `[████████████████████] 100%\n` +
      `Все ${totalAgents} агентов + 5 фаз за ${elapsed}с\n\n` +
      `🔴 ${criticalCount}  🟠 ${highCount}  🟡 ${mediumCount}  ✅ ${okCount}\n` +
      `Health Score: ${healthScore}%`
    );
  }

  await logAgent('SmartOrchestrator',
    `🧠 Цикл ${cycleId} завершён: Score=${healthScore}%, ` +
    `fixed=${fixResults.filter(r => r.outcome === 'fixed').length}, ` +
    `resolved=${verifyResult.resolved.length}, new=${verifyResult.newFindings.length}, ${elapsed}с`
  );

  // ── PHASE 6: Report ───────────────────────────────────────────────────────
  await reportPhase({
    cycleId, elapsed, healthScore, icon,
    criticalCount, highCount, mediumCount, okCount,
    agentSummaries, fixResults, verifyResult,
    totalAgents,
  });

  return {
    cycleId, healthScore, criticalCount, highCount, mediumCount, okCount,
    allFindings, agentSummaries, fixResults, verifyResult,
  };
}

// ─── CLI entry point ──────────────────────────────────────────────────────────

if (require.main === module) {
  runSmartOrchestrator()
    .then(r => {
      console.log(`Score: ${r.healthScore}%  Cycle: ${r.cycleId}`);
      process.exit(0);
    })
    .catch(e => {
      console.error('CRASH:', e);
      tgSend(`🚨 Smart Orchestrator crashed: ${e.message}`)
        .finally(() => process.exit(1));
    });
}

module.exports = { runSmartOrchestrator };
