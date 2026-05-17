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
 *   • agent_discussions (topic='ceo_decision') — for tracking
 *
 * StrategicDecisionMaker (БЛОК 5.3 additions):
 *   • Aggregates agent_logs last 24h by agent (SUCCESS / ERROR counts)
 *   • Loads last ceo_decision from agent_discussions (topic='ceo_decision')
 *   • Simple heuristic: decision older than 7 days → status 'reviewed'
 *   • Rule-based fallback decision when Claude API unavailable
 *   • Saves decisions with topic='ceo_decision'
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
// StrategicDecisionMaker — БЛОК 5.3 additions
// ═════════════════════════════════════════════════════════════════════════════

class StrategicDecisionMaker {
  /**
   * 1. Aggregate agent_logs for the last 24 hours.
   *    Groups by from_name and counts SUCCESS / ERROR occurrences in message text.
   *
   * @returns {{ byAgent: Record<string,{success:number,error:number,total:number}>,
   *             totalSuccess: number, totalError: number }}
   */
  static async aggregateAgentLogs() {
    let rows = [];
    try {
      rows = await dbAll(`
        SELECT from_name, message
        FROM agent_logs
        WHERE created_at > datetime('now', '-24 hours')
        ORDER BY created_at DESC
        LIMIT 500
      `);
    } catch {
      return { byAgent: {}, totalSuccess: 0, totalError: 0 };
    }

    const byAgent = {};
    let totalSuccess = 0;
    let totalError = 0;

    for (const row of rows) {
      const agent = row.from_name || 'unknown';
      if (!byAgent[agent]) byAgent[agent] = { success: 0, error: 0, total: 0 };
      byAgent[agent].total += 1;

      // Heuristic: look for common success / error markers in log messages
      const msg = (row.message || '').toLowerCase();
      const isError =
        msg.includes('error') ||
        msg.includes('ошибк') ||
        msg.includes('failed') ||
        msg.includes('exception') ||
        msg.includes('критич') ||
        msg.includes('❌') ||
        msg.includes('🔴');
      const isSuccess =
        msg.includes('success') ||
        msg.includes('ok') ||
        msg.includes('done') ||
        msg.includes('завершен') ||
        msg.includes('сохранен') ||
        msg.includes('✅') ||
        msg.includes('complete');

      if (isError) {
        byAgent[agent].error += 1;
        totalError += 1;
      } else if (isSuccess) {
        byAgent[agent].success += 1;
        totalSuccess += 1;
      }
    }

    return { byAgent, totalSuccess, totalError };
  }

  /**
   * 2. Load the last CEO decision from agent_discussions (topic='ceo_decision').
   *
   * @returns {{ id:number, message:string, created_at:string }|null}
   */
  static async loadLastCeoDecision() {
    try {
      return await dbGet(`
        SELECT id, message, created_at
        FROM agent_discussions
        WHERE topic = 'ceo_decision'
        ORDER BY created_at DESC
        LIMIT 1
      `);
    } catch {
      return null;
    }
  }

  /**
   * 3. Check whether the previous decision is "reviewed".
   *    Simple heuristic: if the decision was made > 7 days ago → 'reviewed'.
   *    Otherwise → 'pending'.
   *
   * @param {{ created_at:string }|null} lastDecision
   * @returns {'reviewed'|'pending'|'none'}
   */
  static evaluateDecisionStatus(lastDecision) {
    if (!lastDecision) return 'none';
    const createdAt = new Date(lastDecision.created_at);
    if (Number.isNaN(createdAt.getTime())) return 'none';
    const ageMs = Date.now() - createdAt.getTime();
    const ageDays = ageMs / (1000 * 60 * 60 * 24);
    return ageDays > 7 ? 'reviewed' : 'pending';
  }

  /**
   * 4a. Rule-based strategic decision — used when Claude API is unavailable.
   *
   * Priority rules (in order):
   *   a) Error rate > 30% of last 24h logs → stabilise infrastructure
   *   b) No new orders in the last 7 days → marketing / activate leads
   *   c) Otherwise → growth based on most popular event_type from orders
   *
   * @param {{ totalError:number, totalSuccess:number, byAgent:object }} logStats
   * @param {object} metrics  — output of StrategicCEO#loadMetrics()
   * @returns {object}  same shape as makeStrategicDecision() result
   */
  static async buildRuleBasedDecision(logStats, metrics) {
    const totalLogs = logStats.totalSuccess + logStats.totalError;
    const errorRate = totalLogs > 0 ? logStats.totalError / totalLogs : 0;

    // Rule a — stabilise
    if (errorRate > 0.3 || (metrics.agentHealth && metrics.agentHealth.critical >= 3)) {
      const worstAgents = Object.entries(logStats.byAgent)
        .filter(([, s]) => s.error > 0)
        .sort(([, a], [, b]) => b.error - a.error)
        .slice(0, 3)
        .map(([name]) => name)
        .join(', ');

      return {
        decision: 'Приоритет: стабилизация инфраструктуры',
        rationale: `Уровень ошибок агентов за 24ч составил ${(errorRate * 100).toFixed(0)}% (${logStats.totalError} из ${totalLogs} событий). Наиболее проблемные агенты: ${worstAgents || 'неизвестны'}.`,
        department_focus: 'tech',
        priority_action: 'Провести аудит упавших агентов, устранить первопричину ошибок',
        expected_impact: 'Снижение уровня ошибок до < 10% за следующие 24 часа',
        growth_action: {
          action_type: 'ops',
          channel: 'all',
          description: 'Исправить критические ошибки агентов и восстановить стабильность системы',
        },
      };
    }

    // Rule b — no new orders in 7 days
    const ordersLastWeek = metrics.orders?.lastWeek ?? 0;
    if (ordersLastWeek === 0) {
      return {
        decision: 'Приоритет: маркетинг — активировать лиды',
        rationale:
          'За последние 7 дней не зафиксировано ни одной новой заявки. Необходима срочная активация существующих лидов и привлечение новых клиентов.',
        department_focus: 'sales',
        priority_action: 'Запустить рассылку по базе клиентов с акционным предложением',
        expected_impact: 'Получить минимум 1-2 новых заявки в течение 48 часов',
        growth_action: {
          action_type: 'social',
          channel: 'telegram',
          description: 'Активировать лиды: рассылка по базе + пост в соцсетях с промо-предложением',
        },
      };
    }

    // Rule c — growth: find most popular event_type from recent orders
    let popularEventType = 'фотосессия';
    try {
      const row = await dbGet(`
        SELECT event_type, COUNT(*) as cnt
        FROM orders
        WHERE created_at > datetime('now', '-30 days')
          AND event_type IS NOT NULL AND event_type != ''
        GROUP BY event_type
        ORDER BY cnt DESC
        LIMIT 1
      `);
      if (row?.event_type) popularEventType = row.event_type;
    } catch {}

    return {
      decision: `Приоритет: рост — ${popularEventType}`,
      rationale: `Инфраструктура работает стабильно (ошибок ${logStats.totalError} из ${totalLogs}). Активность заявок в норме (${ordersLastWeek} за неделю). Фокус на развитии самого востребованного направления: ${popularEventType}.`,
      department_focus: 'sales',
      priority_action: `Усилить продвижение услуги "${popularEventType}": обновить описание, добавить кейсы`,
      expected_impact: `Рост заявок по направлению "${popularEventType}" на 20% за 7 дней`,
      growth_action: {
        action_type: 'sales',
        channel: 'all',
        description: `Создать промо-материалы и усилить продвижение "${popularEventType}"`,
      },
    };
  }

  /**
   * 5. Save CEO decision to agent_discussions with topic='ceo_decision'.
   *
   * @param {object} decision
   */
  static async saveDecisionAsDiscussion(decision) {
    const decText = [
      decision.decision,
      '',
      `Основание: ${decision.rationale}`,
      '',
      `Приоритетное действие: ${decision.priority_action}`,
      `Ожидаемый результат: ${decision.expected_impact || '—'}`,
    ].join('\n');

    await dbRun(`INSERT INTO agent_discussions (from_agent, to_agent, topic, message) VALUES (?,?,?,?)`, [
      'StrategicCEO',
      'all',
      'ceo_decision',
      decText,
    ]).catch(() => {});
  }

  /**
   * Full pipeline: aggregate logs → load last decision → evaluate status →
   * make rule-based decision → save to agent_discussions.
   *
   * Returns a summary object that StrategicCEO can use to add findings.
   *
   * @param {object} metrics  — from StrategicCEO#loadMetrics()
   * @param {Agent}  agent    — StrategicCEO instance (for addFinding)
   * @param {Function} log    — factoryLog-compatible async (name, msg)
   */
  static async run(metrics, agent, log) {
    // Step 1 — aggregate agent_logs
    const logStats = await StrategicDecisionMaker.aggregateAgentLogs();

    const agentCount = Object.keys(logStats.byAgent).length;
    await log(
      agent.name,
      `StrategicDecisionMaker: agent_logs 24h — agents: ${agentCount}, success: ${logStats.totalSuccess}, error: ${logStats.totalError}`
    );

    // Step 2 — load last CEO decision from agent_discussions
    const lastDecision = await StrategicDecisionMaker.loadLastCeoDecision();

    // Step 3 — evaluate previous decision status (heuristic)
    const prevStatus = StrategicDecisionMaker.evaluateDecisionStatus(lastDecision);

    if (lastDecision) {
      const statusLabel = prevStatus === 'reviewed' ? '✅ рассмотрено (> 7 дней)' : '⏳ на исполнении (< 7 дней)';
      agent.addFinding(
        'INFO',
        `📋 Предыдущее CEO-решение [${lastDecision.created_at?.slice(0, 10)}]: ${statusLabel} — "${lastDecision.message?.slice(0, 80)}"`
      );
    }

    // Step 4 — rule-based decision
    const ruleDecision = await StrategicDecisionMaker.buildRuleBasedDecision(logStats, metrics);

    agent.addFinding(
      'INFO',
      `🏛️ [Rule-based] ${ruleDecision.decision} | Focus: ${ruleDecision.department_focus} | Action: ${ruleDecision.priority_action.slice(0, 80)}`
    );

    // Step 5 — save decision as discussion with topic='ceo_decision'
    await StrategicDecisionMaker.saveDecisionAsDiscussion(ruleDecision);
    await log(
      agent.name,
      `StrategicDecisionMaker: decision saved → topic=ceo_decision: ${ruleDecision.decision.slice(0, 80)}`
    );

    // Also persist to bot_settings for quick access
    try {
      await saveSetting(
        'ceo_rule_based_decision',
        JSON.stringify({
          ...ruleDecision,
          prev_decision_status: prevStatus,
          log_stats: { totalSuccess: logStats.totalSuccess, totalError: logStats.totalError, agentCount },
          created_at: new Date().toISOString(),
        })
      );
    } catch {}

    return { logStats, prevStatus, ruleDecision };
  }
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

  // ── 7. Propose A/B experiment based on conversion data ────────────────────
  async proposeExperiment() {
    try {
      // Gather conversion-relevant data
      const [orderStats, modelStats, reviewStats, lastExp] = await Promise.all([
        dbGet(`SELECT
          COUNT(*) as total,
          SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
          SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) as pending,
          SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) as cancelled,
          SUM(CASE WHEN created_at >= datetime('now','-7 days') THEN 1 ELSE 0 END) as last_week
        FROM orders`).catch(() => ({})),
        dbAll(`SELECT m.name, m.available, COUNT(o.id) as orders_cnt
          FROM models m
          LEFT JOIN orders o ON o.model_id = m.id
          GROUP BY m.id ORDER BY orders_cnt DESC LIMIT 5`).catch(() => []),
        dbGet(`SELECT COUNT(*) as cnt, AVG(rating) as avg_rating
          FROM reviews WHERE approved=1`).catch(() => ({})),
        dbGet(`SELECT value FROM bot_settings WHERE key='ceo_last_experiment'`).catch(() => null),
      ]);

      const conversionRate =
        orderStats.total > 0 ? ((orderStats.completed / orderStats.total) * 100).toFixed(1) : 'н/д';

      const summary = [
        `Заявки всего: ${orderStats.total || 0}, выполнено: ${orderStats.completed || 0}, ожидают: ${orderStats.pending || 0}, отменено: ${orderStats.cancelled || 0}.`,
        `Конверсия (completed/total): ${conversionRate}%.`,
        `Заявки за последние 7 дней: ${orderStats.last_week || 0}.`,
        `Топ модели по заявкам: ${modelStats.map(m => `${m.name} (${m.orders_cnt})`).join(', ') || 'нет данных'}.`,
        `Отзывы: ${reviewStats.cnt || 0} одобрено, средний рейтинг: ${reviewStats.avg_rating ? Number(reviewStats.avg_rating).toFixed(1) : 'н/д'}.`,
        lastExp?.value
          ? `Последний эксперимент: ${lastExp.value.slice(0, 150)}`
          : 'Предыдущих экспериментов не зафиксировано.',
      ].join('\n');

      const apiKey = process.env.ANTHROPIC_API_KEY;
      if (!apiKey) return;

      const raw = await callClaude({
        systemPrompt: [
          'Ты — CEO модельного агентства. Предложи ОДИН конкретный A/B эксперимент для увеличения конверсии или среднего чека.',
          'Формат (строго JSON):',
          '{',
          '  "hypothesis": "Если мы сделаем X, то Y увеличится на Z%",',
          '  "variant_a": "Текущий вариант (контроль)",',
          '  "variant_b": "Новый вариант (эксперимент)",',
          '  "metric": "Что измеряем (конверсия/средний чек/возвраты/рейтинг)",',
          '  "duration_days": 7,',
          '  "expected_uplift": "Ожидаемый прирост"',
          '}',
        ].join('\n'),
        userPrompt: `Данные агентства:\n${summary}`,
        maxTokens: 400,
      });

      const jsonMatch = raw.match(/\{[\s\S]*\}/);
      if (!jsonMatch) {
        await factoryLog(this.name, `proposeExperiment: non-JSON response`);
        return;
      }
      const experiment = JSON.parse(jsonMatch[0]);

      // Persist experiment proposal
      await saveSetting(
        'ceo_last_experiment',
        JSON.stringify({ ...experiment, proposed_at: new Date().toISOString() })
      );

      this.addFinding(
        'INFO',
        `🧪 A/B Эксперимент: ${experiment.hypothesis?.slice(0, 120) || 'предложен'} | Метрика: ${experiment.metric || '—'} | Длительность: ${experiment.duration_days || 7} дней`
      );
      await factoryLog(this.name, `Experiment proposed: ${experiment.hypothesis?.slice(0, 80)}`);
    } catch (e) {
      await factoryLog(this.name, `proposeExperiment error: ${e.message}`);
    }
  }

  // ── 8. Track execution of previous CEO decisions ───────────────────────────
  async trackPreviousDecisions() {
    try {
      // Read last 3 CEO decisions from agent_logs (or bot_settings fallback)
      let pastDecisions = [];
      try {
        pastDecisions = await dbAll(`
          SELECT message, created_at FROM agent_logs
          WHERE agent_name = 'StrategicCEO'
            AND message LIKE 'Decision saved:%'
          ORDER BY created_at DESC LIMIT 3
        `);
      } catch {}

      // Also pull structured decision from bot_settings
      let lastDecisionData = null;
      try {
        const row = await dbGet(`SELECT value, updated_at FROM bot_settings WHERE key='ceo_last_decision'`);
        if (row?.value) lastDecisionData = { ...JSON.parse(row.value), saved_at: row.updated_at };
      } catch {}

      if (!lastDecisionData && pastDecisions.length === 0) {
        await factoryLog(this.name, 'trackPreviousDecisions: no past decisions to evaluate');
        return;
      }

      // Get current metrics to compare against decision context
      const [currentOrders, currentReviews, criticalFindings] = await Promise.all([
        dbGet(`SELECT COUNT(*) as n FROM orders WHERE created_at >= datetime('now','-24 hours')`).catch(() => ({})),
        dbGet(
          `SELECT ROUND(AVG(rating),2) as avg FROM reviews WHERE approved=1 AND created_at >= datetime('now','-24 hours')`
        ).catch(() => ({})),
        dbGet(
          `SELECT COUNT(*) as n FROM agent_findings WHERE severity='🔴' AND created_at >= datetime('now','-24 hours')`
        ).catch(() => ({})),
      ]);

      const decisionsText = pastDecisions.length
        ? pastDecisions.map(d => `[${d.created_at?.slice(0, 16)}] ${d.message}`).join('\n')
        : 'Нет записей в agent_logs.';

      const lastDecText = lastDecisionData
        ? `Последнее решение (${lastDecisionData.created_at?.slice(0, 10)}): ${lastDecisionData.decision} | Ожидалось: ${lastDecisionData.expected_impact}`
        : '';

      const currentStatus = [
        `Новые заявки за 24ч: ${currentOrders?.n ?? 0}.`,
        `Средний рейтинг отзывов за 24ч: ${currentReviews?.avg ?? 'н/д'}.`,
        `Критических проблем за 24ч: ${criticalFindings?.n ?? 0}.`,
      ].join(' ');

      const apiKey = process.env.ANTHROPIC_API_KEY;
      if (!apiKey) return;

      const raw = await callClaude({
        systemPrompt: [
          'Ты — CEO модельного агентства. Оцени, выполнены ли прошлые решения и улучшились ли метрики.',
          'Формат (строго JSON):',
          '{',
          '  "decisions_reviewed": 0,',
          '  "status": "executed|partial|not_executed",',
          '  "metrics_improved": true,',
          '  "assessment": "Краткая оценка (2-3 предложения)",',
          '  "next_step": "Что скорректировать или продолжить"',
          '}',
        ].join('\n'),
        userPrompt: [
          '=== ПРОШЛЫЕ РЕШЕНИЯ CEO ===',
          decisionsText,
          lastDecText,
          '',
          '=== ТЕКУЩИЕ МЕТРИКИ ===',
          currentStatus,
        ].join('\n'),
        maxTokens: 400,
      });

      const jsonMatch = raw.match(/\{[\s\S]*\}/);
      if (!jsonMatch) {
        await factoryLog(this.name, `trackPreviousDecisions: non-JSON response`);
        return;
      }
      const tracking = JSON.parse(jsonMatch[0]);

      // Save tracking result
      await saveSetting(
        'ceo_decisions_tracking',
        JSON.stringify({ ...tracking, checked_at: new Date().toISOString() })
      );

      const statusEmoji = tracking.status === 'executed' ? '✅' : tracking.status === 'partial' ? '🔶' : '⚠️';
      this.addFinding(
        'INFO',
        `${statusEmoji} Выполнение прошлых решений: ${tracking.status || '—'} | Метрики улучшились: ${tracking.metrics_improved ? 'да' : 'нет'} | ${tracking.assessment?.slice(0, 100) || ''}`
      );
      await factoryLog(this.name, `Decision tracking: ${tracking.status} | improved: ${tracking.metrics_improved}`);
    } catch (e) {
      await factoryLog(this.name, `trackPreviousDecisions error: ${e.message}`);
    }
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

    // 4. Track previous decisions (every cycle)
    await this.trackPreviousDecisions();

    // 5. Propose A/B experiment (every cycle)
    await this.proposeExperiment();

    // 6. StrategicDecisionMaker — БЛОК 5.3
    //    Aggregates agent_logs, evaluates previous ceo_decision status,
    //    builds a rule-based fallback decision and saves it with topic='ceo_decision'.
    try {
      await StrategicDecisionMaker.run(metrics, this, factoryLog);
    } catch (e) {
      this.addFinding('MEDIUM', `StrategicDecisionMaker error: ${e.message}`);
      await factoryLog(this.name, `StrategicDecisionMaker error: ${e.message}`);
    }

    // 7. Track A/B experiment results — БЛОК 5.4
    const tracker = new ExperimentTracker();
    await tracker.run(this).catch(e => this.addFinding('LOW', `ExperimentTracker ошибка: ${e.message}`));
  }
}

// ─── БЛОК 5.4: ExperimentTracker ─────────────────────────────────────────────

class ExperimentTracker {
  // Загружает текущий активный эксперимент из bot_settings
  async loadActiveExperiment() {
    const raw = await dbGet("SELECT value FROM bot_settings WHERE key='ceo_last_experiment'").catch(() => null);
    if (!raw?.value) return null;
    try {
      return JSON.parse(raw.value);
    } catch {
      return null;
    }
  }

  // Вычисляет результат: сравнивает метрику ДО и ПОСЛЕ начала эксперимента
  async evaluateExperiment(experiment) {
    if (!experiment?.metric || !experiment?.proposed_at) return null;
    const startDate = new Date(experiment.proposed_at);
    const daysSince = (Date.now() - startDate.getTime()) / (1000 * 60 * 60 * 24);
    if (daysSince < (experiment.duration_days || 7)) {
      return { status: 'in_progress', daysSince: Math.round(daysSince), durationDays: experiment.duration_days || 7 };
    }

    // Получаем данные за период эксперимента vs предыдущий период
    const periodMs = daysSince * 24 * 60 * 60 * 1000;
    const [afterRows, beforeRows] = await Promise.all([
      dbAll(`SELECT COUNT(*) as n FROM orders WHERE created_at >= ? AND status != 'cancelled'`, [
        startDate.toISOString(),
      ]).catch(() => [{ n: 0 }]),
      dbAll(`SELECT COUNT(*) as n FROM orders WHERE created_at < ? AND created_at >= ? AND status != 'cancelled'`, [
        startDate.toISOString(),
        new Date(startDate - periodMs).toISOString(),
      ]).catch(() => [{ n: 0 }]),
    ]);
    const after = afterRows[0]?.n ?? 0;
    const before = beforeRows[0]?.n ?? 0;
    const delta = before > 0 ? Math.round(((after - before) / before) * 100) : null;
    const success = delta !== null && delta > 5; // >5% рост считается успехом

    return { status: success ? 'success' : 'no_effect', after, before, delta, daysSince: Math.round(daysSince) };
  }

  // Сохраняет результат и архивирует эксперимент
  async archiveExperiment(experiment, result) {
    const archived = { ...experiment, result, archived_at: new Date().toISOString() };
    // Получить историю экспериментов
    const histRaw = await dbGet("SELECT value FROM bot_settings WHERE key='ceo_experiment_history'").catch(() => null);
    let history = [];
    try {
      history = JSON.parse(histRaw?.value || '[]');
    } catch {}
    history.unshift(archived);
    if (history.length > 20) history = history.slice(0, 20);
    await dbRun('INSERT OR REPLACE INTO bot_settings(key,value) VALUES(?,?)', [
      'ceo_experiment_history',
      JSON.stringify(history),
    ]).catch(() => {});
    // Сбросить текущий эксперимент
    await dbRun("DELETE FROM bot_settings WHERE key='ceo_last_experiment'").catch(() => {});
  }

  async run(ceoAgent) {
    const experiment = await this.loadActiveExperiment();
    if (!experiment) {
      ceoAgent.addFinding('INFO', 'ExperimentTracker: нет активного эксперимента для отслеживания');
      return;
    }
    const result = await this.evaluateExperiment(experiment);
    if (!result) return;
    if (result.status === 'in_progress') {
      ceoAgent.addFinding(
        'INFO',
        `ExperimentTracker: A/B эксперимент в процессе: ${result.daysSince}/${result.durationDays} дней. Гипотеза: ${experiment.hypothesis?.slice(0, 80) || '—'}`
      );
      return;
    }
    const emoji = result.status === 'success' ? '✅' : '⚠️';
    const deltaStr = result.delta !== null ? `${result.delta > 0 ? '+' : ''}${result.delta}%` : 'н/д';
    ceoAgent.addFinding(
      result.status === 'success' ? 'INFO' : 'LOW',
      `${emoji} ExperimentTracker: эксперимент завершён (${result.daysSince} дней). Заявок до: ${result.before}, после: ${result.after} (${deltaStr}). Статус: ${result.status}`
    );
    await this.archiveExperiment(experiment, result);
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

module.exports = { StrategicCEO, StrategicDecisionMaker };
