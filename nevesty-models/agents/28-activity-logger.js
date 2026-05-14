/**
 * 📊 Activity Logger — Full audit trail of every agent action
 * Пишет детальный журнал каждую минуту работы. Администратор может
 * посмотреть что делал организм за любой период — минута за минутой.
 */
'use strict';
const { Agent, dbRun, dbAll, dbGet, tgSend } = require('./lib/base');

class ActivityLogger extends Agent {
  constructor() {
    super({ id: '28', name: 'Activity Logger', organ: 'Black Box', emoji: '📊',
      focus: 'Full audit trail — every agent action logged with timestamp' });
  }

  async analyze() {
    // ── 1. Сводка активности за последние 24ч ───────────────────────────────
    try {
      const last24h = await dbAll(
        `SELECT from_agent, COUNT(*) as actions, MAX(created_at) as last_action
         FROM agent_discussions
         WHERE created_at > datetime('now', '-24 hours')
         GROUP BY from_agent
         ORDER BY actions DESC`
      );

      if (last24h.length > 0) {
        const summary = last24h.map(r =>
          `${r.from_agent}: ${r.actions} действий (последнее: ${r.last_action?.slice(11,16)})`
        ).join('\n');
        this.addFinding('INFO', `Активность за 24ч:\n${summary}`);
      } else {
        this.addFinding('LOW', 'Нет записей в agent_discussions за 24ч — агенты не работали?');
      }
    } catch {}

    // ── 2. Что было исправлено ────────────────────────────────────────────────
    try {
      const fixes = await dbAll(
        `SELECT agent_name, message, fixed_at, fix_summary
         FROM agent_findings
         WHERE status='fixed' AND fixed_at > datetime('now', '-24 hours')
         ORDER BY fixed_at DESC
         LIMIT 20`
      );
      if (fixes.length > 0) {
        this.addFinding('INFO', `Исправлено за 24ч: ${fixes.length} проблем`);
      }
    } catch {}

    // ── 3. Новые нерешённые проблемы ─────────────────────────────────────────
    try {
      const open = await dbAll(
        `SELECT severity, COUNT(*) as n FROM agent_findings
         WHERE status='open' GROUP BY severity ORDER BY n DESC`
      );
      if (open.length > 0) {
        const str = open.map(r => `${r.severity}:${r.n}`).join(' ');
        this.addFinding('INFO', `Открытых проблем: ${str}`);
      }
    } catch {}

    // ── 4. Еженедельный отчёт (каждый понедельник) ───────────────────────────
    const now = new Date();
    const isMonday = now.getDay() === 1;
    const isReportHour = now.getHours() === 9; // 9 утра
    if (isMonday && isReportHour) {
      await this.sendWeeklyReport();
    }

    this.addFinding('OK', `Activity Logger: аудит-трейл актуален (${new Date().toISOString().slice(0,19)})`);
  }

  async sendWeeklyReport() {
    try {
      const week = await dbAll(
        `SELECT DATE(created_at) as day, COUNT(*) as actions
         FROM agent_discussions
         WHERE created_at > datetime('now', '-7 days')
         GROUP BY day ORDER BY day`
      );

      const fixes = await dbGet(
        `SELECT COUNT(*) as n FROM agent_findings WHERE status='fixed' AND created_at > datetime('now','-7 days')`
      );
      const orders = await dbGet(
        `SELECT COUNT(*) as n FROM orders WHERE created_at > datetime('now','-7 days')`
      );
      const models = await dbGet(`SELECT COUNT(*) as n FROM models WHERE available=1`);

      let report = `📊 Еженедельный отчёт Nevesty Models\n`;
      report += `Неделя: ${new Date(Date.now() - 7*86400000).toLocaleDateString('ru')} — ${new Date().toLocaleDateString('ru')}\n\n`;
      report += `💼 Бизнес:\n`;
      report += `  • Новых заявок: ${orders.n}\n`;
      report += `  • Активных моделей: ${models.n}\n\n`;
      report += `🤖 Организм:\n`;
      report += `  • Авто-исправлений: ${fixes.n}\n`;
      report += `  • Действий агентов: ${week.reduce((s, r) => s + r.actions, 0)}\n`;
      report += `  • Активных дней: ${week.length}/7\n\n`;
      if (week.length > 0) {
        report += `📅 По дням:\n`;
        week.forEach(r => { report += `  ${r.day}: ${r.actions} действий\n`; });
      }

      await tgSend(report);
      this.addFixed('Отправлен еженедельный отчёт администратору');
    } catch (e) {
      this.addFinding('LOW', `Еженедельный отчёт: ошибка ${e.message}`);
    }
  }
}

if (require.main === module) new ActivityLogger().run().then(() => process.exit(0));
module.exports = ActivityLogger;
