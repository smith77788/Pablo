/**
 * 💚 Customer Success Department — Onboarding, retention, feedback, upsell
 *
 * Agents:
 *   OnboardingSpecialist — flags new clients with stale first orders
 *   RetentionAnalyst     — identifies lapsed clients and generates re-engagement offers
 *   FeedbackCollector    — lists clients due for review invitation
 *   UpsellAdvisor        — analyses top models and suggests upsell ideas via Claude
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

const { Agent, _dbRun, dbAll, logAgent } = require('../lib/base');

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
// 1. OnboardingSpecialist
// ═════════════════════════════════════════════════════════════════════════════
class OnboardingSpecialist extends Agent {
  constructor() {
    super({
      id: 'cs-01',
      name: 'OnboardingSpecialist',
      organ: 'Customer Success',
      emoji: '👋',
      focus: 'Detect new clients whose first order is stalled and suggest welcome messages',
    });
  }

  async analyze() {
    let orders;
    try {
      orders = await dbAll(
        `SELECT id, order_number, client_chat_id, client_name, client_phone, event_type, created_at
         FROM orders
         WHERE status = 'new'
           AND created_at > datetime('now', '-7 days')
           AND created_at < datetime('now', '-1 day')
         ORDER BY created_at ASC
         LIMIT 30`
      );
    } catch (e) {
      this.addFinding('HIGH', `OnboardingSpecialist: не удалось загрузить заявки: ${e.message}`);
      return;
    }

    if (!orders.length) {
      this.addFinding('OK', 'Нет зависших новых заявок за последние 7 дней');
      return;
    }

    // Filter to first-time clients (no older orders from same chat_id)
    const firstTimers = [];
    for (const order of orders) {
      if (!order.client_chat_id) continue;
      try {
        const prev = await dbAll(`SELECT id FROM orders WHERE client_chat_id = ? AND created_at < ? LIMIT 1`, [
          order.client_chat_id,
          order.created_at,
        ]);
        if (!prev.length) firstTimers.push(order);
      } catch {
        firstTimers.push(order); // Include if check fails
      }
    }

    if (!firstTimers.length) {
      this.addFinding('OK', `${orders.length} зависших заявок, но все от повторных клиентов`);
      return;
    }

    const list = firstTimers
      .map(o => `#${o.order_number} (${o.client_name || 'неизвестно'}, ${o.event_type || 'тип не указан'})`)
      .join(', ');

    this.addFinding(
      'MEDIUM',
      `👋 ${firstTimers.length} новых клиентов с зависшей первой заявкой > 24ч: ${list}. Рекомендуется отправить приветственное сообщение.`
    );

    for (const order of firstTimers) {
      await factoryLog(
        this.name,
        `New client stalled: order #${order.order_number} (${order.client_name}), created ${order.created_at}`
      );
    }
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 2. RetentionAnalyst
// ═════════════════════════════════════════════════════════════════════════════
class RetentionAnalyst extends Agent {
  constructor() {
    super({
      id: 'cs-02',
      name: 'RetentionAnalyst',
      organ: 'Customer Success',
      emoji: '🔄',
      focus: 'Find lapsed clients and generate personalised re-engagement offers via Claude',
    });
  }

  async analyze() {
    let lapsed;
    try {
      lapsed = await dbAll(
        `SELECT client_chat_id, COUNT(*) as cnt, MAX(created_at) as last
         FROM orders
         WHERE status = 'completed'
         GROUP BY client_chat_id
         HAVING last < datetime('now', '-60 days')
         LIMIT 20`
      );
    } catch (e) {
      this.addFinding('HIGH', `RetentionAnalyst: ошибка запроса: ${e.message}`);
      return;
    }

    if (!lapsed.length) {
      this.addFinding('OK', 'Нет клиентов без заказов более 60 дней');
      return;
    }

    this.addFinding('MEDIUM', `🔄 ${lapsed.length} клиентов не делали заказов более 60 дней — потенциальный отток`);

    // Generate personalised offer for first lapsed client via Claude
    if (process.env.ANTHROPIC_API_KEY && lapsed.length > 0) {
      const sample = lapsed[0];
      try {
        // Load last order details for personalisation
        const lastOrder = await dbAll(
          `SELECT event_type, location FROM orders WHERE client_chat_id = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1`,
          [sample.client_chat_id]
        ).catch(() => []);

        const context = lastOrder[0]
          ? `Последний заказ: ${lastOrder[0].event_type || 'событие'} в ${lastOrder[0].location || 'городе'}`
          : `Всего заказов: ${sample.cnt}`;

        const offer = await callClaude({
          systemPrompt: [
            'Ты — менеджер по работе с клиентами модельного агентства Nevesty Models.',
            'Напиши короткое персонализированное предложение для возврата клиента (2-3 предложения, без имени).',
            'Тон: тёплый, ненавязчивый, без агрессивных скидок.',
            'Упомяни, что появились новые модели.',
          ].join(' '),
          userPrompt: `${context}. Клиент не заказывал более 60 дней.`,
          maxTokens: 200,
        });

        this.addFinding('LOW', `💡 Пример предложения для возврата клиента: ${offer.slice(0, 200)}`);

        await factoryLog(this.name, `Generated re-engagement offer for ${sample.client_chat_id}`);
      } catch (e) {
        await factoryLog(this.name, `Claude offer generation failed: ${e.message}`);
      }
    }

    await factoryLog(this.name, `Found ${lapsed.length} lapsed clients (no orders 60+ days)`);
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 3. FeedbackCollector
// ═════════════════════════════════════════════════════════════════════════════
class FeedbackCollector extends Agent {
  constructor() {
    super({
      id: 'cs-03',
      name: 'FeedbackCollector',
      organ: 'Customer Success',
      emoji: '⭐',
      focus: 'Identify clients due for review invitation (no send — recommendations only)',
    });
  }

  async analyze() {
    let candidates;
    try {
      candidates = await dbAll(
        `SELECT o.id, o.order_number, o.client_chat_id, o.client_name,
                o.completed_at, o.updated_at, o.review_invitation_sent_at
         FROM orders o
         LEFT JOIN reviews r ON r.order_id = o.id
         WHERE o.status = 'completed'
           AND o.review_invitation_sent_at IS NULL
           AND r.id IS NULL
           AND datetime(COALESCE(o.completed_at, o.updated_at), '+48 hours') <= datetime('now')
         ORDER BY COALESCE(o.completed_at, o.updated_at) ASC
         LIMIT 30`
      );
    } catch (e) {
      this.addFinding('HIGH', `FeedbackCollector: ошибка запроса: ${e.message}`);
      return;
    }

    if (!candidates.length) {
      this.addFinding('OK', 'Нет клиентов, ожидающих приглашения оставить отзыв');
      return;
    }

    const list = candidates
      .slice(0, 10)
      .map(o => `#${o.order_number} (${o.client_name || o.client_chat_id})`)
      .join(', ');

    this.addFinding(
      'LOW',
      `⭐ ${candidates.length} клиентов готовы к приглашению оставить отзыв (48+ ч после завершения): ${list}`
    );

    await factoryLog(this.name, `${candidates.length} clients pending review invitation: ${list}`);
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 4. UpsellAdvisor
// ═════════════════════════════════════════════════════════════════════════════
class UpsellAdvisor extends Agent {
  constructor() {
    super({
      id: 'cs-04',
      name: 'UpsellAdvisor',
      organ: 'Customer Success',
      emoji: '📈',
      focus: 'Analyse top models and generate upsell ideas via Claude',
    });
  }

  async analyze() {
    // Top-3 models by order count
    let topModels;
    try {
      topModels = await dbAll(
        `SELECT m.id, m.name, m.age, m.height, m.category,
                COUNT(o.id) as order_count,
                AVG(CAST(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(o.budget,'0'),'₽',''),'руб',''),' ',''),',','.') AS REAL)) as avg_budget
         FROM models m
         JOIN orders o ON o.model_id = m.id
         WHERE o.status IN ('confirmed', 'completed')
         GROUP BY m.id
         ORDER BY order_count DESC
         LIMIT 3`
      );
    } catch (e) {
      this.addFinding('HIGH', `UpsellAdvisor: ошибка загрузки моделей: ${e.message}`);
      return;
    }

    if (!topModels.length) {
      this.addFinding('OK', 'Недостаточно данных для анализа апселлинга');
      return;
    }

    if (!process.env.ANTHROPIC_API_KEY) {
      const names = topModels.map(m => `${m.name} (${m.order_count} заказов)`).join(', ');
      this.addFinding('LOW', `📈 Топ модели: ${names}. ANTHROPIC_API_KEY не задан — идеи апселла не генерируются.`);
      return;
    }

    try {
      const modelList = topModels
        .map(
          m =>
            `• ${m.name}, ${m.age || '?'} лет, ${m.height || '?'} см, ${m.category || 'категория не указана'}, заказов: ${m.order_count}, средний бюджет: ${Math.round(m.avg_budget || 0)} ₽`
        )
        .join('\n');

      const ideas = await callClaude({
        systemPrompt: [
          'Ты — менеджер по развитию модельного агентства Nevesty Models.',
          'Предложи 1-2 конкретных идеи апселлинга на основе данных о топ-моделях.',
          'Примеры: добавить вторую модель к пакету, предложить видеосъёмку к фото, пакет "свадьба + репетиция".',
          'Краткий формат: одна идея — одна строка. Без вступлений.',
        ].join(' '),
        userPrompt: `Топ-3 модели по заказам:\n${modelList}`,
        maxTokens: 200,
      });

      this.addFinding('LOW', `📈 Идеи апселлинга на основе топ-моделей: ${ideas.slice(0, 300)}`);
      await factoryLog(this.name, `Upsell ideas generated for top ${topModels.length} models`);
    } catch (e) {
      this.addFinding('LOW', `UpsellAdvisor: не удалось сгенерировать идеи: ${e.message}`);
    }
  }
}

// ─── Run all four agents when invoked directly ────────────────────────────────
async function runCustomerSuccessDepartment() {
  console.log('💚 Customer Success Department — запуск...\n');

  const agents = [new OnboardingSpecialist(), new RetentionAnalyst(), new FeedbackCollector(), new UpsellAdvisor()];

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

  console.log('\n💚 Customer Success Department — завершено.');
}

if (require.main === module) runCustomerSuccessDepartment().then(() => process.exit(0));

module.exports = { OnboardingSpecialist, RetentionAnalyst, FeedbackCollector, UpsellAdvisor };
