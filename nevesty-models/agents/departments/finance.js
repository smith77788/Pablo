/**
 * 💰 Finance Department — Revenue, pricing, forecasting, budget planning
 *
 * Agents:
 *   RevenueForecaster  — forecasts next 30-day revenue based on recent performance
 *   CostOptimizer      — spots event categories with below-average budget
 *   PricingStrategist  — detects budget drops > 15% vs previous quarter
 *   BudgetPlanner      — summarises pipeline and flags excess pending orders
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

const { Agent, dbGet, dbAll, logAgent } = require('../lib/base');

/** Normalise budget string → number (0 if unparseable) */
function _parseBudget(raw) {
  if (!raw) return 0;
  const n = parseFloat(String(raw).replace(/₽/g, '').replace(/руб/gi, '').replace(/\s/g, '').replace(',', '.'));
  return isNaN(n) ? 0 : n;
}

const BUDGET_SQL_EXPR = `CAST(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(budget,'0'),'₽',''),'руб',''),' ',''),',','.') AS REAL)`;

/** Write result to agent_logs + console */
async function factoryLog(agentName, message) {
  console.log(`[${agentName}] ${message}`);
  await logAgent(agentName, message);
}

// ═════════════════════════════════════════════════════════════════════════════
// 1. RevenueForecaster
// ═════════════════════════════════════════════════════════════════════════════
class RevenueForecaster extends Agent {
  constructor() {
    super({
      id: 'fin-01',
      name: 'RevenueForecaster',
      organ: 'Finance Department',
      emoji: '📊',
      focus: 'Forecast next 30-day revenue based on last 30 days performance',
    });
  }

  async analyze() {
    let revenue30;
    try {
      revenue30 = await dbGet(
        `SELECT SUM(${BUDGET_SQL_EXPR}) as total, COUNT(*) as cnt
         FROM orders
         WHERE status IN ('confirmed', 'completed')
           AND created_at >= datetime('now', '-30 days')
           AND budget IS NOT NULL AND budget != ''
           AND budget GLOB '[0-9]*'`
      );
    } catch (e) {
      this.addFinding('HIGH', `RevenueForecaster: ошибка запроса: ${e.message}`);
      return;
    }

    const total = Math.round(revenue30?.total || 0);
    const cnt = revenue30?.cnt || 0;

    if (!total) {
      this.addFinding('MEDIUM', 'RevenueForecaster: нет данных о выручке за последние 30 дней');
      return;
    }

    // Days remaining in current month
    const now = new Date();
    const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    const dayOfMonth = now.getDate();
    const daysLeft = daysInMonth - dayOfMonth;

    const dailyRate = total / 30;
    const forecast = Math.round(dailyRate * daysLeft);

    const msg = [
      `📊 Выручка за 30 дней: ${total.toLocaleString('ru')} ₽ (${cnt} заказов).`,
      `Прогноз на остаток месяца (${daysLeft} дн.): ~${forecast.toLocaleString('ru')} ₽.`,
      `Среднедневная выручка: ~${Math.round(dailyRate).toLocaleString('ru')} ₽.`,
    ].join(' ');

    this.addFinding('INFO', msg);
    await factoryLog(
      this.name,
      `Revenue 30d: ${total} RUB (${cnt} orders). Forecast: +${forecast} RUB in ${daysLeft} days`
    );
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 2. CostOptimizer
// ═════════════════════════════════════════════════════════════════════════════
class CostOptimizer extends Agent {
  constructor() {
    super({
      id: 'fin-02',
      name: 'CostOptimizer',
      organ: 'Finance Department',
      emoji: '✂️',
      focus: 'Identify event categories with low average budget and suggest price correction',
    });
  }

  async analyze() {
    let categories;
    try {
      categories = await dbAll(
        `SELECT event_type,
                AVG(${BUDGET_SQL_EXPR}) as avg_budget,
                COUNT(*) as cnt
         FROM orders
         WHERE status IN ('confirmed', 'completed')
           AND event_type IS NOT NULL AND event_type != ''
           AND budget IS NOT NULL AND budget != ''
           AND budget GLOB '[0-9]*'
         GROUP BY event_type
         ORDER BY avg_budget`
      );
    } catch (e) {
      this.addFinding('HIGH', `CostOptimizer: ошибка запроса: ${e.message}`);
      return;
    }

    if (!categories.length) {
      this.addFinding('OK', 'CostOptimizer: нет данных по категориям событий');
      return;
    }

    const LOW_BUDGET_THRESHOLD = 10000;
    const lowBudgetCats = categories.filter(c => c.avg_budget > 0 && c.avg_budget < LOW_BUDGET_THRESHOLD);

    if (!lowBudgetCats.length) {
      this.addFinding('OK', `CostOptimizer: все категории выше порога ${LOW_BUDGET_THRESHOLD.toLocaleString('ru')} ₽`);
      return;
    }

    for (const cat of lowBudgetCats) {
      this.addFinding(
        'MEDIUM',
        `✂️ Категория "${cat.event_type}": средний бюджет ${Math.round(cat.avg_budget).toLocaleString('ru')} ₽ (${cat.cnt} заказов) — ниже порога ${LOW_BUDGET_THRESHOLD.toLocaleString('ru')} ₽. Рекомендуется скорректировать прайс.`
      );
    }

    const allList = categories
      .map(c => `${c.event_type}: ${Math.round(c.avg_budget).toLocaleString('ru')} ₽ (${c.cnt})`)
      .join(', ');
    await factoryLog(this.name, `Category budgets: ${allList}`);
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 3. PricingStrategist
// ═════════════════════════════════════════════════════════════════════════════
class PricingStrategist extends Agent {
  constructor() {
    super({
      id: 'fin-03',
      name: 'PricingStrategist',
      organ: 'Finance Department',
      emoji: '🎯',
      focus: 'Detect > 15% budget drop vs previous quarter — flag as HIGH risk',
    });
  }

  async analyze() {
    // Current quarter: last 90 days. Previous quarter: 90-180 days ago.
    let current, previous;
    try {
      [current, previous] = await Promise.all([
        dbAll(
          `SELECT event_type,
                  AVG(${BUDGET_SQL_EXPR}) as avg_budget,
                  COUNT(*) as cnt
           FROM orders
           WHERE status IN ('confirmed', 'completed')
             AND event_type IS NOT NULL AND event_type != ''
             AND budget IS NOT NULL AND budget != ''
             AND budget GLOB '[0-9]*'
             AND created_at >= datetime('now', '-90 days')
           GROUP BY event_type`
        ),
        dbAll(
          `SELECT event_type,
                  AVG(${BUDGET_SQL_EXPR}) as avg_budget,
                  COUNT(*) as cnt
           FROM orders
           WHERE status IN ('confirmed', 'completed')
             AND event_type IS NOT NULL AND event_type != ''
             AND budget IS NOT NULL AND budget != ''
             AND budget GLOB '[0-9]*'
             AND created_at >= datetime('now', '-180 days')
             AND created_at < datetime('now', '-90 days')
           GROUP BY event_type`
        ),
      ]);
    } catch (e) {
      this.addFinding('HIGH', `PricingStrategist: ошибка запроса: ${e.message}`);
      return;
    }

    if (!current.length || !previous.length) {
      this.addFinding('OK', 'PricingStrategist: недостаточно данных для сравнения кварталов');
      return;
    }

    const prevMap = new Map(previous.map(r => [r.event_type, r.avg_budget]));
    let hasDrops = false;

    for (const curr of current) {
      const prev = prevMap.get(curr.event_type);
      if (!prev || prev <= 0 || curr.avg_budget <= 0) continue;

      const drop = (prev - curr.avg_budget) / prev;
      if (drop > 0.15) {
        hasDrops = true;
        this.addFinding(
          'HIGH',
          `🎯 Категория "${curr.event_type}": средний бюджет упал на ${Math.round(drop * 100)}% — с ${Math.round(prev).toLocaleString('ru')} ₽ до ${Math.round(curr.avg_budget).toLocaleString('ru')} ₽ (${curr.cnt} заказов). Требуется ревизия ценообразования.`
        );
        await factoryLog(
          this.name,
          `Budget drop ${Math.round(drop * 100)}% for "${curr.event_type}": ${Math.round(prev)} -> ${Math.round(curr.avg_budget)} RUB`
        );
      }
    }

    if (!hasDrops) {
      this.addFinding('OK', 'PricingStrategist: значительных падений бюджета по категориям не выявлено');
    }
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 4. BudgetPlanner
// ═════════════════════════════════════════════════════════════════════════════
class BudgetPlanner extends Agent {
  constructor() {
    super({
      id: 'fin-04',
      name: 'BudgetPlanner',
      organ: 'Finance Department',
      emoji: '📋',
      focus: 'Summarise pipeline value and flag excess unprocessed pending orders',
    });
  }

  async analyze() {
    let pipeline;
    try {
      pipeline = await dbAll(
        `SELECT status,
                COUNT(*) as cnt,
                SUM(${BUDGET_SQL_EXPR}) as total_budget
         FROM orders
         WHERE status IN ('new', 'confirmed')
         GROUP BY status`
      );
    } catch (e) {
      this.addFinding('HIGH', `BudgetPlanner: ошибка запроса: ${e.message}`);
      return;
    }

    const newRow = pipeline.find(r => r.status === 'new') || { cnt: 0, total_budget: 0 };
    const confirmedRow = pipeline.find(r => r.status === 'confirmed') || { cnt: 0, total_budget: 0 };

    const totalCnt = (newRow.cnt || 0) + (confirmedRow.cnt || 0);
    const totalBudget = Math.round((newRow.total_budget || 0) + (confirmedRow.total_budget || 0));

    if (totalCnt === 0) {
      this.addFinding('OK', 'BudgetPlanner: нет активных заявок в пайплайне');
      return;
    }

    this.addFinding(
      'INFO',
      `📋 Пайплайн: ${totalCnt} активных заявок (new: ${newRow.cnt || 0}, confirmed: ${confirmedRow.cnt || 0}), ожидаемая выручка: ~${totalBudget.toLocaleString('ru')} ₽`
    );

    // Check stale new orders older than 3 days
    let staleNew;
    try {
      staleNew = await dbGet(
        `SELECT COUNT(*) as cnt FROM orders WHERE status = 'new' AND created_at < datetime('now', '-3 days')`
      );
    } catch {
      staleNew = { cnt: 0 };
    }

    const staleCnt = staleNew?.cnt || 0;
    if (staleCnt > 5) {
      this.addFinding(
        'MEDIUM',
        `📋 ${staleCnt} заявок в статусе "new" старше 3 дней — нужно обработать pending заявки`
      );
    }

    await factoryLog(
      this.name,
      `Pipeline: ${totalCnt} active orders, estimated ~${totalBudget} RUB. Stale new (>3d): ${staleCnt}`
    );
  }
}

// ─── Run all four agents when invoked directly ────────────────────────────────
async function runFinanceDepartment() {
  console.log('💰 Finance Department — запуск...\n');

  const agents = [new RevenueForecaster(), new CostOptimizer(), new PricingStrategist(), new BudgetPlanner()];

  for (const agent of agents) {
    console.log(`\n${agent.emoji} ${agent.name}`);
    try {
      await agent.run({ silent: true });
      agent.findings.forEach(f => console.log(`  ${f.sev} ${f.msg}`));
      agent.fixed.forEach(fx => console.log(`  🔧 ${fx}`));
    } catch (e) {
      console.error(`  ❌ Error: ${e.message}`);
    }
  }

  console.log('\n💰 Finance Department — завершено.');
}

if (require.main === module) runFinanceDepartment().then(() => process.exit(0));

module.exports = { RevenueForecaster, CostOptimizer, PricingStrategist, BudgetPlanner };
