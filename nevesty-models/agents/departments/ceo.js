/**
 * 🏛️ CEO Core — Strategic Intelligence (БЛОК 5.3)
 *
 * Analyzes department reports from all agents over the last cycle
 * and produces one strategic decision per run.
 *
 * Weekly (on Mondays) it also generates a full weekly report
 * saved to bot_settings['ceo_weekly_report'].
 *
 * Decisions are saved to:
 *   • bot_settings['ceo_last_decision']  — in data.db
 *   • factory.db → ceo_decisions         — for Factory dashboard
 *   • factory.db → growth_actions        — actionable tasks
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

const path = require('path');
const sqlite = require('sqlite3').verbose();

const { Agent, dbRun, dbGet, dbAll, logAgent } = require('../lib/base');

// ─── Paths ────────────────────────────────────────────────────────────────────
const FACTORY_DB_PATH = path.join(__dirname, '../../../factory/factory.db');

// ─── Claude API helper (haiku-4-5) ───────────────────────────────────────────
async function callClaude({ systemPrompt, userPrompt, maxTokens = 800 }) {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) throw new Error('ANTHROPIC_API_KEY not configured');

  const response = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: maxTokens,
      system: systemPrompt,
      messages: [{ role: 'user', content: userPrompt }],
    }),
  });

  if (!response.ok) {
    const err = await response.text();
    throw new Error(`Claude API error ${response.status}: ${err.slice(0, 200)}`);
  }

  const data = await response.json();
  return data.content?.[0]?.text ?? '';
}

// ─── Factory DB helpers (separate file) ───────────────────────────────────────

function factoryDbRun(sql, params = []) {
  return new Promise((res, rej) => {
    const db = new sqlite.Database(FACTORY_DB_PATH, sqlite.OPEN_READWRITE | sqlite.OPEN_CREATE, err => {
      if (err) return rej(err);
      db.configure('busyTimeout', 5000);
      db.run(sql, params, function (e) {
        db.close();
        e ? rej(e) : res({ id: this.lastID, changes: this.changes });
      });
    });
  });
}

function factoryDbGet(sql, params = []) {
  return new Promise((res, rej) => {
    const db = new sqlite.Database(FACTORY_DB_PATH, sqlite.OPEN_READONLY, err => {
      if (err) return rej(err);
      db.configure('busyTimeout', 5000);
      db.get(sql, params, (e, row) => {
        db.close();
        e ? rej(e) : res(row);
      });
    });
  });
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

async function factoryLog(name, message) {
  console.log(`[${name}] ${message}`);
  await logAgent(name, message);
}

/** Write a key/value pair into bot_settings in data.db. */
async function saveSetting(key, value) {
  await dbRun(
    `INSERT OR REPLACE INTO bot_settings (key, value, updated_at)
     VALUES (?, ?, CURRENT_TIMESTAMP)`,
    [key, typeof value === 'string' ? value : JSON.stringify(value)]
  );
}

/** Returns true if today is Monday (server local time). */
function isMonday() {
  return new Date().getDay() === 1;
}

// ═════════════════════════════════════════════════════════════════════════════
// StrategicCEO Agent
// ═════════════════════════════════════════════════════════════════════════════

class StrategicCEO extends Agent {
  constructor() {
    super({
      id: 'ceo-01',
      name: 'StrategicCEO',
      organ: 'C-Suite',
      emoji: '🏛️',
      focus: 'Strategic analysis of all department findings — one decision per cycle',
    });
  }

  // ── 1. Load recent agent findings from data.db ──────────────────────────────
  async loadRecentFindings() {
    try {
      return await dbAll(`
        SELECT agent_name, severity, message, file, status, created_at
        FROM agent_findings
        WHERE created_at > datetime('now', '-24 hours')
        ORDER BY created_at DESC
        LIMIT 50
      `);
    } catch (e) {
      await factoryLog(this.name, `loadRecentFindings error: ${e.message}`);
      return [];
    }
  }

  // ── 2. Load operational metrics from data.db ────────────────────────────────
  async loadMetrics() {
    const metrics = {};

    try {
      // Orders
      const ordersTotal = await dbGet(`SELECT COUNT(*) as n FROM orders`);
      const ordersNew = await dbGet(`SELECT COUNT(*) as n FROM orders WHERE status='new'`);
      const ordersWeek = await dbGet(`SELECT COUNT(*) as n FROM orders WHERE created_at > datetime('now','-7 days')`);
      metrics.orders = {
        total: ordersTotal?.n ?? 0,
        new: ordersNew?.n ?? 0,
        lastWeek: ordersWeek?.n ?? 0,
      };
    } catch {}

    try {
      // Models
      const modelsTotal = await dbGet(`SELECT COUNT(*) as n FROM models`);
      const modelsAvail = await dbGet(`SELECT COUNT(*) as n FROM models WHERE available=1`);
      metrics.models = {
        total: modelsTotal?.n ?? 0,
        available: modelsAvail?.n ?? 0,
      };
    } catch {}

    try {
      // Reviews
      const reviewsAvg = await dbGet(`SELECT ROUND(AVG(rating),2) as avg, COUNT(*) as n FROM reviews WHERE approved=1`);
      metrics.reviews = { avg: reviewsAvg?.avg ?? null, count: reviewsAvg?.n ?? 0 };
    } catch {}

    try {
      // Agent health last 24h
      const critCount = await dbGet(
        `SELECT COUNT(*) as n FROM agent_findings WHERE severity='🔴' AND created_at > datetime('now','-24 hours')`
      );
      const highCount = await dbGet(
        `SELECT COUNT(*) as n FROM agent_findings WHERE severity='🟠' AND created_at > datetime('now','-24 hours')`
      );
      metrics.agentHealth = {
        critical: critCount?.n ?? 0,
        high: highCount?.n ?? 0,
      };
    } catch {}

    try {
      // Factory: last cycle health score
      const lastCycle = await factoryDbGet(
        `SELECT health_score, phase, finished_at FROM cycles ORDER BY started_at DESC LIMIT 1`
      );
      if (lastCycle) metrics.lastFactoryCycle = lastCycle;
    } catch {}

    try {
      // Factory: pending growth actions
      const pending = await factoryDbGet(`SELECT COUNT(*) as n FROM growth_actions WHERE status='pending'`);
      metrics.pendingGrowthActions = pending?.n ?? 0;
    } catch {}

    return metrics;
  }

  // ── 3. Load recent agent discussions for context ───────────────────────────
  async loadDiscussions() {
    try {
      return await dbAll(`
        SELECT from_agent, to_agent, topic, message, created_at
        FROM agent_discussions
        WHERE created_at > datetime('now', '-24 hours')
        ORDER BY created_at DESC
        LIMIT 30
      `);
    } catch {
      return [];
    }
  }

  // ── 4. Make strategic decision via Claude ──────────────────────────────────
  async makeStrategicDecision(findings, metrics, discussions) {
    const systemPrompt = [
      'Ты — CEO модельного агентства Nevesty Models.',
      'Анализируй данные мониторинга, метрики и отчёты команды.',
      'Дай ОДНО конкретное стратегическое решение на следующие 24 часа.',
      'Формат ответа (строго JSON):',
      '{',
      '  "decision": "Краткое решение в 1-2 предложениях",',
      '  "rationale": "Обоснование на основе данных (2-3 предложения)",',
      '  "department_focus": "sales | creative | ops | tech | all",',
      '  "priority_action": "Конкретное первое действие",',
      '  "expected_impact": "Ожидаемый результат через 24 часа",',
      '  "growth_action": {',
      '    "action_type": "тип действия (seo|social|sales|tech|ops)",',
      '    "channel": "канал (telegram|site|email|all)",',
      '    "description": "Что именно нужно сделать"',
      '  }',
      '}',
    ].join('\n');

    const criticalFindings = findings.filter(f => f.severity === '🔴' || f.severity === '🟠');
    const findingsSummary = criticalFindings.length
      ? criticalFindings
          .slice(0, 15)
          .map(f => `[${f.severity}] ${f.agent_name}: ${f.message}`)
          .join('\n')
      : 'Критических проблем не обнаружено.';

    const discussionsSummary = discussions.length
      ? discussions
          .slice(0, 10)
          .map(d => `${d.from_agent} → ${d.to_agent}: ${d.topic}`)
          .join('\n')
      : 'Нет активных обсуждений.';

    const userPrompt = [
      '=== ОТЧЁТЫ АГЕНТОВ (последние 24 часа) ===',
      findingsSummary,
      '',
      '=== АКТИВНЫЕ ОБСУЖДЕНИЯ ===',
      discussionsSummary,
      '',
      '=== ОПЕРАЦИОННЫЕ МЕТРИКИ ===',
      JSON.stringify(metrics, null, 2),
    ].join('\n');

    const raw = await callClaude({ systemPrompt, userPrompt, maxTokens: 600 });

    // Parse JSON from response (Claude sometimes wraps it in ```json blocks)
    const jsonMatch = raw.match(/\{[\s\S]*\}/);
    if (!jsonMatch) throw new Error(`CEO: Claude returned non-JSON: ${raw.slice(0, 100)}`);
    return JSON.parse(jsonMatch[0]);
  }

  // ── 5. Save decision to DB tables ──────────────────────────────────────────
  async saveDecision(decision, metrics) {
    const now = new Date().toISOString();
    const decText = `${decision.decision}\n\nОснование: ${decision.rationale}\n\nПриоритетное действие: ${decision.priority_action}`;
    const cycleId = `ceo-${now.slice(0, 16)}`;

    // bot_settings in data.db
    const settingValue = JSON.stringify({
      decision: decision.decision,
      rationale: decision.rationale,
      department_focus: decision.department_focus,
      priority_action: decision.priority_action,
      expected_impact: decision.expected_impact,
      created_at: now,
    });
    await saveSetting('ceo_last_decision', settingValue);

    // agent_discussions — visible in admin dashboard
    await dbRun(`INSERT INTO agent_discussions (from_agent, to_agent, topic, message) VALUES (?,?,?,?)`, [
      this.name,
      'all',
      `🏛️ CEO Decision — ${now.slice(0, 10)}`,
      `🏛️ *CEO стратегическое решение*\n\n${decText}\n\nОжидаемый результат: ${decision.expected_impact}`,
    ]).catch(() => {});

    // ceo_decisions in factory.db
    try {
      await factoryDbRun(
        `INSERT INTO ceo_decisions
           (cycle_id, decision_text, health_score, departments_active, weekly_focus, department_focus, created_at)
         VALUES (?,?,?,?,?,?,?)`,
        [
          cycleId,
          decText,
          metrics.agentHealth
            ? Math.max(0, 100 - metrics.agentHealth.critical * 20 - metrics.agentHealth.high * 5)
            : null,
          JSON.stringify(['sales', 'creative', 'ops', 'tech']),
          decision.decision,
          decision.department_focus,
          now,
        ]
      );
    } catch (e) {
      await factoryLog(this.name, `ceo_decisions insert warning: ${e.message}`);
    }

    // growth_actions in factory.db
    if (decision.growth_action) {
      const ga = decision.growth_action;
      try {
        await factoryDbRun(
          `INSERT INTO growth_actions
             (action_type, channel, description, status, priority, created_at)
           VALUES (?,?,?,?,?,?)`,
          [
            ga.action_type || 'ops',
            ga.channel || 'all',
            ga.description || decision.priority_action,
            'pending',
            8, // CEO decisions get high priority
            now,
          ]
        );
      } catch (e) {
        await factoryLog(this.name, `growth_actions insert warning: ${e.message}`);
      }
    }

    await factoryLog(
      this.name,
      `Decision saved: ${decision.decision.slice(0, 100)} [focus: ${decision.department_focus}]`
    );
  }

  // ── 6. Generate weekly report (Mondays only) ────────────────────────────────
  async generateWeeklyReport(findings, metrics) {
    const systemPrompt = [
      'Ты — CEO модельного агентства Nevesty Models.',
      'Напиши недельный стратегический отчёт на русском языке.',
      'Структура (строго JSON):',
      '{',
      '  "period": "Неделя X — дата",',
      '  "headline": "Главный итог недели (1 предложение)",',
      '  "wins": ["Достижение 1", "Достижение 2"],',
      '  "risks": ["Риск 1", "Риск 2"],',
      '  "kpi_summary": "Краткий обзор KPI",',
      '  "next_week_focus": "Главный фокус на следующую неделю",',
      '  "department_scores": {',
      '    "sales": 0-10,',
      '    "tech": 0-10,',
      '    "ops": 0-10',
      '  }',
      '}',
    ].join('\n');

    const weekFindingsSummary =
      findings
        .slice(0, 20)
        .map(f => `[${f.severity}] ${f.agent_name}: ${f.message}`)
        .join('\n') || 'Проблем не зафиксировано.';

    const userPrompt = [
      '=== СОБЫТИЯ НЕДЕЛИ (agent_findings, последние 24ч как пример) ===',
      weekFindingsSummary,
      '',
      '=== МЕТРИКИ ===',
      JSON.stringify(metrics, null, 2),
    ].join('\n');

    const raw = await callClaude({ systemPrompt, userPrompt, maxTokens: 700 });

    const jsonMatch = raw.match(/\{[\s\S]*\}/);
    if (!jsonMatch) throw new Error(`CEO weekly: non-JSON response: ${raw.slice(0, 100)}`);
    return JSON.parse(jsonMatch[0]);
  }

  // ── Main analyze() lifecycle ───────────────────────────────────────────────
  async analyze() {
    // 1. Gather data in parallel
    const [findings, metrics, discussions] = await Promise.all([
      this.loadRecentFindings(),
      this.loadMetrics(),
      this.loadDiscussions(),
    ]);

    await factoryLog(
      this.name,
      `Loaded: ${findings.length} findings, discussions: ${discussions.length}, metrics: ${JSON.stringify(metrics).slice(0, 120)}`
    );

    // 2. Make strategic decision
    let decision;
    try {
      decision = await this.makeStrategicDecision(findings, metrics, discussions);
      await this.saveDecision(decision, metrics);

      this.addFinding('INFO', `🏛️ CEO Decision: ${decision.decision.slice(0, 120)}`);
      this.addFinding(
        'INFO',
        `📌 Focus: ${decision.department_focus} | Action: ${decision.priority_action.slice(0, 80)}`
      );
    } catch (e) {
      this.addFinding('HIGH', `CEO стратегический анализ не выполнен: ${e.message}`);
      await factoryLog(this.name, `Decision error: ${e.message}`);
    }

    // 3. Weekly report (Mondays only)
    if (isMonday()) {
      try {
        const weeklyReport = await this.generateWeeklyReport(findings, metrics);
        await saveSetting(
          'ceo_weekly_report',
          JSON.stringify({ ...weeklyReport, generated_at: new Date().toISOString() })
        );

        // Save to factory_reports if table exists
        try {
          const now = new Date().toISOString();
          const weekKey = now.slice(0, 10); // YYYY-MM-DD (Monday date)
          await factoryDbRun(
            `INSERT OR REPLACE INTO factory_reports (report_type, period_key, data, created_at)
             VALUES (?,?,?,?)`,
            ['weekly', weekKey, JSON.stringify(weeklyReport), now]
          );
        } catch {}

        // Announce in agent_discussions
        const report = weeklyReport;
        await dbRun(`INSERT INTO agent_discussions (from_agent, to_agent, topic, message) VALUES (?,?,?,?)`, [
          this.name,
          'all',
          `📊 CEO Недельный отчёт — ${report.period || new Date().toISOString().slice(0, 10)}`,
          [
            `📊 *Недельный отчёт CEO*`,
            ``,
            `*${report.headline}*`,
            ``,
            `✅ Достижения: ${(report.wins || []).join('; ')}`,
            `⚠️ Риски: ${(report.risks || []).join('; ')}`,
            ``,
            `📌 Фокус следующей недели: ${report.next_week_focus}`,
          ].join('\n'),
        ]).catch(() => {});

        this.addFinding('INFO', `📊 Недельный отчёт сгенерирован: ${report.headline}`);
        await factoryLog(this.name, `Weekly report generated: ${report.headline}`);
      } catch (e) {
        this.addFinding('MEDIUM', `Недельный отчёт не сгенерирован: ${e.message}`);
        await factoryLog(this.name, `Weekly report error: ${e.message}`);
      }
    }
  }
}

// ─── Run when invoked directly ────────────────────────────────────────────────

async function runCEO() {
  console.log('🏛️ CEO Intelligence — запуск стратегического анализа...\n');
  const agent = new StrategicCEO();

  try {
    await agent.run({ silent: true });
    agent.findings.forEach(f => console.log(`  ${f.sev} ${f.msg}`));
    if (agent.fixed.length) agent.fixed.forEach(fx => console.log(`  🔧 ${fx}`));
  } catch (e) {
    console.error(`  ❌ CEO error: ${e.message}`);
    process.exit(1);
  }

  console.log('\n🏛️ CEO Intelligence — завершено.');
}

if (require.main === module) runCEO().then(() => process.exit(0));

module.exports = { StrategicCEO };
