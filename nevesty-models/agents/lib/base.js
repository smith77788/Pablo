/**
 * Base Agent — shared "cell biology" for every organ in the organism.
 * Every agent extends this and implements analyze() + optionally fix().
 */
require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });
const path   = require('path');
const fs     = require('fs');
const sqlite = require('sqlite3').verbose();

const DB_PATH  = path.join(__dirname, '../../data.db');
const BOT_PATH = path.join(__dirname, '../../bot.js');
const SRV_PATH = path.join(__dirname, '../../server.js');
const API_PATH = path.join(__dirname, '../../routes/api.js');
const DB_MOD   = path.join(__dirname, '../../database.js');

// ─── Shared DB helpers — один постоянный write-коннект, очередь записей ───────
// Вместо 140+ открытий соединений за цикл — одно постоянное.
// Это устраняет lock-storm на бот во время работы агентов.
const DB_TIMEOUT = 8000; // 8s max per query

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error(`DB timeout: ${label}`)), ms))
  ]);
}

// ── Persistent write connection ───────────────────────────────────────────────
let _writeDb     = null;
let _writeSerial = Promise.resolve();  // one writer at a time

function _acquireWriteDb() {
  if (_writeDb) return Promise.resolve(_writeDb);
  return new Promise((resolve, reject) => {
    const db = new sqlite.Database(DB_PATH, sqlite.OPEN_READWRITE | sqlite.OPEN_CREATE, err => {
      if (err) return reject(err);
      db.configure('busyTimeout', 5000);
      db.run('PRAGMA journal_mode=WAL');
      db.run('PRAGMA synchronous=NORMAL');
      _writeDb = db;
      db.on('error', () => { _writeDb = null; });
      resolve(db);
    });
  });
}

function dbRun(sql, params = []) {
  const task = _writeSerial.then(() =>
    _acquireWriteDb()
      .then(db => new Promise((res, rej) =>
        db.run(sql, params, function(e) {
          if (e && (e.code === 'SQLITE_MISUSE' || e.code === 'SQLITE_ABORT')) _writeDb = null;
          e ? rej(e) : res({ id: this.lastID, changes: this.changes });
        })
      ))
      .catch(e => { _writeDb = null; throw e; })
  );
  _writeSerial = task.catch(() => {});  // errors must not stall the queue
  return withTimeout(task, DB_TIMEOUT, sql.slice(0, 40));
}

function dbGet(sql, params = []) {
  return withTimeout(new Promise((res, rej) => {
    const db = new sqlite.Database(DB_PATH, sqlite.OPEN_READONLY, err => {
      if (err) return rej(err);
      db.configure('busyTimeout', 5000);
      db.run('PRAGMA journal_mode=WAL');
      db.get(sql, params, (e, row) => { db.close(); e ? rej(e) : res(row); });
    });
  }), DB_TIMEOUT, sql.slice(0, 40));
}
function dbAll(sql, params = []) {
  return withTimeout(new Promise((res, rej) => {
    const db = new sqlite.Database(DB_PATH, sqlite.OPEN_READONLY, err => {
      if (err) return rej(err);
      db.configure('busyTimeout', 5000);
      db.run('PRAGMA journal_mode=WAL');
      db.all(sql, params, (e, rows) => { db.close(); e ? rej(e) : res(rows); });
    });
  }), DB_TIMEOUT, sql.slice(0, 40));
}

// ─── File read helper ─────────────────────────────────────────────────────────
function readFile(p) {
  try { return fs.readFileSync(p, 'utf8'); }
  catch { return ''; }
}

// ─── Telegram — HTTP helper ───────────────────────────────────────────────────
const https = require('https');
const TG_TIMEOUT = 15000;

function tgRequest(method, body) {
  const TOKEN = process.env.TELEGRAM_BOT_TOKEN;
  if (!TOKEN) return Promise.resolve(null);
  return new Promise(resolve => {
    const payload = JSON.stringify(body);
    let settled = false;
    let raw = '';
    const done = (val = null) => { if (!settled) { settled = true; resolve(val); } };

    const req = https.request({
      hostname: 'api.telegram.org',
      path: `/bot${TOKEN}/${method}`,
      method: 'POST',
      timeout: TG_TIMEOUT,
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) }
    }, res => {
      res.on('data', d => { raw += d; });
      res.on('end', () => {
        try { done(JSON.parse(raw)); } catch { done(null); }
      });
    });

    req.on('error', () => done(null));
    req.on('timeout', () => { req.destroy(); done(null); });
    req.write(payload);
    req.end();
    setTimeout(() => done(null), TG_TIMEOUT + 1000);
  });
}

/** Send to all admins. Returns void. */
function tgSend(text, opts = {}) {
  const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
  if (!ADMIN_IDS.length) return Promise.resolve();
  const sends = ADMIN_IDS.map(chatId =>
    tgRequest('sendMessage', { chat_id: chatId, text: text.slice(0, 4000), disable_web_page_preview: true, ...opts })
  );
  return Promise.allSettled(sends);
}

/** Send to first admin only, return {chatId, messageId} for later editing. */
async function tgSendGetId(text, opts = {}) {
  const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
  if (!ADMIN_IDS.length) return null;
  const chatId = ADMIN_IDS[0];
  const res = await tgRequest('sendMessage', { chat_id: chatId, text: text.slice(0, 4000), disable_web_page_preview: true, ...opts });
  if (res?.ok && res.result?.message_id) return { chatId, messageId: res.result.message_id };
  return null;
}

/** Edit a previously sent message. */
function tgEditMessage(chatId, messageId, text, opts = {}) {
  if (!chatId || !messageId) return Promise.resolve();
  return tgRequest('editMessageText', {
    chat_id: chatId, message_id: messageId,
    text: text.slice(0, 4000), disable_web_page_preview: true, ...opts
  });
}

/** Build ASCII progress bar: ████░░░░░░ */
function progressBar(done, total, width = 20) {
  const filled = Math.round((done / Math.max(total, 1)) * width);
  return '█'.repeat(filled) + '░'.repeat(width - filled);
}

// ─── Log to agent_logs table ─────────────────────────────────────────────────
async function logAgent(fromName, message) {
  try {
    await dbRun('INSERT INTO agent_logs (from_name, message) VALUES (?, ?)', [fromName, message]);
  } catch {}
}

// ─── Finding severity ─────────────────────────────────────────────────────────
const SEV = { CRITICAL: '🔴', HIGH: '🟠', MEDIUM: '🟡', LOW: '🟢', INFO: '⚪', OK: '✅' };

// ─── Base Agent class ─────────────────────────────────────────────────────────
class Agent {
  constructor({ id, name, organ, emoji, focus }) {
    this.id    = id;       // e.g. '01'
    this.name  = name;     // e.g. 'UX Architect'
    this.organ = organ;    // metaphor: 'Cerebral Cortex'
    this.emoji = emoji;    // e.g. '🧠'
    this.focus = focus;    // one-line description
    this.findings = [];
    this.fixed    = [];
  }

  /** Override: return array of { sev, msg, fixed? } */
  async analyze() { return []; }

  /** Override: apply automated fix */
  async fix(issue) { return false; }

  addFinding(sev, msg, { autoFixable = false, proposedFix = null, file = null, line = null } = {}) {
    this.findings.push({ sev: SEV[sev] || sev, msg, autoFixable, proposedFix, file, line });
  }

  addFixed(description) {
    this.fixed = this.fixed || [];
    this.fixed.push(description);
  }

  /** Run the full agent lifecycle.
   *  silent=true: skip individual Telegram notification (orchestrator collects all findings itself) */
  async run({ silent = false } = {}) {
    const t0 = Date.now();
    const label = `Agent: ${this.name}`;

    try {
      this.findings = [];
      this.fixed    = [];
      await this.analyze();

      // Post findings to shared blackboard
      for (const f of this.findings) {
        if (!['✅', '⚪'].includes(f.sev)) {
          await dbRun(
            `INSERT INTO agent_findings (agent_name, severity, message, file, line, auto_fixable, proposed_fix, status)
             VALUES (?, ?, ?, ?, ?, ?, ?, 'open')`,
            [this.name, f.sev, f.msg, f.file || null, f.line || null, f.autoFixable ? 1 : 0, f.proposedFix || null]
          ).catch(() => {});

          // CRITICAL и HIGH — агент сообщает в обсуждения
          if ([SEV.CRITICAL, SEV.HIGH].includes(f.sev)) {
            const fixHint = f.proposedFix
              ? ` Предлагаю: ${f.proposedFix.slice(0, 100)}`
              : f.autoFixable ? ' Могу исправить автоматически.' : ' Нужен ручной ревью.';
            await dbRun(
              `INSERT INTO agent_discussions (from_agent, to_agent, topic, message)
               VALUES (?, ?, ?, ?)`,
              [
                this.name,
                'Orchestrator',
                `${f.sev} ${f.msg.slice(0, 80)}`,
                `${this.emoji} Обнаружил проблему [${f.sev}] в ${f.file || 'коде'}: ${f.msg}.${fixHint}`,
              ]
            ).catch(() => {});
          }
        }
      }

      // Если агент что-то исправил — сообщает об этом
      for (const fix of this.fixed) {
        await dbRun(
          `INSERT INTO agent_discussions (from_agent, to_agent, topic, message)
           VALUES (?, ?, ?, ?)`,
          [
            this.name,
            'all',
            `✅ Исправление`,
            `${this.emoji} Автоматически исправил: ${fix.slice(0, 200)}`,
          ]
        ).catch(() => {});
      }
    } catch (e) {
      this.addFinding('HIGH', `Ошибка выполнения агента: ${e.message}`);
    }

    const elapsed  = ((Date.now() - t0) / 1000).toFixed(1);
    const critical = this.findings.filter(f => f.sev === SEV.CRITICAL).length;
    const high     = this.findings.filter(f => f.sev === SEV.HIGH).length;
    const fixCount = this.fixed.length;

    // Build summary line for DB log (compact)
    const badges = [critical && `🔴${critical}`, high && `🟠${high}`, fixCount && `🔧${fixCount}`].filter(Boolean).join(' ');
    const logLine = `${this.emoji} ${this.name} [${this.organ}] ${elapsed}s ${badges||'✅'}\n` +
      [...this.findings.filter(f => f.sev !== SEV.OK && f.sev !== SEV.INFO).map(f => `${f.sev} ${f.msg}`),
       ...this.fixed.map(m => `🔧 ${m}`)].join('\n');

    await logAgent(label, logLine);

    // In silent mode, no individual Telegram notification (orchestrator handles it)
    if (!silent && (critical + high + fixCount > 0)) {
      const detail = [
        `${this.emoji} ${this.name} [${this.organ}]`,
        '',
        ...this.findings.filter(f => [SEV.CRITICAL, SEV.HIGH].includes(f.sev)).map(f => `${f.sev} ${f.msg}`),
        ...this.fixed.map(m => `🔧 ${m}`),
      ].join('\n');
      await tgSend(detail); // plain text — finding messages may contain MarkdownV2 special chars
    }

    return { findings: this.findings, fixed: this.fixed, elapsed };
  }
}

module.exports = {
  Agent, dbRun, dbGet, dbAll, readFile, logAgent,
  tgSend, tgSendGetId, tgEditMessage, progressBar, SEV,
  BOT_PATH, SRV_PATH, API_PATH, DB_MOD, DB_PATH
};
