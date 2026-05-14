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

// ─── Shared DB helpers (own connection — non-blocking) ────────────────────────
function dbRun(sql, params = []) {
  return new Promise((res, rej) => {
    const db = new sqlite.Database(DB_PATH);
    db.run(sql, params, function (err) {
      db.close();
      err ? rej(err) : res({ id: this.lastID, changes: this.changes });
    });
  });
}
function dbGet(sql, params = []) {
  return new Promise((res, rej) => {
    const db = new sqlite.Database(DB_PATH);
    db.get(sql, params, (err, row) => { db.close(); err ? rej(err) : res(row); });
  });
}
function dbAll(sql, params = []) {
  return new Promise((res, rej) => {
    const db = new sqlite.Database(DB_PATH);
    db.all(sql, params, (err, rows) => { db.close(); err ? rej(err) : res(rows); });
  });
}

// ─── File read helper ─────────────────────────────────────────────────────────
function readFile(p) {
  try { return fs.readFileSync(p, 'utf8'); }
  catch { return ''; }
}

// ─── Telegram notify ──────────────────────────────────────────────────────────
const https = require('https');
function tgSend(text) {
  const TOKEN    = process.env.TELEGRAM_BOT_TOKEN;
  const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
  if (!TOKEN || !ADMIN_IDS.length) return Promise.resolve();
  const sends = ADMIN_IDS.map(chatId => new Promise(resolve => {
    const payload = JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true });
    const req = https.request({
      hostname: 'api.telegram.org',
      path: `/bot${TOKEN}/sendMessage`,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) }
    }, res => { res.on('data', ()=>{}); res.on('end', resolve); });
    req.on('error', resolve);
    req.write(payload);
    req.end();
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

  /** Run the full agent lifecycle */
  async run() {
    const t0 = Date.now();
    const label = `Agent: ${this.name}`;

    await logAgent(label, `${this.emoji} [${this.organ}] активирован — ${this.focus}`);

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

    let summary = `${this.emoji} [${this.organ}] ${this.name} — ${elapsed}s\n`;
    if (this.findings.length === 0 && fixCount === 0) {
      summary += `✅ Всё в порядке`;
    } else {
      if (critical) summary += `🔴 Критических: ${critical}  `;
      if (high)     summary += `🟠 Важных: ${high}  `;
      summary += `Всего: ${this.findings.length}`;
      if (fixCount) summary += `  🔧 Исправлено: ${fixCount}`;
      this.findings.forEach(f => { summary += `\n${f.sev} ${f.msg}`; });
      this.fixed.forEach(m    => { summary += `\n✅ Fixed: ${m}`; });
    }

    await logAgent(label, summary);

    // Notify only if there's something interesting
    if (critical + high + fixCount > 0) {
      await tgSend(`🤖 ${label}\n${summary}`);
    }

    return { findings: this.findings, fixed: this.fixed, elapsed };
  }
}

module.exports = {
  Agent, dbRun, dbGet, dbAll, readFile, logAgent, tgSend, SEV,
  BOT_PATH, SRV_PATH, API_PATH, DB_MOD, DB_PATH
};
