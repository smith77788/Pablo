/**
 * 🔬 Research Department — Market intelligence, competitor signals, trends, insights
 *
 * Agents:
 *   MarketResearcher    — analyses event_type distribution and growth segments
 *   CompetitorAnalyst   — spots cities with above-average budget (competitive markets)
 *   TrendSpotter        — detects new event types / cities appearing in last 14 days
 *   InsightSynthesizer  — collects findings from the three above and produces executive summary via Claude
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

const { Agent, dbAll, dbGet, logAgent } = require('../lib/base');

// ─── Shared Claude API helper ─────────────────────────────────────────────────
async function callClaude({ systemPrompt, userPrompt, maxTokens = 600 }) {
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

/** Write result to agent_logs + console */
async function factoryLog(agentName, message) {
  console.log(`[${agentName}] ${message}`);
  await logAgent(agentName, message);
}

// ═════════════════════════════════════════════════════════════════════════════
// 1. MarketResearcher
// ═════════════════════════════════════════════════════════════════════════════
class MarketResearcher extends Agent {
  constructor() {
    super({
      id: 'res-01',
      name: 'MarketResearcher',
      organ: 'Research Department',
      emoji: '🌍',
      focus: 'Analyse event_type distribution and detect fastest-growing segment',
    });
  }

  async analyze() {
    let current, previous;
    try {
      [current, previous] = await Promise.all([
        // Last 30 days
        dbAll(
          `SELECT event_type, COUNT(*) as cnt
           FROM orders
           WHERE event_type IS NOT NULL AND event_type != ''
             AND created_at >= datetime('now', '-30 days')
           GROUP BY event_type
           ORDER BY cnt DESC`
        ),
        // Previous 30 days (30–60 days ago)
        dbAll(
          `SELECT event_type, COUNT(*) as cnt
           FROM orders
           WHERE event_type IS NOT NULL AND event_type != ''
             AND created_at >= datetime('now', '-60 days')
             AND created_at < datetime('now', '-30 days')
           GROUP BY event_type
           ORDER BY cnt DESC`
        ),
      ]);
    } catch (e) {
      this.addFinding('HIGH', `MarketResearcher: ошибка запроса: ${e.message}`);
      return;
    }

    if (!current.length) {
      this.addFinding('OK', 'MarketResearcher: нет данных о заявках за последние 30 дней');
      return;
    }

    const totalCurrent = current.reduce((s, r) => s + r.cnt, 0);
    const distribution = current
      .slice(0, 5)
      .map(r => `${r.event_type}: ${r.cnt} (${Math.round((r.cnt / totalCurrent) * 100)}%)`)
      .join(', ');

    this.addFinding('INFO', `🌍 Распределение типов событий (30 дн.): ${distribution}`);

    // Find fastest-growing segment
    if (previous.length) {
      const prevMap = new Map(previous.map(r => [r.event_type, r.cnt]));
      let bestType = null;
      let bestGrowth = 0;

      for (const row of current) {
        const prevCnt = prevMap.get(row.event_type) || 0;
        if (prevCnt > 0) {
          const growth = (row.cnt - prevCnt) / prevCnt;
          if (growth > bestGrowth) {
            bestGrowth = growth;
            bestType = row;
          }
        } else if (row.cnt > 0) {
          // New segment — treat as 100%+ growth
          if (!bestType || row.cnt > (bestType.cnt || 0)) {
            bestGrowth = 1; // 100%
            bestType = row;
          }
        }
      }

      if (bestType && bestGrowth > 0.1) {
        this.addFinding(
          'LOW',
          `📈 Самый быстро растущий сегмент: "${bestType.event_type}" (+${Math.round(bestGrowth * 100)}% за месяц, ${bestType.cnt} заказов)`
        );
        await factoryLog(this.name, `Fastest growing: "${bestType.event_type}" +${Math.round(bestGrowth * 100)}%`);
      }
    }

    await factoryLog(this.name, `Event distribution: ${distribution}`);
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 2. CompetitorAnalyst
// ═════════════════════════════════════════════════════════════════════════════
class CompetitorAnalyst extends Agent {
  constructor() {
    super({
      id: 'res-02',
      name: 'CompetitorAnalyst',
      organ: 'Research Department',
      emoji: '🏙️',
      focus: 'Spot cities with significantly above-average budget — potential premium markets',
    });
  }

  async analyze() {
    let cities;
    try {
      cities = await dbAll(
        `SELECT location,
                AVG(CAST(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(budget,'0'),'₽',''),'руб',''),' ',''),',','.') AS REAL)) as avg_budget,
                COUNT(*) as cnt
         FROM orders
         WHERE status IN ('confirmed', 'completed')
           AND location IS NOT NULL AND location != ''
           AND budget IS NOT NULL AND budget != ''
           AND budget GLOB '[0-9]*'
         GROUP BY location
         HAVING cnt >= 2
         ORDER BY avg_budget DESC`
      );
    } catch (e) {
      this.addFinding('HIGH', `CompetitorAnalyst: ошибка запроса: ${e.message}`);
      return;
    }

    if (!cities.length) {
      this.addFinding('OK', 'CompetitorAnalyst: недостаточно данных по городам');
      return;
    }

    const totalAvg = cities.reduce((s, c) => s + c.avg_budget, 0) / cities.length;
    const PREMIUM_THRESHOLD = 1.3; // 30% above average

    const premiumCities = cities.filter(c => c.avg_budget > totalAvg * PREMIUM_THRESHOLD);

    if (!premiumCities.length) {
      this.addFinding(
        'OK',
        `CompetitorAnalyst: нет городов со значительно выше среднего бюджетом (порог +30%). Средний: ${Math.round(totalAvg).toLocaleString('ru')} ₽`
      );
      return;
    }

    for (const city of premiumCities) {
      const pct = Math.round((city.avg_budget / totalAvg - 1) * 100);
      this.addFinding(
        'LOW',
        `🏙️ "${city.location}": средний бюджет ${Math.round(city.avg_budget).toLocaleString('ru')} ₽ (+${pct}% к среднему, ${city.cnt} заказов) — потенциально конкурентный рынок, возможно повышение цен.`
      );
    }

    await factoryLog(
      this.name,
      `Premium cities: ${premiumCities.map(c => `${c.location} ${Math.round(c.avg_budget)}`).join(', ')}. Overall avg: ${Math.round(totalAvg)} RUB`
    );
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 3. TrendSpotter
// ═════════════════════════════════════════════════════════════════════════════
class TrendSpotter extends Agent {
  constructor() {
    super({
      id: 'res-03',
      name: 'TrendSpotter',
      organ: 'Research Department',
      emoji: '🔭',
      focus: 'Detect new event types or cities first seen in the last 14 days',
    });
  }

  async analyze() {
    let newTypes, newCities;
    try {
      // event_types that appeared for the first time in last 14 days
      [newTypes, newCities] = await Promise.all([
        dbAll(
          `SELECT event_type, COUNT(*) as cnt, MIN(created_at) as first_seen
           FROM orders
           WHERE event_type IS NOT NULL AND event_type != ''
             AND created_at >= datetime('now', '-14 days')
           GROUP BY event_type
           HAVING MIN(created_at) >= datetime('now', '-14 days')
             AND event_type NOT IN (
               SELECT DISTINCT event_type FROM orders WHERE created_at < datetime('now', '-14 days') AND event_type IS NOT NULL
             )`
        ),
        dbAll(
          `SELECT location, COUNT(*) as cnt, MIN(created_at) as first_seen
           FROM orders
           WHERE location IS NOT NULL AND location != ''
             AND created_at >= datetime('now', '-14 days')
           GROUP BY location
           HAVING MIN(created_at) >= datetime('now', '-14 days')
             AND location NOT IN (
               SELECT DISTINCT location FROM orders WHERE created_at < datetime('now', '-14 days') AND location IS NOT NULL
             )`
        ),
      ]);
    } catch (e) {
      this.addFinding('HIGH', `TrendSpotter: ошибка запроса: ${e.message}`);
      return;
    }

    if (!newTypes.length && !newCities.length) {
      this.addFinding('OK', 'TrendSpotter: новых типов событий и городов за 14 дней не выявлено');
      return;
    }

    if (newTypes.length) {
      const list = newTypes.map(t => `"${t.event_type}" (${t.cnt} заказов)`).join(', ');
      this.addFinding('LOW', `🔭 Новый тренд — новые типы событий за 14 дней: ${list}`);
      await factoryLog(this.name, `New event types: ${list}`);
    }

    if (newCities.length) {
      const list = newCities.map(c => `"${c.location}" (${c.cnt} заказов)`).join(', ');
      this.addFinding('LOW', `🔭 Новый тренд — новые города за 14 дней: ${list}`);
      await factoryLog(this.name, `New cities: ${list}`);
    }
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 4. InsightSynthesizer
// ═════════════════════════════════════════════════════════════════════════════
class InsightSynthesizer extends Agent {
  constructor() {
    super({
      id: 'res-04',
      name: 'InsightSynthesizer',
      organ: 'Research Department',
      emoji: '💡',
      focus: 'Synthesise findings from MarketResearcher, CompetitorAnalyst, TrendSpotter into executive summary',
    });

    // Will be populated by the department runner before analyze() is called
    this._siblingFindings = [];
  }

  /**
   * Accepts findings from sibling agents to include in synthesis.
   * @param {Array<{agentName: string, findings: Array}>} siblings
   */
  setSiblingFindings(siblings) {
    this._siblingFindings = siblings;
  }

  async analyze() {
    const allFindings = this._siblingFindings.flatMap(s =>
      (s.findings || []).map(f => `[${s.agentName}] ${f.sev} ${f.msg}`)
    );

    if (!process.env.ANTHROPIC_API_KEY) {
      // Fallback: produce a rule-based summary
      if (!allFindings.length) {
        this.addFinding('INFO', '💡 Синтез: данные собраны, но API-ключ не настроен для генерации резюме.');
      } else {
        this.addFinding(
          'INFO',
          `💡 Research summary (${allFindings.length} findings): ${allFindings.slice(0, 3).join(' | ')}`
        );
      }
      return;
    }

    let userPrompt;

    if (allFindings.length) {
      userPrompt = `Отчёты исследовательского департамента:\n${allFindings.join('\n')}`;
    } else {
      // No sibling findings: summarise current DB state
      let totals;
      try {
        totals = await dbGet(
          `SELECT COUNT(*) as orders_total,
                  SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) as new_cnt,
                  SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed_cnt
           FROM orders`
        );
      } catch {
        totals = {};
      }
      userPrompt = `Нет находок от других агентов. Текущее состояние БД: всего заявок ${totals?.orders_total || 0}, новых ${totals?.new_cnt || 0}, завершённых ${totals?.completed_cnt || 0}.`;
    }

    try {
      const summary = await callClaude({
        systemPrompt: [
          'Ты — аналитик модельного агентства Nevesty Models.',
          'На основе отчётов исследовательского департамента напиши краткое executive summary (3-5 строк) на русском языке.',
          'Выдели главные инсайты, риски и возможности. Без вступлений — только суть.',
        ].join(' '),
        userPrompt,
        maxTokens: 250,
      });

      this.addFinding('INFO', `💡 Executive Summary (Research): ${summary.trim()}`);
      await factoryLog(this.name, `Executive summary generated (${allFindings.length} input findings)`);
    } catch (e) {
      this.addFinding('LOW', `InsightSynthesizer: не удалось сгенерировать резюме: ${e.message}`);
      await factoryLog(this.name, `Summary generation failed: ${e.message}`);
    }
  }
}

// ─── Run all four agents when invoked directly ────────────────────────────────
async function runResearchDepartment() {
  console.log('🔬 Research Department — запуск...\n');

  const researcher = new MarketResearcher();
  const competitor = new CompetitorAnalyst();
  const spotter = new TrendSpotter();
  const synthesizer = new InsightSynthesizer();

  const siblings = [];

  for (const agent of [researcher, competitor, spotter]) {
    console.log(`\n${agent.emoji} ${agent.name}`);
    try {
      await agent.run({ silent: true });
      agent.findings.forEach(f => console.log(`  ${f.sev} ${f.msg}`));
      agent.fixed.forEach(fx => console.log(`  🔧 ${fx}`));
      siblings.push({ agentName: agent.name, findings: agent.findings });
    } catch (e) {
      console.error(`  ❌ Error: ${e.message}`);
    }
  }

  // Feed sibling findings into synthesizer before running
  synthesizer.setSiblingFindings(siblings);

  console.log(`\n${synthesizer.emoji} ${synthesizer.name}`);
  try {
    await synthesizer.run({ silent: true });
    synthesizer.findings.forEach(f => console.log(`  ${f.sev} ${f.msg}`));
  } catch (e) {
    console.error(`  ❌ Error: ${e.message}`);
  }

  console.log('\n🔬 Research Department — завершено.');
}

if (require.main === module) runResearchDepartment().then(() => process.exit(0));

module.exports = { MarketResearcher, CompetitorAnalyst, TrendSpotter, InsightSynthesizer };
