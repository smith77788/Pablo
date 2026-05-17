'use strict';
const { execFile } = require('child_process');
const path = require('path');
const fs = require('fs');

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
        const prefs = await get('SELECT notify_reminders FROM client_prefs WHERE chat_id=?', [
          order.client_chat_id,
        ]).catch(() => null);
        // Default is 1 (enabled); skip only if explicitly set to 0
        if (prefs && prefs.notify_reminders === 0) continue;

        const daysLeft = Math.round((new Date(order.event_date) - Date.now()) / 86400000);
        const daysStr = daysLeft === 1 ? '1 день' : daysLeft === 2 ? '2 дня' : '3 дня';

        const text =
          `🔔 *Напоминание о мероприятии*\n\n` +
          `Заявка \\#${order.order_number}\n` +
          `📅 Мероприятие через *${daysStr}*: ${order.event_date}\n` +
          (order.model_name ? `💃 Модель: ${order.model_name}\n` : '') +
          (order.location ? `📍 Место: ${order.location}\n` : '') +
          `\nЕсли нужна помощь — напишите менеджеру\\.`;

        if (botModule.sendMessageToClient && order.client_chat_id) {
          await botModule.sendMessageToClient(order.client_chat_id, order.order_number, text);
        }

        // Mark as sent
        await run(`UPDATE orders SET reminder_sent_at=CURRENT_TIMESTAMP WHERE id=?`, [order.id]);
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

// Weekly WAL TRUNCATE checkpoint (Sunday at 4am) — ensures WAL file is fully flushed
async function runWalCheckpointTruncate() {
  try {
    const { run } = require('../database');
    await run('PRAGMA wal_checkpoint(TRUNCATE)', []);
    console.log('[Scheduler] WAL checkpoint (TRUNCATE) complete');
  } catch (e) {
    console.error('[Scheduler] WAL checkpoint (TRUNCATE) failed:', e.message);
  }
}

// WAL file size monitoring — alert + force checkpoint if WAL exceeds 100 MB
function checkWalSize() {
  try {
    const walPath = path.join(__dirname, '..', 'data.db-wal');
    if (!fs.existsSync(walPath)) return;
    const stat = fs.statSync(walPath);
    const sizeMb = stat.size / 1024 / 1024;
    if (sizeMb > 100) {
      const msg = `⚠️ WAL файл БД большой: ${sizeMb.toFixed(1)} МБ. Запускаю checkpoint.`;
      console.warn('[scheduler]', msg);
      _notify(msg);
      const { run } = require('../database');
      run('PRAGMA wal_checkpoint(TRUNCATE)').catch(err => {
        console.error('[scheduler] WAL checkpoint (TRUNCATE) after size alert failed:', err.message);
      });
    }
  } catch (err) {
    /* ignore */
  }
}

// ─── Scheduled Broadcast Processor ───────────────────────────────────────────
async function processScheduledBroadcasts() {
  try {
    const { query, run } = require('../database');

    // Find broadcasts due to be sent (scheduled_at <= now, status = 'pending')
    const pending = await query(
      "SELECT * FROM scheduled_broadcasts WHERE status='pending' AND datetime(scheduled_at) <= datetime('now')"
    ).catch(() => []);

    if (!pending.length) return;

    for (const bcast of pending) {
      // Mark as 'sending' first to prevent duplicate runs
      const updated = await run("UPDATE scheduled_broadcasts SET status='sending' WHERE id=? AND status='pending'", [
        bcast.id,
      ]).catch(() => null);
      // If another process already claimed it (0 rows changed), skip
      if (!updated || updated.changes === 0) continue;

      console.log(`[scheduler] Processing scheduled broadcast #${bcast.id}, segment=${bcast.segment}`);

      try {
        // Build recipient list using same logic as bot.js _getBroadcastClients
        const segment = bcast.segment || 'all';
        let rows = [];
        if (segment === 'completed') {
          rows = await query(
            "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND status='completed'"
          ).catch(() => []);
        } else if (segment === 'active') {
          rows = await query(
            "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND created_at >= datetime('now', '-30 days')"
          ).catch(() => []);
        } else if (segment === 'new') {
          rows = await query(
            "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND client_chat_id NOT IN (SELECT DISTINCT client_chat_id FROM orders WHERE status IN ('confirmed','in_progress','completed') AND client_chat_id IS NOT NULL AND client_chat_id != '')"
          ).catch(() => []);
        } else if (segment && (segment.startsWith('city:') || segment.startsWith('city_'))) {
          const city = segment.startsWith('city:') ? segment.slice(5) : segment.slice(5);
          rows = await query(
            `SELECT DISTINCT o.client_chat_id FROM orders o JOIN models m ON o.model_id = m.id WHERE o.client_chat_id IS NOT NULL AND o.client_chat_id != '' AND m.city = ?`,
            [city]
          ).catch(() => []);
        } else {
          rows = await query(
            "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != ''"
          ).catch(() => []);
        }

        // Filter out admin IDs
        const adminIdList = (_adminIds || []).map(String);
        const recipients = rows.map(r => r.client_chat_id).filter(id => id && !adminIdList.includes(String(id)));

        let sent = 0;
        let errorCount = 0;
        const photoId = bcast.photo_url || null;
        const text = bcast.text || '';
        const caption = text ? `📢 *Сообщение от Nevesty Models*\n\n${text}` : '📢 *Nevesty Models*';

        for (const cid of recipients) {
          try {
            if (photoId) {
              await _bot.sendPhoto(cid, photoId, { caption: caption.slice(0, 1020), parse_mode: 'MarkdownV2' });
            } else {
              await _bot.sendMessage(cid, caption.slice(0, 4096), { parse_mode: 'MarkdownV2' });
            }
            sent++;
          } catch (err) {
            // Handle 429 Too Many Requests
            const retryAfter =
              err?.response?.parameters?.retry_after ||
              (err?.message && /retry after (\d+)/i.test(err.message)
                ? parseInt(err.message.match(/retry after (\d+)/i)[1])
                : null);
            if (retryAfter) {
              await new Promise(r => setTimeout(r, (retryAfter + 1) * 1000));
              try {
                if (photoId) {
                  await _bot.sendPhoto(cid, photoId, { caption: caption.slice(0, 1020), parse_mode: 'MarkdownV2' });
                } else {
                  await _bot.sendMessage(cid, caption.slice(0, 4096), { parse_mode: 'MarkdownV2' });
                }
                sent++;
              } catch {
                errorCount++;
              }
            } else {
              errorCount++;
            }
          }
          // 50ms delay between sends (rate limit safety)
          await new Promise(r => setTimeout(r, 50));
        }

        // Mark as sent with stats
        await run(
          "UPDATE scheduled_broadcasts SET status='sent', sent_count=?, error_count=?, sent_at=datetime('now') WHERE id=?",
          [sent, errorCount, bcast.id]
        ).catch(() => {});

        console.log(
          `[scheduler] Scheduled broadcast #${bcast.id} done: sent=${sent}, errors=${errorCount}, recipients=${recipients.length}`
        );

        // Notify admins about completion
        const segLabels = {
          all: 'Все клиенты',
          completed: 'Завершённые',
          active: 'Активные 30д',
          new: 'Новые',
        };
        const segLabel = segLabels[segment] || (segment.startsWith('city') ? `Город: ${segment.slice(5)}` : segment);
        _notify(
          `📢 Запланированная рассылка #${bcast.id} отправлена!\n✅ ${sent} доставлено, ❌ ${errorCount} ошибок\nАудитория: ${segLabel}`
        );
      } catch (sendErr) {
        console.error(`[scheduler] Scheduled broadcast #${bcast.id} failed:`, sendErr.message);
        await run("UPDATE scheduled_broadcasts SET status='error', error_count=? WHERE id=?", [1, bcast.id]).catch(
          () => {}
        );
        _notify(`❌ Ошибка запланированной рассылки #${bcast.id}: ${sendErr.message}`);
      }
    }
  } catch (e) {
    console.error('[scheduler] processScheduledBroadcasts error:', e.message);
  }
}

async function checkDiskSpace() {
  try {
    const { execSync } = require('child_process');
    const backupDir = path.join(__dirname, '../backups');
    const output = execSync(`du -sb "${backupDir}" 2>/dev/null || echo "0\t"`, { encoding: 'utf8' });
    const bytes = parseInt(output.split('\t')[0]) || 0;
    const GB = 1024 * 1024 * 1024;
    if (bytes > GB) {
      const msg = `⚠️ Backup папка занимает ${(bytes / GB).toFixed(1)} GB. Очистите старые файлы.`;
      console.warn(`[scheduler] ${msg}`);
      _notify(msg);
    }
  } catch (e) {
    console.error('[scheduler] checkDiskSpace error:', e.message);
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

// ─── Memory usage monitor ─────────────────────────────────────────────────────
function checkMemoryUsage() {
  try {
    const mem = process.memoryUsage();
    const heapMb = Math.round(mem.heapUsed / 1024 / 1024);
    const rssMb = Math.round(mem.rss / 1024 / 1024);
    const threshold = parseInt(process.env.MEMORY_ALERT_MB || '500');
    if (heapMb > threshold) {
      const msg = `⚠️ Высокое использование памяти: heap ${heapMb} МБ (RSS ${rssMb} МБ). Порог: ${threshold} МБ`;
      console.warn('[scheduler]', msg);
      _notify(msg);
    }
  } catch (e) {
    console.error('[scheduler] checkMemoryUsage error:', e.message);
  }
}

// ─── БЛОК 11.1: Client reminder tasks ─────────────────────────────────────────

/**
 * Reminder 1: 24h before shooting.
 * Every hour — find confirmed orders whose event_date is tomorrow
 * and reminder_24h_sent IS NULL, then send a Telegram message.
 */
async function sendShootingReminder24h() {
  try {
    const { query, run } = require('../database');
    const botModule = require('../bot');

    const orders = await query(`
      SELECT o.id, o.order_number, o.client_chat_id, o.event_date, o.event_time, o.location
      FROM orders o
      WHERE o.status = 'confirmed'
        AND date(o.event_date) = date('now', '+1 day')
        AND o.reminder_24h_sent IS NULL
        AND o.client_chat_id IS NOT NULL
        AND o.client_chat_id != ''
    `).catch(() => []);

    if (!orders.length) return;

    let sent = 0;
    for (const order of orders) {
      try {
        const timeStr = order.event_time ? ` в ${order.event_time}` : '';
        const locationStr = order.location ? `\n📍 Адрес: ${order.location}` : '';
        const text =
          `📅 *Напоминание о съёмке*\n\n` +
          `Завтра ваша съёмка${timeStr}\\.\n` +
          `Заявка \\#${order.order_number}` +
          (locationStr ? `\n${locationStr}` : '') +
          `\n\nЕсли есть вопросы — напишите менеджеру\\.`;

        const ok = await botModule.sendClientReminder(order.client_chat_id, text);
        if (ok) {
          await run(`UPDATE orders SET reminder_24h_sent = datetime('now') WHERE id = ?`, [order.id]);
          sent++;
        }
      } catch (e) {
        console.error('[scheduler] 24h reminder error for order', order.id, e.message);
      }
    }

    if (sent > 0) console.log(`[scheduler] 24h shooting reminders sent: ${sent}`);
  } catch (e) {
    console.error('[scheduler] sendShootingReminder24h error:', e.message);
  }
}

/**
 * Reminder 2: Stale pending orders (client hasn't completed the form).
 * Every hour — find orders with status='pending' that haven't been updated
 * for more than 2 hours and haven't been reminded yet (reminded_at IS NULL).
 */
async function remindStalePendingOrders() {
  try {
    const { query, run } = require('../database');
    const botModule = require('../bot');

    const orders = await query(`
      SELECT o.id, o.order_number, o.client_chat_id, o.client_name
      FROM orders o
      WHERE o.status = 'pending'
        AND o.updated_at < datetime('now', '-2 hours')
        AND o.reminded_at IS NULL
        AND o.client_chat_id IS NOT NULL
        AND o.client_chat_id != ''
    `).catch(() => []);

    if (!orders.length) return;

    let sent = 0;
    for (const order of orders) {
      try {
        const text =
          `📋 *Незавершённая заявка*\n\n` +
          `Вы начали оформление заявки \\#${order.order_number}, ` +
          `но не заполнили все данные\\.\n\n` +
          `Пожалуйста, завершите заявку или напишите менеджеру — мы поможем\\.`;

        const ok = await botModule.sendClientReminder(order.client_chat_id, text);
        if (ok) {
          await run(`UPDATE orders SET reminded_at = datetime('now') WHERE id = ?`, [order.id]);
          sent++;
        }
      } catch (e) {
        console.error('[scheduler] pending reminder error for order', order.id, e.message);
      }
    }

    if (sent > 0) console.log(`[scheduler] Stale pending order reminders sent: ${sent}`);
  } catch (e) {
    console.error('[scheduler] remindStalePendingOrders error:', e.message);
  }
}

/**
 * Reminder 3: Post-shoot congratulation.
 * Every hour — find orders with status='completed' where event_date was yesterday
 * (i.e. >= 1 hour ago but within the same day) and completed_reminder_sent IS NULL.
 * We check event_date < date('now') so we send after the day has passed.
 */
async function sendCompletedShootingCongrats() {
  try {
    const { query, run } = require('../database');
    const botModule = require('../bot');

    const orders = await query(`
      SELECT o.id, o.order_number, o.client_chat_id, o.event_date
      FROM orders o
      WHERE o.status = 'completed'
        AND o.event_date IS NOT NULL
        AND date(o.event_date) < date('now')
        AND date(o.event_date) >= date('now', '-2 days')
        AND o.completed_reminder_sent IS NULL
        AND o.client_chat_id IS NOT NULL
        AND o.client_chat_id != ''
    `).catch(() => []);

    if (!orders.length) return;

    let sent = 0;
    for (const order of orders) {
      try {
        const text =
          `🎉 *Спасибо за съёмку\\!*\n\n` +
          `Мы рады были поработать с вами\\.\n` +
          `Надеемся увидеть вас снова\\! 💃\n\n` +
          `Если хотите оставить отзыв или забронировать следующую съёмку — мы всегда на связи\\.`;

        const ok = await botModule.sendClientReminder(order.client_chat_id, text);
        if (ok) {
          await run(`UPDATE orders SET completed_reminder_sent = datetime('now') WHERE id = ?`, [order.id]);
          sent++;
        }
      } catch (e) {
        console.error('[scheduler] completed congrats error for order', order.id, e.message);
      }
    }

    if (sent > 0) console.log(`[scheduler] Post-shoot congratulations sent: ${sent}`);
  } catch (e) {
    console.error('[scheduler] sendCompletedShootingCongrats error:', e.message);
  }
}

// ─── БЛОК 35: Uptime logging — record health check every 5 minutes ────────────
async function recordUptimeLog() {
  try {
    const { run: dbRun, get: dbGet } = require('../database');
    const start = Date.now();
    let status = 'ok';
    let latencyMs = null;
    try {
      await dbGet('SELECT 1 as ok');
      latencyMs = Date.now() - start;
      if (latencyMs > 2000) status = 'degraded';
    } catch (_) {
      status = 'down';
    }
    await dbRun(`INSERT INTO uptime_logs (checked_at, status, latency_ms) VALUES (datetime('now'), ?, ?)`, [
      status,
      latencyMs,
    ]).catch(() => {});
    // Prune logs older than 91 days to keep the table lean
    await dbRun(`DELETE FROM uptime_logs WHERE checked_at < datetime('now', '-91 days')`).catch(() => {});
  } catch (e) {
    console.error('[scheduler] recordUptimeLog error:', e.message);
  }
}

// ─── БЛОК 6.4: Pending orders queue alert ─────────────────────────────────────
// Alert if > 10 'new' orders have been waiting > 1 hour without attention
async function checkPendingOrdersQueue() {
  try {
    const { get: dbGet } = require('../database');
    const staleThreshold = parseInt(process.env.QUEUE_ALERT_THRESHOLD || '10');
    const row = await dbGet(
      `SELECT COUNT(*) as n FROM orders WHERE status='new' AND created_at < datetime('now', '-1 hour')`
    );
    const staleCount = row?.n || 0;
    if (staleCount > staleThreshold) {
      const msg = `⚠️ Накопилось ${staleCount} необработанных заявок! Проверьте панель.`;
      console.warn('[scheduler]', msg);
      _notify(msg);
    } else if (staleCount > 0) {
      console.log(`[scheduler] Queue check: ${staleCount} stale new orders (threshold: ${staleThreshold})`);
    }
  } catch (e) {
    console.error('[scheduler] checkPendingOrdersQueue error:', e.message);
  }
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
  // Run immediately on startup so first backup is available right away
  runBackup();
  scheduleEvery(runBackup, 'DB backup (every 6h)', 6);
  scheduleWeekly(runVacuum, 'SQLite VACUUM + WAL TRUNCATE', 0, 3, 0); // Sunday 03:00
  // Also run vacuum via shell script weekly (Sunday 03:30) for additional WAL cleanup
  scheduleWeekly(runVacuumScript, 'SQLite VACUUM shell script', 0, 3, 30); // Sunday 03:30
  scheduleEvery(runWalCheckpoint, 'WAL checkpoint (PASSIVE)', 6);
  // Weekly WAL TRUNCATE checkpoint: Sunday 04:00 (ensures WAL is fully flushed after VACUUM)
  scheduleWeekly(runWalCheckpointTruncate, 'WAL checkpoint TRUNCATE (Sunday 04:00)', 0, 4, 0);
  scheduleDaily(sendEventReminders, 'Event reminders', 9, 0);
  scheduleEvery(checkFactoryStaleness, 'Factory staleness check', 6);
  // Additional 30-min factory staleness check (faster detection)
  scheduleEveryMinutes(checkFactoryStaleness, 'Factory staleness check (30min)', 30);
  // Bot watchdog: check bot polling every 5 minutes
  scheduleEveryMinutes(checkBotHealth, 'Bot watchdog', 5);
  // Disk space alert: check backup folder size every 6 hours
  scheduleEvery(checkDiskSpace, 'Disk space check (every 6h)', 6);
  // WAL size monitor: check every hour, alert + force TRUNCATE checkpoint if > 100 MB
  scheduleEvery(checkWalSize, 'WAL size monitor (every 1h)', 1);
  // Scheduled broadcasts processor: check every minute
  scheduleEveryMinutes(processScheduledBroadcasts, 'Scheduled broadcasts processor', 1);
  // Memory usage monitor: check every hour
  scheduleEvery(checkMemoryUsage, 'Memory monitor', 1);
  // БЛОК 11.1 — Client reminder tasks (every hour)
  scheduleEvery(sendShootingReminder24h, '24h shooting reminder', 1);
  scheduleEvery(remindStalePendingOrders, 'Stale pending order reminder', 1);
  scheduleEvery(sendCompletedShootingCongrats, 'Post-shoot congratulation', 1);
  // БЛОК 6.4 — Pending orders queue alert (every hour)
  scheduleEvery(checkPendingOrdersQueue, 'Pending orders queue alert (every 1h)', 1);
  // БЛОК 35 — Uptime logging: record health status every 5 minutes
  recordUptimeLog(); // first log on startup
  scheduleEveryMinutes(recordUptimeLog, 'Uptime log (every 5min)', 5);
  console.log(
    '[scheduler] Started: backup (every 6h), VACUUM (Sunday 03:00 + 03:30), WAL checkpoint (PASSIVE every 6h, TRUNCATE Sunday 04:00), WAL size monitor (every 1h), event reminders (daily 09:00), factory staleness check (every 6h + 30min), bot watchdog (every 5min), disk space check (every 6h), scheduled broadcasts processor (every 1min), memory monitor (every 1h), 24h shooting reminder (every 1h), stale pending reminder (every 1h), post-shoot congrats (every 1h), pending queue alert (every 1h), uptime log (every 5min)'
  );
}

function stop() {
  _intervals.forEach(t => clearTimeout(t));
  _intervals = [];
}

module.exports = { init, start, stop };
