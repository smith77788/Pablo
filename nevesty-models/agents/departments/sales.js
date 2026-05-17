/**
 * 💼 Sales Department — Lead qualification, proposals, follow-ups
 *
 * Agents:
 *   LeadQualifier      — scores incoming orders by budget, event type, city
 *   ProposalWriter     — generates a personalised offer via Claude API
 *   FollowUpSpecialist — flags stale orders that need a nudge
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

const { Agent, dbRun, dbAll, logAgent } = require('../lib/base');

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

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Write result to agent_logs + console */
async function factoryLog(agentName, message) {
  console.log(`[${agentName}] ${message}`);
  await logAgent(agentName, message);
}

// ═════════════════════════════════════════════════════════════════════════════
// 1. LeadQualifier
// ═════════════════════════════════════════════════════════════════════════════
class LeadQualifier extends Agent {
  constructor() {
    super({
      id: 'sales-01',
      name: 'LeadQualifier',
      organ: 'Sales Department',
      emoji: '🔍',
      focus: 'Score incoming orders by budget, event type, location',
    });
  }

  /**
   * Score a single order row.
   * Returns { score: 0-100, tier: 'hot'|'warm'|'cold', reasons: string[] }
   */
  scoreOrder(order) {
    const reasons = [];
    let score = 0;

    // ── Budget ────────────────────────────────────────────────────────────────
    const budget = (order.budget || '').toLowerCase();
    if (/[5-9]\d{4,}|[1-9]\d{5,}/.test(budget.replace(/\s/g, ''))) {
      score += 40;
      reasons.push('Высокий бюджет (50 000+ ₽)');
    } else if (/[2-4]\d{4}/.test(budget.replace(/\s/g, ''))) {
      score += 20;
      reasons.push('Средний бюджет (20 000–49 999 ₽)');
    } else if (budget && budget !== 'не указан') {
      score += 10;
      reasons.push('Бюджет указан');
    }

    // ── Event type ────────────────────────────────────────────────────────────
    const highValueEvents = ['commercial', 'runway', 'fashion_show', 'advertising', 'magazine'];
    const midValueEvents = ['photo_shoot', 'video', 'corporate', 'exhibition'];
    const evType = (order.event_type || '').toLowerCase();
    if (highValueEvents.some(t => evType.includes(t))) {
      score += 35;
      reasons.push('Коммерческое / рекламное мероприятие');
    } else if (midValueEvents.some(t => evType.includes(t))) {
      score += 20;
      reasons.push('Фотосъёмка / видео / корпоратив');
    } else if (evType) {
      score += 10;
      reasons.push(`Тип: ${order.event_type}`);
    }

    // ── City / location ───────────────────────────────────────────────────────
    const loc = (order.location || '').toLowerCase();
    if (/москва|спб|санкт/.test(loc)) {
      score += 15;
      reasons.push('Крупный город (Москва / СПб)');
    } else if (loc && loc.length > 2) {
      score += 8;
      reasons.push(`Город: ${order.location}`);
    }

    // ── Completeness bonus ────────────────────────────────────────────────────
    const filled = ['client_name', 'client_phone', 'event_date', 'comments'].filter(
      k => order[k] && String(order[k]).trim().length > 0
    ).length;
    score += filled * 2;
    if (filled === 4) reasons.push('Заявка заполнена полностью');

    const tier = score >= 70 ? 'hot' : score >= 40 ? 'warm' : 'cold';
    return { score: Math.min(score, 100), tier, reasons };
  }

  async analyze() {
    // Qualify new orders from the last 7 days
    let orders;
    try {
      orders = await dbAll(
        `SELECT id, order_number, client_name, event_type, location, budget,
                client_phone, event_date, comments, created_at
         FROM orders
         WHERE status = 'new'
           AND created_at > datetime('now', '-7 days')
         ORDER BY created_at DESC
         LIMIT 50`
      );
    } catch (e) {
      this.addFinding('HIGH', `LeadQualifier: не удалось загрузить заявки: ${e.message}`);
      return;
    }

    if (!orders.length) {
      this.addFinding('OK', 'Нет новых заявок за последние 7 дней');
      return;
    }

    const results = [];
    for (const order of orders) {
      const { score, tier, reasons } = this.scoreOrder(order);
      results.push({ order, score, tier, reasons });

      // Persist score as a note so managers can filter
      await dbRun(
        `UPDATE orders
         SET admin_notes = CASE
               WHEN admin_notes IS NULL OR admin_notes = '' THEN ?
               ELSE admin_notes || char(10) || ?
             END,
             updated_at = CURRENT_TIMESTAMP
         WHERE id = ?`,
        [
          `[LeadScore] ${tier.toUpperCase()} ${score}/100: ${reasons.join('; ')}`,
          `[LeadScore] ${tier.toUpperCase()} ${score}/100: ${reasons.join('; ')}`,
          order.id,
        ]
      ).catch(() => {});

      await factoryLog(
        this.name,
        `Order #${order.order_number} → ${tier.toUpperCase()} (${score}/100): ${reasons.join(', ')}`
      );
    }

    const hot = results.filter(r => r.tier === 'hot').length;
    const warm = results.filter(r => r.tier === 'warm').length;
    const cold = results.filter(r => r.tier === 'cold').length;

    this.addFinding('INFO', `Квалифицировано заявок: 🔥 горячих ${hot}, 🌡 тёплых ${warm}, 🧊 холодных ${cold}`);

    if (hot > 0) {
      const hotOrders = results
        .filter(r => r.tier === 'hot')
        .map(r => `#${r.order.order_number} (${r.order.client_name}, ${r.score}/100)`)
        .join(', ');
      this.addFinding('HIGH', `🔥 Горячие лиды требуют немедленной обработки: ${hotOrders}`);
    }

    return results;
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 2. ProposalWriter
// ═════════════════════════════════════════════════════════════════════════════
class ProposalWriter extends Agent {
  constructor() {
    super({
      id: 'sales-02',
      name: 'ProposalWriter',
      organ: 'Sales Department',
      emoji: '✍️',
      focus: 'Generate personalised client proposals using Claude API',
    });
  }

  /**
   * Build a proposal for a single order.
   * @param {object} order  — row from orders table
   * @param {object[]} [models] — available models from DB (optional)
   * @returns {string} proposal text
   */
  async buildProposal(order, models = []) {
    const modelList = models.length
      ? models
          .slice(0, 5)
          .map(m => `• ${m.name}, ${m.age} лет, ${m.height} см, категория: ${m.category}`)
          .join('\n')
      : 'Каталог моделей доступен на сайте агентства.';

    const userPrompt = [
      `Клиент: ${order.client_name}`,
      `Тип мероприятия: ${order.event_type}`,
      order.event_date ? `Дата: ${order.event_date}` : null,
      order.location ? `Город: ${order.location}` : null,
      order.budget ? `Бюджет: ${order.budget}` : null,
      order.comments ? `Комментарий: ${order.comments}` : null,
      '',
      'Доступные модели:',
      modelList,
    ]
      .filter(l => l !== null)
      .join('\n');

    const text = await callClaude({
      systemPrompt: [
        'Ты — менеджер модельного агентства Nevesty Models.',
        'Напиши персонализированное коммерческое предложение клиенту на русском языке.',
        'Стиль: профессиональный, тёплый, убедительный. Длина: 150-250 слов.',
        'Включи: приветствие по имени, краткий анализ запроса, 1-2 рекомендованные модели (если есть), следующий шаг.',
        'Не используй шаблонные фразы вроде "Уважаемый клиент".',
      ].join(' '),
      userPrompt,
      maxTokens: 400,
    });

    return text;
  }

  async analyze() {
    // Find new orders that don't yet have a proposal in admin_notes
    let orders;
    try {
      orders = await dbAll(
        `SELECT id, order_number, client_name, event_type, event_date, location,
                budget, comments, model_id
         FROM orders
         WHERE status = 'new'
           AND (admin_notes IS NULL
             OR admin_notes NOT LIKE '%[Proposal]%')
         ORDER BY created_at DESC
         LIMIT 5`
      );
    } catch (e) {
      this.addFinding('HIGH', `ProposalWriter: ошибка загрузки заявок: ${e.message}`);
      return;
    }

    if (!orders.length) {
      this.addFinding('OK', 'Нет заявок, требующих предложений прямо сейчас');
      return;
    }

    // Load available models once
    let models = [];
    try {
      models = await dbAll('SELECT name, age, height, category FROM models WHERE available = 1 ORDER BY id LIMIT 20');
    } catch {}

    let written = 0;
    for (const order of orders) {
      try {
        const proposal = await this.buildProposal(order, models);

        // Save proposal into admin_notes
        await dbRun(
          `UPDATE orders
           SET admin_notes = CASE
                 WHEN admin_notes IS NULL OR admin_notes = '' THEN ?
                 ELSE admin_notes || char(10) || ?
               END,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?`,
          [`[Proposal]\n${proposal}`, `[Proposal]\n${proposal}`, order.id]
        ).catch(() => {});

        await factoryLog(this.name, `Proposal written for order #${order.order_number} (${order.client_name})`);
        written++;
      } catch (e) {
        this.addFinding('MEDIUM', `Не удалось сгенерировать предложение для #${order.order_number}: ${e.message}`);
      }
    }

    if (written > 0) {
      this.addFinding('INFO', `✍️ Сгенерировано ${written} персонализированных предложений`);
      this.addFixed(`Предложения записаны в admin_notes для ${written} заявок`);
    }
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 3. FollowUpSpecialist
// ═════════════════════════════════════════════════════════════════════════════
class FollowUpSpecialist extends Agent {
  constructor() {
    super({
      id: 'sales-03',
      name: 'FollowUpSpecialist',
      organ: 'Sales Department',
      emoji: '⏰',
      focus: 'Detect stale orders and schedule follow-up reminders',
    });
  }

  async analyze() {
    // Tiers: >24h → remind, >72h → urgent, >168h (7d) → escalate
    const tiers = [
      { label: 'escalate', hours: 168, sev: 'HIGH', prefix: '🚨 Эскалация' },
      { label: 'urgent', hours: 72, sev: 'MEDIUM', prefix: '⚠️ Срочный фоллоу-ап' },
      { label: 'remind', hours: 24, sev: 'LOW', prefix: '🔔 Напоминание' },
    ];

    let total = 0;

    for (const tier of tiers) {
      let orders;
      try {
        orders = await dbAll(
          `SELECT id, order_number, client_name, client_phone, event_type,
                  created_at, updated_at
           FROM orders
           WHERE status = 'new'
             AND updated_at < datetime('now', '-${tier.hours} hours')
             AND (admin_notes IS NULL OR admin_notes NOT LIKE '%[FollowUp:${tier.label}]%')
           ORDER BY updated_at ASC
           LIMIT 20`
        );
      } catch (e) {
        this.addFinding('HIGH', `FollowUpSpecialist: ошибка запроса (${tier.label}): ${e.message}`);
        continue;
      }

      if (!orders.length) continue;

      total += orders.length;

      const summary = orders.map(o => `#${o.order_number} (${o.client_name})`).join(', ');

      this.addFinding(tier.sev, `${tier.prefix}: ${orders.length} заявок — ${summary}`);

      // Tag each order so we don't re-alert on next run
      for (const order of orders) {
        await dbRun(
          `UPDATE orders
           SET admin_notes = CASE
                 WHEN admin_notes IS NULL OR admin_notes = '' THEN ?
                 ELSE admin_notes || char(10) || ?
               END,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?`,
          [
            `[FollowUp:${tier.label}] ${new Date().toISOString().slice(0, 16)}`,
            `[FollowUp:${tier.label}] ${new Date().toISOString().slice(0, 16)}`,
            order.id,
          ]
        ).catch(() => {});

        await factoryLog(
          this.name,
          `Follow-up needed [${tier.label}] for order #${order.order_number} (${order.client_name})`
        );
      }

      // Post to agent_discussions so admin dashboard shows it
      await dbRun(
        `INSERT INTO agent_discussions (from_agent, to_agent, topic, message)
         VALUES (?, ?, ?, ?)`,
        [
          this.name,
          'Admin',
          `${tier.prefix}: ${orders.length} заявок`,
          `⏰ FollowUpSpecialist: ${orders.length} заявок без ответа > ${tier.hours}ч.\n${summary}`,
        ]
      ).catch(() => {});
    }

    if (total === 0) {
      this.addFinding('OK', 'Все заявки обрабатываются в срок — фоллоу-апы не нужны');
    } else {
      this.addFinding('INFO', `Всего помечено для фоллоу-апа: ${total} заявок`);
    }
  }
}

// ─── Run all three agents when invoked directly ───────────────────────────────
async function runSalesDepartment() {
  console.log('💼 Sales Department — запуск...\n');

  const agents = [new LeadQualifier(), new ProposalWriter(), new FollowUpSpecialist()];

  for (const agent of agents) {
    console.log(`\n${agent.emoji} ${agent.name}`);
    try {
      await agent.run({ silent: true });
      const f = agent.findings;
      f.forEach(fi => console.log(`  ${fi.sev} ${fi.msg}`));
      agent.fixed.forEach(fx => console.log(`  🔧 ${fx}`));
    } catch (e) {
      console.error(`  ❌ Error: ${e.message}`);
    }
  }

  console.log('\n💼 Sales Department — завершено.');
}

if (require.main === module) runSalesDepartment().then(() => process.exit(0));

module.exports = { LeadQualifier, ProposalWriter, FollowUpSpecialist };
