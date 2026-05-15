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
  console.log('[scheduler] Started: backup (daily 01:00), VACUUM (Sunday 03:00)');
}

function stop() {
  _intervals.forEach(t => clearTimeout(t));
  _intervals = [];
}

module.exports = { init, start, stop };
