/**
 * 💰 Sales Analyst — Revenue & Growth
 * Анализирует воронку продаж, находит узкие места, автоматически улучшает
 * тексты кнопок, FAQ и настройки для повышения конверсии.
 */
'use strict';
const { Agent, dbRun, dbAll, dbGet, readFile, BOT_PATH } = require('./lib/base');
const path = require('path');
const fs   = require('fs');

const SETTINGS_PATH = path.join(__dirname, '../data.db');

class SalesAnalyst extends Agent {
  constructor() {
    super({ id: '26', name: 'Sales Analyst', organ: 'Revenue Engine', emoji: '💰',
      focus: 'Conversion funnel, order trends, pricing, CTA optimization' });
  }

  async analyze() {
    // ── 1. Воронка: сколько сессий начали бронирование vs завершили ──────────
    try {
      const total   = await dbGet('SELECT COUNT(*) as n FROM orders');
      const week    = await dbGet("SELECT COUNT(*) as n FROM orders WHERE created_at > datetime('now','-7 days')");
      const month   = await dbGet("SELECT COUNT(*) as n FROM orders WHERE created_at > datetime('now','-30 days')");
      const newOrd  = await dbGet("SELECT COUNT(*) as n FROM orders WHERE status='new'");
      const done    = await dbGet("SELECT COUNT(*) as n FROM orders WHERE status IN ('confirmed','completed')");

      const convRate = total.n > 0 ? Math.round((done.n / total.n) * 100) : 0;

      if (convRate < 30 && total.n > 5) {
        this.addFinding('HIGH', `Конверсия ${convRate}% — только ${done.n} из ${total.n} заявок подтверждено. Нужно ускорить обработку.`);
      } else {
        this.addFinding('OK', `Конверсия ${convRate}% (${done.n}/${total.n} заявок подтверждено)`);
      }

      if (week.n === 0 && month.n > 0) {
        this.addFinding('MEDIUM', 'Нет новых заявок за 7 дней — рекомендую обновить приветствие и запустить акцию');
        await this.suggestGreetingUpdate();
      }

      this.addFinding('INFO', `Заявок: всего ${total.n}, за неделю ${week.n}, за месяц ${month.n}`);
    } catch (e) { this.addFinding('LOW', `Воронка: ${e.message}`); }

    // ── 2. Популярные типы мероприятий ───────────────────────────────────────
    try {
      const byType = await dbAll(
        `SELECT event_type, COUNT(*) as cnt FROM orders GROUP BY event_type ORDER BY cnt DESC LIMIT 5`
      );
      if (byType.length > 0) {
        const top = byType[0];
        this.addFinding('INFO', `Топ тип мероприятий: ${top.event_type} (${top.cnt} заявок)`);
        // Если явный лидер — убеждаемся что он первым идёт в кнопках (audit)
        const src = readFile(BOT_PATH);
        if (top.event_type === 'photo_shoot' && !src.includes("'photo_shoot'")) {
          this.addFinding('MEDIUM', 'photo_shoot самый популярный — проверь что он есть в EVENT_TYPES');
        }
      }
    } catch {}

    // ── 3. Заброшенные заявки (status=new > 48ч) ─────────────────────────────
    try {
      const abandoned = await dbAll(
        `SELECT id, client_name, client_chat_id, order_number FROM orders
         WHERE status='new' AND created_at < datetime('now', '-48 hours')
         LIMIT 10`
      );
      if (abandoned.length > 0) {
        this.addFinding('MEDIUM', `${abandoned.length} заявок висят в статусе NEW > 48ч — нужна реакция менеджера`);
        await this.notifyAbandonedOrders(abandoned);
      } else {
        this.addFinding('OK', 'Заброшенных заявок (>48ч) нет');
      }
    } catch {}

    // ── 4. Модели без заявок (плохо продаются) ───────────────────────────────
    try {
      const noOrders = await dbAll(
        `SELECT m.id, m.name FROM models m
         WHERE m.available=1
           AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.model_id=m.id)
         LIMIT 5`
      );
      if (noOrders.length > 0) {
        const names = noOrders.map(m => m.name).join(', ');
        this.addFinding('LOW', `Модели без заявок: ${names} — рассмотри улучшение их профилей`);
      } else {
        this.addFinding('OK', 'Все модели получали заявки');
      }
    } catch {}

    // ── 5. Проверяем прайс-лист — он обновлялся? ─────────────────────────────
    try {
      const pricing = await dbGet("SELECT value, updated_at FROM bot_settings WHERE key='pricing'");
      if (pricing) {
        const daysSince = pricing.updated_at
          ? Math.floor((Date.now() - new Date(pricing.updated_at).getTime()) / 86400000)
          : 999;
        if (daysSince > 30) {
          this.addFinding('LOW', `Прайс-лист не обновлялся ${daysSince} дней — проверь актуальность цен`);
        } else {
          this.addFinding('OK', `Прайс обновлён ${daysSince}д назад`);
        }
      }
    } catch {}
  }

  async suggestGreetingUpdate() {
    try {
      const greetings = [
        'Добро пожаловать в Nevesty Models — топовые модели для ваших проектов! 🌟',
        'Nevesty Models: профессиональные модели для fashion, коммерции и мероприятий. Запишитесь сегодня!',
        'Привет! Я бот агентства Nevesty Models. Найдём идеальную модель для вашего проекта за 24 часа.',
      ];
      const current = await dbGet("SELECT value FROM bot_settings WHERE key='greeting'");
      const idx = Math.floor(Date.now() / (7 * 86400000)) % greetings.length; // rotate weekly
      const newGreeting = greetings[idx];
      if (current?.value !== newGreeting) {
        await dbRun(
          "UPDATE bot_settings SET value=?, updated_at=CURRENT_TIMESTAMP WHERE key='greeting'",
          [newGreeting]
        );
        this.addFixed(`Приветствие обновлено для повышения конверсии`);
      }
    } catch {}
  }

  async notifyAbandonedOrders(orders) {
    // Записываем в agent_discussions чтобы тимлид знал
    const names = orders.map(o => `${o.order_number} (${o.client_name})`).join(', ');
    await dbRun(
      `INSERT INTO agent_discussions (from_agent, to_agent, topic, message) VALUES (?,?,?,?)`,
      ['Sales Analyst', 'Admin', '⚠️ Заброшенные заявки',
       `${orders.length} заявок не обработаны > 48ч: ${names}. Срочно нужна реакция менеджера!`]
    ).catch(() => {});
  }
}

if (require.main === module) new SalesAnalyst().run().then(() => process.exit(0));
module.exports = SalesAnalyst;
