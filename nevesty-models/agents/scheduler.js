/**
 * 🧬 Living Organism Scheduler — 24/7 autonomous operation
 *
 * Каждые 15 минут запускает полный цикл:
 * 1. AutoFixer — немедленные тех. исправления (сессии, индексы, orphans)
 * 2. SmartOrchestrator — 28 агентов анализируют и исправляют
 * 3. Детальный audit-log каждого действия
 *
 * Администратор может посмотреть что делал организм за ЛЮБОЙ период через
 * Telegram → 💬 Обсуждения или 📡 Фид агентов.
 */
'use strict';
require('dotenv').config({ path: require('path').join(__dirname, '../.env') });

let runCheck;
try {
  runCheck = require('./smart-orchestrator').runSmartOrchestrator;
} catch {
  runCheck = require('./orchestrator').runOrchestrator;
}

const AutoFixer = require('./auto-fixer');
const { tgSend, logAgent, dbAll, dbRun, dbGet } = require('./lib/base');

const INTERVAL_MS = 15 * 60 * 1000;   // 15 минут
const CYCLE_LOG_LIMIT = 10000;         // храним до 10000 записей в agent_logs

let cycleNumber   = 0;
let lastScore     = null;
let totalFixed    = 0;
let totalCycles   = 0;
let startupTime   = Date.now();
let cycleRunning  = false;  // circuit breaker: skip if prev cycle still running

// ─── Детальная запись каждого цикла ──────────────────────────────────────────

async function logCycleStart(cycleNum) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  await dbRun(
    `INSERT INTO agent_discussions (from_agent, to_agent, topic, message) VALUES (?,?,?,?)`,
    ['Scheduler', 'all', `Цикл #${cycleNum} старт`,
     `🧬 Цикл #${cycleNum} начат в ${ts}. Аптайм: ${formatUptime(Date.now() - startupTime)}.`]
  ).catch(() => {});
}

async function logCycleEnd(cycleNum, score, fixed, elapsed) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  await dbRun(
    `INSERT INTO agent_discussions (from_agent, to_agent, topic, message) VALUES (?,?,?,?)`,
    ['Scheduler', 'all', `Цикл #${cycleNum} завершён`,
     `✅ Цикл #${cycleNum} завершён в ${ts}. Score=${score}%, исправлено=${fixed}, время=${elapsed}с. Итого за сессию: циклов=${totalCycles}, исправлений=${totalFixed}.`]
  ).catch(() => {});
}

function formatUptime(ms) {
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  return h > 0 ? `${h}ч ${m}м` : `${m}м`;
}

// ─── Очистка старых логов (чтобы БД не пухла) ────────────────────────────────

async function pruneOldLogs() {
  try {
    // Оставляем только 10000 последних записей в agent_logs
    const count = await dbGet('SELECT COUNT(*) as n FROM agent_logs');
    if (count?.n > CYCLE_LOG_LIMIT) {
      await dbRun(
        `DELETE FROM agent_logs WHERE id IN (
           SELECT id FROM agent_logs ORDER BY created_at ASC LIMIT ?
         )`,
        [count.n - CYCLE_LOG_LIMIT]
      );
    }
    // Оставляем 30 дней обсуждений
    await dbRun(
      `DELETE FROM agent_discussions WHERE created_at < datetime('now', '-30 days')`
    ).catch(() => {});
    // Оставляем 7 дней открытых findings (закрытые — навсегда)
    await dbRun(
      `DELETE FROM agent_findings WHERE status='open' AND created_at < datetime('now', '-7 days')`
    ).catch(() => {});
  } catch {}
}

// ─── Основной цикл ───────────────────────────────────────────────────────────

async function runCycle() {
  if (cycleRunning) {
    const ts = new Date().toLocaleString('ru', { timeZone: 'Europe/Moscow' });
    console.log(`[${ts}] ⏭ Цикл пропущен — предыдущий ещё выполняется`);
    return;
  }
  cycleRunning = true;
  cycleNumber++;
  totalCycles++;
  const t0 = Date.now();
  const ts = new Date().toLocaleString('ru', { timeZone: 'Europe/Moscow' });
  console.log(`\n[${ts}] 🧬 Цикл #${cycleNumber} начат...`);

  await logCycleStart(cycleNumber);

  try {
    // ── Шаг 1: AutoFixer (быстрые тех. исправления) ──────────────────────────
    const fixer = new AutoFixer();
    await fixer.run({ silent: true });
    const autoFixed = fixer.fixed || [];
    if (autoFixed.length > 0) {
      totalFixed += autoFixed.length;
      await tgSend(
        `🔧 AutoFixer [цикл #${cycleNumber}]:\n` +
        autoFixed.slice(0, 5).map(f => `• ${f}`).join('\n')
      ).catch(() => {});
    }

    // ── Шаг 2: Smart Orchestrator (28 агентов) ───────────────────────────────
    const result = await runCheck();
    const { healthScore = 100, criticalCount = 0, highCount = 0 } = result;
    const orchFixed = (result.fixResults || []).filter(r => r.outcome === 'fixed').length;
    totalFixed += orchFixed;

    // ── Шаг 3: Алерт если health score деградировал ──────────────────────────
    if (lastScore !== null && healthScore < lastScore - 15) {
      await tgSend(
        `⚠️ Health Score деградировал: ${lastScore}% → ${healthScore}%\n` +
        `🔴 ${criticalCount} крит, 🟠 ${highCount} высоких\n` +
        `Цикл #${cycleNumber} — проверьте бот.`
      ).catch(() => {});
    }

    // ── Шаг 4: Очистка старых логов ──────────────────────────────────────────
    await pruneOldLogs();

    const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    lastScore = healthScore;

    await logCycleEnd(cycleNumber, healthScore, autoFixed.length + orchFixed, elapsed);
    await logAgent('Scheduler',
      `Цикл #${cycleNumber}: Score=${healthScore}% autoFixed=${autoFixed.length} orchFixed=${orchFixed} ${elapsed}с`
    );
    console.log(`[Scheduler] Цикл #${cycleNumber} done. Score=${healthScore}% fixed=${autoFixed.length + orchFixed} (${elapsed}с)`);

  } catch (err) {
    const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    console.error(`[Scheduler] Цикл #${cycleNumber} ERROR (${elapsed}с):`, err.message);
    await dbRun(
      `INSERT INTO agent_discussions (from_agent, to_agent, topic, message) VALUES (?,?,?,?)`,
      ['Scheduler', 'all', `❌ Цикл #${cycleNumber} ошибка`,
       `Ошибка в цикле #${cycleNumber}: ${err.message}. Следующая попытка через ${INTERVAL_MS/60000} мин.`]
    ).catch(() => {});
    // Не спамим в Telegram при повторных ошибках
    if (cycleNumber <= 3 || cycleNumber % 10 === 0) {
      await tgSend(`🚨 Organism error [цикл #${cycleNumber}]: ${err.message}`).catch(() => {});
    }
  } finally {
    cycleRunning = false;
  }
}

// ─── Вспомогательные задачи (cron-like, проверка каждую минуту) ──────────────

const { execSync } = require('child_process');
const fs           = require('fs');
const NOTIFY_PATH  = '/home/user/Pablo/nevesty-models/tools/notify.js';
const NOTIFY_CWD   = '/home/user/Pablo/nevesty-models';
const FACTORY_DB   = '/home/user/Pablo/factory/factory.db';

// ─── escapeMarkdown helper ────────────────────────────────────────────────────
function escapeMarkdown(text) {
  return String(text || '').replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, '\\$&');
}

function notify(msg) {
  try {
    const safe = msg.replace(/"/g, '\\"').replace(/\n/g, '\\n');
    execSync(`node ${NOTIFY_PATH} --from "Scheduler" "${safe}"`, { cwd: NOTIFY_CWD, timeout: 15000 });
  } catch (e) {
    console.error('[Scheduler] notify error:', e.message);
  }
}

// Хранит метки последнего запуска по ключу, чтобы не дублировать в рамках суток
const _lastRun = {};

function shouldRun(key, nowH, nowM, nowDow, targetH, targetM, targetDow /* -1 = every day */) {
  if (nowH !== targetH || nowM !== targetM) return false;
  if (targetDow !== -1 && nowDow !== targetDow) return false;
  const today = new Date().toISOString().slice(0, 10);
  if (_lastRun[key] === today) return false;
  _lastRun[key] = today;
  return true;
}

// Таск 1: Еженедельно (воскресенье 03:00) — VACUUM + ANALYZE БД
async function taskWeeklyVacuum() {
  try {
    await dbRun('VACUUM');
    await dbRun('ANALYZE');
    console.log('[Scheduler] DB VACUUM + ANALYZE completed');
  } catch (e) {
    console.error('[Scheduler] VACUUM error:', e.message);
  }
}

// Таск 2: Каждые 6 часов — проверка AI Factory
function taskFactoryHealthCheck() {
  if (!fs.existsSync(FACTORY_DB)) return;
  const sqlite3 = require('sqlite3').verbose();
  const fdb = new sqlite3.Database(FACTORY_DB, sqlite3.OPEN_READONLY);
  fdb.get("SELECT MAX(started_at) as last FROM cycles WHERE status='completed'", [], (err, row) => {
    fdb.close();
    if (err || !row?.last) return;
    const hoursSince = (Date.now() - new Date(row.last).getTime()) / 3600000;
    if (hoursSince > 12) {
      notify(`⚠️ AI Factory не запускался ${Math.round(hoursSince)} часов! Последний цикл: ${row.last}`);
    }
  });
}

// ─── Lazy bot instance for client messaging ───────────────────────────────────
function getBot() {
  const TelegramBot = require('node-telegram-bot-api');
  const token = process.env.TELEGRAM_BOT_TOKEN || process.env.BOT_TOKEN;
  if (!token || token === 'your_bot_token_here') return null;
  return new TelegramBot(token, { polling: false });
}

// ─── Задача: отправка запроса отзыва через 24ч после завершения заявки ────────
const reviewFollowup = async () => {
  try {
    const completed = await dbAll(`
      SELECT o.id, o.client_chat_id, o.order_number, o.client_name
      FROM orders o
      LEFT JOIN reviews r ON r.order_id = o.id
      WHERE o.status = 'completed'
        AND o.client_chat_id IS NOT NULL
        AND CAST(o.client_chat_id AS INTEGER) > 0
        AND r.id IS NULL
        AND datetime(o.updated_at) < datetime('now', '-24 hours')
        AND datetime(o.updated_at) > datetime('now', '-48 hours')
        AND o.review_requested IS NULL
    `).catch(() => []);

    if (!completed.length) return;
    const bot = getBot();
    if (!bot) return;

    for (const order of completed) {
      try {
        // Check client notification prefs
        const prefs = await dbGet('SELECT notify_review FROM client_prefs WHERE chat_id=?', [order.client_chat_id]).catch(() => null);
        if (prefs && prefs.notify_review === 0) continue;

        await bot.sendMessage(order.client_chat_id,
          `⭐ *${escapeMarkdown(order.client_name || 'Здравствуйте')}*, ваша заявка завершена\\!\n\nКак всё прошло? Оставьте отзыв — это займёт минуту и поможет другим клиентам выбрать модель\\.`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '⭐ Оставить отзыв', callback_data: `leave_review_${order.id}` }],
            [{ text: '✅ Всё хорошо, спасибо', callback_data: 'review_skip' }]
          ]}
        });

        // Mark as requested
        await dbRun('UPDATE orders SET review_requested=CURRENT_TIMESTAMP WHERE id=?', [order.id]).catch(() => {});
      } catch {} // User may have blocked bot
    }

    console.log(`[Scheduler] Review follow-up sent: ${completed.length} orders`);
  } catch (e) {
    console.error('[scheduler] review followup error:', e.message);
  }
};

// Запускать каждые 6 часов
setInterval(reviewFollowup, 6 * 60 * 60 * 1000);
setTimeout(reviewFollowup, 30000); // Один раз после старта

// ─── Задача: выполнение запланированных рассылок (каждые 5 минут) ────────────

const processScheduledBroadcasts = async () => {
  try {
    const pending = await dbAll(
      `SELECT * FROM scheduled_broadcasts WHERE status='pending' AND scheduled_at <= datetime('now') LIMIT 10`
    ).catch(() => []);

    if (!pending.length) return;

    const bot = getBot();
    if (!bot) return;

    for (const bcast of pending) {
      try {
        // Mark as processing to avoid double-send
        await dbRun("UPDATE scheduled_broadcasts SET status='processing' WHERE id=? AND status='pending'", [bcast.id]).catch(() => {});

        // Get recipients by segment
        let recipientsQuery = "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != ''";
        if (bcast.segment === 'completed') {
          recipientsQuery += " AND status='completed'";
        } else if (bcast.segment === 'active') {
          recipientsQuery += " AND status IN ('new','reviewing','confirmed','in_progress')";
        }
        const recipients = await dbAll(recipientsQuery).catch(() => []);

        let sent = 0, failed = 0;
        const msgText = `📢 *Сообщение от Nevesty Models*\n\n${escapeMarkdown(bcast.text)}`;

        for (const r of recipients) {
          try {
            await bot.sendMessage(r.client_chat_id, msgText, { parse_mode: 'MarkdownV2' });
            sent++;
          } catch { failed++; }
          await new Promise(resolve => setTimeout(resolve, 60)); // Rate limit: ~16 msg/sec
        }

        await dbRun("UPDATE scheduled_broadcasts SET status='sent' WHERE id=?", [bcast.id]).catch(() => {});
        console.log(`[Scheduler] Scheduled broadcast #${bcast.id} sent: ${sent} ok, ${failed} failed`);
        notify(`📅 Запланированная рассылка #${bcast.id} отправлена: ${sent} получателей`);
      } catch (e) {
        await dbRun("UPDATE scheduled_broadcasts SET status='error' WHERE id=?", [bcast.id]).catch(() => {});
        console.error(`[Scheduler] Scheduled broadcast #${bcast.id} error:`, e.message);
      }
    }
  } catch (e) {
    console.error('[Scheduler] processScheduledBroadcasts error:', e.message);
  }
};

// Проверять каждые 5 минут
setInterval(processScheduledBroadcasts, 5 * 60 * 1000);
setTimeout(processScheduledBroadcasts, 60000); // Первый запуск через 1 мин после старта

// ─── Задача: еженедельный re-engagement неактивных клиентов ──────────────────

// Таск 3: Понедельник 10:00 — re-engagement клиентов
async function taskReEngagement() {
  try {
    const inactive = await dbAll(`
      SELECT DISTINCT o.client_chat_id, MAX(o.client_name) as name
      FROM orders o
      WHERE o.client_chat_id IS NOT NULL AND CAST(o.client_chat_id AS INTEGER) > 0
        AND o.status = 'completed'
      GROUP BY o.client_chat_id
      HAVING MAX(o.created_at) < datetime('now', '-60 days')
        AND MAX(o.created_at) > datetime('now', '-90 days')
    `).catch(() => []);

    console.log(`[Scheduler] Re-engagement: ${inactive.length} clients eligible`);
    if (!inactive.length) return;

    const bot = getBot();
    if (!bot) return;

    for (const client of inactive.slice(0, 20)) { // Max 20 per run
      try {
        const prefs = await dbGet('SELECT notify_promo FROM client_prefs WHERE chat_id=?', [client.client_chat_id]).catch(() => null);
        if (prefs && prefs.notify_promo === 0) continue;

        await bot.sendMessage(client.client_chat_id,
          `👋 *${escapeMarkdown(client.name || 'Здравствуйте')}\\!*\n\nДавно не видели вас в нашем агентстве\\. У нас появились новые модели\\!\n\n💃 Посмотрите каталог — возможно найдёте подходящую для вашего следующего события\\.`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '💃 Открыть каталог', callback_data: 'cat_cat__0' }],
            [{ text: '📋 Оформить заявку', callback_data: 'bk_start' }]
          ]}
        });
        await new Promise(resolve => setTimeout(resolve, 500)); // Throttle
      } catch {}
    }
  } catch (e) {
    console.error('[Scheduler] re-engagement error:', e.message);
  }
}

// Таск 4: Ежедневно 09:00 — утренний отчёт
async function taskDailyReport() {
  try {
    const today        = new Date().toISOString().split('T')[0];
    const todayOrders  = await dbGet('SELECT COUNT(*) as cnt FROM orders WHERE date(created_at)=?', [today]);
    const activeOrders = await dbGet("SELECT COUNT(*) as cnt FROM orders WHERE status IN ('new','reviewing','confirmed','in_progress')");
    const pendingRevs  = await dbGet('SELECT COUNT(*) as cnt FROM reviews WHERE approved=0');
    const msg =
      `📊 Утренний отчёт\n\n` +
      `Заявок сегодня: ${todayOrders?.cnt || 0}\n` +
      `Активных заявок: ${activeOrders?.cnt || 0}\n` +
      `Отзывов на модерации: ${pendingRevs?.cnt || 0}`;
    notify(msg);
  } catch (e) {
    console.error('[Scheduler] daily report error:', e.message);
  }
}

// Таск 5: Ежедневно 02:00 — очистка устаревших сессий
async function taskSessionCleanup() {
  try {
    const result = await dbRun(
      `DELETE FROM telegram_sessions WHERE updated_at < datetime('now', '-7 days') AND state='idle'`
    );
    console.log(`[Scheduler] Cleaned ${result?.changes ?? 0} stale sessions`);
  } catch (e) {
    console.error('[Scheduler] session cleanup error:', e.message);
  }
}

// ─── Планировщик дополнительных задач (тик каждые 60 сек) ───────────────────

// Каждые 6 часов — Factory health (с момента старта)
let _lastFactoryCheck = 0;
const FACTORY_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

setInterval(() => {
  const now  = new Date();
  const h    = now.getHours();
  const m    = now.getMinutes();
  const dow  = now.getDay(); // 0=Sun,1=Mon,...

  // Воскресенье 03:00 — VACUUM
  if (shouldRun('vacuum', h, m, dow, 3, 0, 0)) {
    console.log('[Scheduler] Running weekly VACUUM...');
    taskWeeklyVacuum();
  }

  // Понедельник 10:00 — Re-engagement
  if (shouldRun('reengagement', h, m, dow, 10, 0, 1)) {
    console.log('[Scheduler] Running re-engagement...');
    taskReEngagement();
  }

  // Ежедневно 09:00 — Daily report
  if (shouldRun('dailyreport', h, m, dow, 9, 0, -1)) {
    console.log('[Scheduler] Sending daily report...');
    taskDailyReport();
  }

  // Ежедневно 02:00 — Session cleanup
  if (shouldRun('sessioncleanup', h, m, dow, 2, 0, -1)) {
    console.log('[Scheduler] Running session cleanup...');
    taskSessionCleanup();
  }

  // Каждые 6 часов — Factory health check
  if (Date.now() - _lastFactoryCheck >= FACTORY_CHECK_INTERVAL_MS) {
    _lastFactoryCheck = Date.now();
    console.log('[Scheduler] Running factory health check...');
    taskFactoryHealthCheck();
  }
}, 60 * 1000); // каждую минуту

// ─── Напоминание о незавершённых заявках (каждые 6 часов) ────────────────────

setInterval(async () => {
  try {
    const staleBookings = await dbAll(`
      SELECT s.chat_id, s.state, s.updated_at
      FROM telegram_sessions s
      WHERE s.state LIKE 'bk_%'
        AND datetime(s.updated_at) < datetime('now', '-2 hours')
        AND CAST(s.chat_id AS INTEGER) > 0
    `).catch(() => []);

    for (const s of staleBookings) {
      try {
        const TelegramBot = require('node-telegram-bot-api');
        const bot = new TelegramBot(process.env.BOT_TOKEN, { polling: false });
        await bot.sendMessage(s.chat_id,
          '⏰ Вы начали оформлять заявку, но не завершили\\. Продолжить?', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '📋 Продолжить заявку', callback_data: 'bk_start' }],
            [{ text: '❌ Отмена', callback_data: 'cancel_booking' }]
          ]}
        });
      } catch {} // Ignore if user blocked bot
    }

    if (staleBookings.length > 0) {
      console.log(`[Scheduler] Booking reminders sent: ${staleBookings.length}`);
    }
  } catch (e) {
    console.error('[Scheduler] booking reminder error:', e.message);
  }
}, 6 * 60 * 60 * 1000);

// ─── Запуск ──────────────────────────────────────────────────────────────────

console.log('🧬 Living Organism Scheduler запущен (28 агентов, каждые 15 мин)');

const uptimeStr = () => formatUptime(Date.now() - startupTime);

tgSend(
  `🟢 Organism запущен\n` +
  `28 агентов | цикл каждые 15 мин\n\n` +
  `Агенты:\n` +
  `• 25 аналитиков (UX, Security, DB, Booking...)\n` +
  `• 💰 Sales Analyst — конверсия и продажи\n` +
  `• 📝 Content Manager — контент бота\n` +
  `• 📊 Activity Logger — аудит каждого действия\n\n` +
  `Первая проверка начинается...`
).catch(() => {});

// Первый цикл — сразу
runCycle().then(() => {
  // Повторяем каждые 15 минут
  setInterval(runCycle, INTERVAL_MS);
  console.log(`[Scheduler] Следующий цикл через ${INTERVAL_MS / 60000} мин`);
});
