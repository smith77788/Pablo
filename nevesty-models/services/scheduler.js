'use strict';
const { execFile } = require('child_process');
const path = require('path');

let _db;
let _bot;
let _adminIds;
let _intervals = [];

function init({ db, bot, adminIds }) {
  _db = db;
  _bot = bot;
  _adminIds = (adminIds || '').split(',').filter(Boolean);
}

function notify(msg) {
  if (!_bot || !_adminIds.length) return;
  for (const id of _adminIds) {
    _bot.sendMessage(id, msg).catch(() => {});
  }
}

// ─── Parse simple cron-like schedule ──────────────────────────────────────────
function nextRunMs(hour, minute, dayOfWeek = null) {
  const now = new Date();
  const next = new Date(now);
  next.setSeconds(0, 0);
  next.setMinutes(minute);
  next.setHours(hour);
  if (next <= now) next.setDate(next.getDate() + 1);
  if (dayOfWeek !== null) {
    while (next.getDay() !== dayOfWeek) next.setDate(next.getDate() + 1);
  }
  return next.getTime() - now.getTime();
}

// ─── Tasks ─────────────────────────────────────────────────────────────────────

async function sendEventReminders() {
  try {
    // Lazy require to avoid circular dependency: scheduler → bot → scheduler
    const { query, run, getSetting } = require('../database');

    // Check if admin has disabled event reminders
    const enabled = await getSetting('event_reminders_enabled').catch(() => '1');
    if (enabled === '0') return;

    // Get all confirmed/in_progress orders with event_date in next 3 days
    const orders = await query(`
      SELECT o.*, m.name as model_name
      FROM orders o
      LEFT JOIN models m ON o.model_id = m.id
      WHERE o.status IN ('confirmed', 'in_progress')
        AND o.event_date IS NOT NULL
        AND o.client_chat_id IS NOT NULL
        AND date(o.event_date) BETWEEN date('now', '+1 days') AND date('now', '+3 days')
        AND (o.reminder_sent_at IS NULL OR date(o.reminder_sent_at) < date('now'))
    `);

    if (!orders.length) return;

    // Lazy require bot to avoid circular deps
    const botModule = require('../bot');
    const { get } = require('../database');
    let sent = 0;

    for (const order of orders) {
      try {
        // Check client preference for reminders
        const prefs = await get(
          'SELECT notify_reminders FROM client_prefs WHERE chat_id=?',
          [order.client_chat_id]
        ).catch(() => null);
        // Default is 1 (enabled); skip only if explicitly set to 0
        if (prefs && prefs.notify_reminders === 0) continue;

        const daysLeft = Math.round((new Date(order.event_date) - Date.now()) / 86400000);
        const daysStr = daysLeft === 1 ? '1 день' : daysLeft === 2 ? '2 дня' : '3 дня';

        const text = `🔔 *Напоминание о мероприятии*\n\n` +
          `Заявка \\#${order.order_number}\n` +
          `📅 Мероприятие через *${daysStr}*: ${order.event_date}\n` +
          (order.model_name ? `💃 Модель: ${order.model_name}\n` : '') +
          (order.location ? `📍 Место: ${order.location}\n` : '') +
          `\nЕсли нужна помощь — напишите менеджеру\\.`;

        if (botModule.sendMessageToClient && order.client_chat_id) {
          await botModule.sendMessageToClient(order.client_chat_id, order.order_number, text);
        }

        // Mark as sent
        await run(
          `UPDATE orders SET reminder_sent_at=CURRENT_TIMESTAMP WHERE id=?`,
          [order.id]
        );
        sent++;
      } catch (e) {
        console.error('[scheduler] reminder error for order', order.id, e.message);
      }
    }

    if (sent > 0) console.log(`[scheduler] Sent ${sent} event reminders`);
  } catch (e) {
    console.error('[scheduler] sendEventReminders error:', e.message);
  }
}

async function runVacuum() {
  if (!_db) return;
  try {
    const { run } = _db;
    if (run) {
      await run('VACUUM');
      console.log('[scheduler] VACUUM completed');
    }
  } catch (e) {
    console.error('[scheduler] VACUUM error:', e.message);
  }
}

function runBackup() {
  const script = path.join(__dirname, '../scripts/backup.sh');
  execFile('bash', [script], { timeout: 60000 }, (err, stdout, stderr) => {
    if (err) {
      console.error('[scheduler] Backup error:', err.message);
    } else {
      console.log('[scheduler] Backup done:', stdout.trim().split('\n').pop());
    }
  });
}

function scheduleOnce(fn, delayMs, name) {
  const timer = setTimeout(() => {
    fn();
    // Re-schedule for next occurrence
    scheduleDaily(fn, name);
  }, delayMs);
  _intervals.push(timer);
}

function scheduleDaily(fn, name, hour = 1, minute = 0) {
  const delay = nextRunMs(hour, minute);
  const timer = setTimeout(() => {
    console.log(`[scheduler] Running: ${name}`);
    fn();
    scheduleDaily(fn, name, hour, minute);
  }, delay);
  _intervals.push(timer);
  const h = Math.floor(delay / 3600000);
  const m = Math.floor((delay % 3600000) / 60000);
  console.log(`[scheduler] ${name} scheduled in ${h}h ${m}m`);
}

function scheduleWeekly(fn, name, dayOfWeek, hour = 3, minute = 0) {
  const delay = nextRunMs(hour, minute, dayOfWeek);
  const timer = setTimeout(() => {
    console.log(`[scheduler] Running: ${name}`);
    fn();
    scheduleWeekly(fn, name, dayOfWeek, hour, minute);
  }, delay);
  _intervals.push(timer);
}

function start() {
  scheduleDaily(runBackup, 'DB backup', 1, 0);
  scheduleWeekly(runVacuum, 'SQLite VACUUM', 0, 3, 0); // Sunday 03:00
  scheduleDaily(sendEventReminders, 'Event reminders', 9, 0);
  console.log('[scheduler] Started: backup (daily 01:00), VACUUM (Sunday 03:00), event reminders (daily 09:00)');
}

function stop() {
  _intervals.forEach(t => clearTimeout(t));
  _intervals = [];
}

module.exports = { init, start, stop };
