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

// ─── Shared DB helpers — каждый запрос в отдельном соединении с таймаутом ─────
const DB_TIMEOUT = 8000; // 8s max per query

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error(`DB timeout: ${label}`)), ms))
  ]);
}

function dbRun(sql, params = []) {
  return withTimeout(new Promise((res, rej) => {
    const db = new sqlite.Database(DB_PATH, sqlite.OPEN_READWRITE | sqlite.OPEN_CREATE, err => {
      if (err) return rej(err);
      db.configure('busyTimeout', 5000); // wait up to 5s for lock
      db.run(sql, params, function(e) { db.close(); e ? rej(e) : res({ id: this.lastID, changes: this.changes }); });
    });
  }), DB_TIMEOUT, sql.slice(0, 40));
}
function dbGet(sql, params = []) {
  return withTimeout(new Promise((res, rej) => {
    const db = new sqlite.Database(DB_PATH, sqlite.OPEN_READONLY, err => {
      if (err) return rej(err);
      db.configure('busyTimeout', 5000);
      db.get(sql, params, (e, row) => { db.close(); e ? rej(e) : res(row); });
    });
  }), DB_TIMEOUT, sql.slice(0, 40));
}
function dbAll(sql, params = []) {
  return withTimeout(new Promise((res, rej) => {
    const db = new sqlite.Database(DB_PATH, sqlite.OPEN_READONLY, err => {
      if (err) return rej(err);
      db.configure('busyTimeout', 5000);
      db.all(sql, params, (e, rows) => { db.close(); e ? rej(e) : res(rows); });
    });
  }), DB_TIMEOUT, sql.slice(0, 40));
}

// ─── File read helper ─────────────────────────────────────────────────────────
function readFile(p) {
  try { return fs.readFileSync(p, 'utf8'); }
  catch { return ''; }
}

// ─── Telegram notify — с таймаутом 15с на запрос ─────────────────────────────
const https = require('https');
const TG_TIMEOUT = 15000;

function tgSend(text, opts = {}) {
  const TOKEN    = process.env.TELEGRAM_BOT_TOKEN;
  const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
  if (!TOKEN || !ADMIN_IDS.length) return Promise.resolve();
  const sends = ADMIN_IDS.map(chatId => new Promise(resolve => {
    const body = { chat_id: chatId, text: text.slice(0, 4000), disable_web_page_preview: true, ...opts };
    const payload = JSON.stringify(body);
    let settled = false;
    const done = () => { if (!settled) { settled = true; resolve(); } };

    const req = https.request({
      hostname: 'api.telegram.org',
      path: `/bot${TOKEN}/sendMessage`,
      method: 'POST',
      timeout: TG_TIMEOUT,
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) }
    }, res => { res.on('data', ()=>{}); res.on('end', done); });

    req.on('error', done);
    req.on('timeout', () => { req.destroy(); done(); });
    req.write(payload);
    req.end();

    // Hard fallback
    setTimeout(done, TG_TIMEOUT + 1000);
  }));
  return Promise.allSettled(sends);
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

  addFinding(sev, msg, fixable = false) {
    this.findings.push({ sev: SEV[sev] || sev, msg, fixable });
  }

  addFixed(msg) {
    this.fixed.push(msg);
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
        `${this.emoji} *${this.name}* [${this.organ}]`,
        '',
        ...this.findings.filter(f => [SEV.CRITICAL, SEV.HIGH].includes(f.sev)).map(f => `${f.sev} ${f.msg}`),
        ...this.fixed.map(m => `🔧 ${m}`),
      ].join('\n');
      await tgSend(detail, { parse_mode: 'Markdown' });
    }

    return { findings: this.findings, fixed: this.fixed, elapsed };
  }
}

module.exports = {
  Agent, dbRun, dbGet, dbAll, readFile, logAgent, tgSend, SEV,
  BOT_PATH, SRV_PATH, API_PATH, DB_MOD, DB_PATH
};
