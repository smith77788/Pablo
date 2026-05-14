/**
 * 🔧 Bot Fixer — automatically patches bot.js
 * Invoked by smart-orchestrator when bot.js analysis findings are CRITICAL/HIGH.
 */
'use strict';
const { Agent } = require('../lib/base');
const fs   = require('fs');
const path = require('path');

const BOT_PATH = path.join(__dirname, '../../bot.js');

class BotFixer extends Agent {
  constructor() {
    super({ id: 'BF', name: 'Bot Fixer', emoji: '🔧', organ: 'Auto-Surgeon', focus: 'Patches bot.js automatically' });
    this.patched = [];
  }

  async analyze() {
    let src = fs.readFileSync(BOT_PATH, 'utf8');
    let changed = false;

    // ── Fix 1: queries without LIMIT on large tables ──────────────────────────
    const noLimitPattern = /query\(`SELECT [^`]*FROM (models|orders|messages|agent_logs|agent_findings|agent_discussions)[^`]*(?<!LIMIT \d+)`/g;
    const matches = src.match(noLimitPattern) || [];
    if (matches.length > 0) {
      this.addFinding('MEDIUM', `${matches.length} SELECT без LIMIT`);
    }

    // ── Fix 2: safeSend without 4096 truncation — already handled in safeSend helper ──

    // ── Fix 3: bot.sendMessage direct calls (bypassing safeSend) ─────────────
    const directSend = (src.match(/bot\.sendMessage\([^,]+,\s*[^,]+(?!\s*\.\s*catch)/g) || []).length;
    if (directSend > 3) {
      this.addFinding('LOW', `${directSend} прямых bot.sendMessage вместо safeSend`);
    }

    // ── Fix 4: parse_mode: 'Markdown' instead of MarkdownV2 ──────────────────
    const markdownOld = (src.match(/parse_mode:\s*['"]Markdown['"]/g) || [])
      .filter(m => !m.includes('MarkdownV2'));
    if (markdownOld.length > 0) {
      src = src.replace(/parse_mode:\s*'Markdown'(?!\s*V2)/g, "parse_mode: 'MarkdownV2'");
      this.patched.push(`parse_mode Markdown → MarkdownV2 (${markdownOld.length} мест)`);
      changed = true;
    }

    // ── Fix 5: showAdminOrders called without page arg ────────────────────────
    const missingPage = src.match(/showAdminOrders\(chatId,\s*\d+\)(?!\s*;?\s*\/\/)/g);
    if (missingPage) {
      src = src.replace(/showAdminOrders\(chatId,\s*(\d+)\)(?!\s*;?\s*\/\/)/g,
        'showAdminOrders(chatId, $1, 0)');
      this.patched.push('showAdminOrders: добавлен аргумент page=0');
      changed = true;
    }

    // ── Apply pending findings passed from orchestrator ───────────────────────
    if (this._pendingFindings) {
      for (const f of this._pendingFindings) {
        const msg = (f.msg || '').toLowerCase();

        // Fix: missing PRAGMA / WAL
        if (msg.includes('wal') || msg.includes('pragma') || msg.includes('synchronous')) {
          this.addFinding('INFO', 'WAL/PRAGMA: уже настроены в database.js');
        }
      }
    }

    if (changed) {
      fs.writeFileSync(BOT_PATH, src, 'utf8');
      for (const msg of this.patched) {
        this.addFixed(msg);
        console.log(`  🔧 BotFixer: ${msg}`);
      }
    }

    this.addFinding('OK', `BotFixer проверил ${path.basename(BOT_PATH)}: ${this.patched.length} патчей`);
  }
}

if (require.main === module) {
  const f = new BotFixer();
  f.run().then(r => {
    console.log(`[BotFixer] fixed: ${r.fixed?.length || 0}, findings: ${r.findings?.length || 0}`);
    r.fixed?.forEach(m => console.log(`  🔧 ${m}`));
    process.exit(0);
  }).catch(e => { console.error(e); process.exit(1); });
}
module.exports = BotFixer;
