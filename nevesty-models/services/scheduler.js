'use strict';
const { execFile } = require('child_process');
const path = require('path');

let _bot;
let _adminIds;
let _intervals = [];

function init({ db: _db, bot, adminIds }) {
  _bot = bot;
  _adminIds = (adminIds || '').split(',').filter(Boolean);
  void _db; // kept for API compatibility
}

function _notify(msg) {
  if (!_bot || !_adminIds.length) return;
  for (const id of _adminIds) {
    _bot.sendMessage(id, msg).catch(() => {});
  }
  void msg;
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

async function checkFactoryStaleness() {
  try {
    const fs = require('fs');
    const { get } = require('../database');

    // Primary: check factory/.last_run file (written by cycle.py)
    const lastRunFile = require('path').join(__dirname, '../../factory/.last_run');
    let lastRun = null;

    if (fs.existsSync(lastRunFile)) {
      try {
        const ts = fs.readFileSync(lastRunFile, 'utf8').trim();
        lastRun = new Date(ts);
        if (isNaN(lastRun.getTime())) lastRun = null;
      } catch (_) {}
    }

    // Fallback: check bot_settings DB record
    if (!lastRun) {
      const row = await get("SELECT value FROM bot_settings WHERE key = 'factory_last_cycle'", []);
      if (row?.value) lastRun = new Date(row.value);
    }

    if (!lastRun) return;

    const hoursSince = (Date.now() - lastRun.getTime()) / (1000 * 60 * 60);
    if (hoursSince > 12) {
      const h = Math.round(hoursSince);
      const lastStr = lastRun.toISOString().slice(0, 16).replace('T', ' ');
      const msg = `⚠️ Factory Alert: последний цикл был ${h}ч назад (${lastStr}). Проверьте factory/cycle.py`;
      console.warn(`[scheduler] ${msg}`);
      _notify(msg);
    }
  } catch (e) {
    console.error('[scheduler] checkFactoryStaleness error:', e.message);
  }
}

// ─── Bot watchdog: check every 5 minutes ─────────────────────────────────────
let _botDownSince = null;
let _botAlertSent = false;

async function checkBotHealth() {
  if (!_bot) return; // bot not configured
  try {
    await _bot.getMe();
    // Bot is responsive — reset watchdog
    if (_botDownSince !== null) {
      const downMin = Math.round((Date.now() - _botDownSince) / 60000);
      console.log(`[scheduler] Bot recovered after ${downMin} min downtime`);
      _notify(`✅ Telegram бот восстановился после ${downMin} хв простою`);
    }
    _botDownSince = null;
    _botAlertSent = false;
  } catch (e) {
    if (_botDownSince === null) {
      _botDownSince = Date.now();
      _botAlertSent = false;
      console.warn('[scheduler] Bot getMe() failed — watchdog started:', e.message);
    } else {
      const downMin = (Date.now() - _botDownSince) / 60000;
      if (downMin >= 5 && !_botAlertSent) {
        _botAlertSent = true;
        const msg = `🚨 Telegram бот недоступний > 5 хвилин! Перевірте polling. Помилка: ${e.message}`;
        console.error(`[scheduler] ${msg}`);
        _notify(msg);
      }
    }
  }
}

async function runVacuum() {
  try {
    const { run } = require('../database');
    await run('PRAGMA wal_checkpoint(TRUNCATE)');
    await run('VACUUM');
    console.log('[scheduler] Weekly VACUUM + WAL checkpoint completed');
  } catch (e) {
    console.error('[scheduler] VACUUM error:', e.message);
  }
}

async function runWalCheckpoint() {
  try {
    const { run } = require('../database');
    await run('PRAGMA wal_checkpoint(PASSIVE)');
  } catch (e) {
    console.error('[scheduler] WAL checkpoint error:', e.message);
  }
}

function runBackup() {
  const script = path.join(__dirname, '../scripts/backup.sh');
  execFile('bash', [script], { timeout: 60000 }, (err, stdout) => {
    if (err) {
      console.error('[scheduler] Backup error:', err.message);
    } else {
      console.log('[scheduler] Backup done:', stdout.trim().split('\n').pop());
    }
  });
}

function runVacuumScript() {
  const script = path.join(__dirname, '../scripts/vacuum-db.sh');
  execFile('bash', [script], { timeout: 120000 }, (err, stdout) => {
    if (err) {
      console.error('[scheduler] Vacuum script error:', err.message);
    } else {
      console.log('[scheduler] Vacuum script done:', stdout.trim().split('\n').pop());
    }
  });
}

function _scheduleOnce(fn, delayMs, name) {
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

// Schedule a task every N hours
function scheduleEvery(fn, name, intervalHours) {
  const intervalMs = intervalHours * 60 * 60 * 1000;
  const timer = setInterval(() => {
    console.log(`[scheduler] Running: ${name}`);
    fn();
  }, intervalMs);
  if (timer.unref) timer.unref();
  _intervals.push(timer);
  console.log(`[scheduler] ${name} scheduled every ${intervalHours}h`);
}

// Schedule a task every N minutes
function scheduleEveryMinutes(fn, name, intervalMinutes) {
  const intervalMs = intervalMinutes * 60 * 1000;
  const timer = setInterval(() => {
    fn();
  }, intervalMs);
  if (timer.unref) timer.unref();
  _intervals.push(timer);
  console.log(`[scheduler] ${name} scheduled every ${intervalMinutes}min`);
}

function start() {
  // DB backup every 6 hours (keeps last 28 = 7 days of backups)
  scheduleEvery(runBackup, 'DB backup (every 6h)', 6);
  scheduleWeekly(runVacuum, 'SQLite VACUUM + WAL TRUNCATE', 0, 3, 0); // Sunday 03:00
  // Also run vacuum via shell script weekly (Sunday 03:30) for additional WAL cleanup
  scheduleWeekly(runVacuumScript, 'SQLite VACUUM shell script', 0, 3, 30); // Sunday 03:30
  scheduleEvery(runWalCheckpoint, 'WAL checkpoint (PASSIVE)', 6);
  scheduleDaily(sendEventReminders, 'Event reminders', 9, 0);
  scheduleEvery(checkFactoryStaleness, 'Factory staleness check', 6);
  // Additional 30-min factory staleness check (faster detection)
  scheduleEveryMinutes(checkFactoryStaleness, 'Factory staleness check (30min)', 30);
  // Bot watchdog: check bot polling every 5 minutes
  scheduleEveryMinutes(checkBotHealth, 'Bot watchdog', 5);
  console.log('[scheduler] Started: backup (every 6h), VACUUM (Sunday 03:00 + 03:30), WAL checkpoint (every 6h), event reminders (daily 09:00), factory staleness check (every 6h + 30min), bot watchdog (every 5min)');
}

function stop() {
  _intervals.forEach(t => clearTimeout(t));
  _intervals = [];
}

module.exports = { init, start, stop };
