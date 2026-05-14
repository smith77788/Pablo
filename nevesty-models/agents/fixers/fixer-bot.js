/**
 * 🔧 Bot Fixer — patches bot.js automatically
 * Called by smart-orchestrator when bot.js issues are found
 */
const { Agent } = require('../lib/base');
const fs = require('fs');
const path = require('path');

const BOT_PATH = path.join(__dirname, '../../bot.js');

class BotFixer extends Agent {
  constructor() {
    super({ id: 'BF', name: 'Bot Fixer', emoji: '🔧', organ: 'Auto-Surgeon', focus: 'Patches bot.js automatically' });
    this.patched = [];
  }

  async analyze() {
    const src = fs.readFileSync(BOT_PATH, 'utf8');

    // Fix 1: queries without LIMIT that could return huge result sets
    const noLimitQueries = (src.match(/query\(['"](SELECT .+? FROM (models|orders|messages|agent_logs)(?!.{0,100}LIMIT))/g) || []);
    if (noLimitQueries.length > 0) {
      this.addFinding('MEDIUM', `${noLimitQueries.length} запросов без LIMIT — патчу`);
      await this.fixMissingLimits(src);
    }

    // Fix 2: bot.sendMessage calls without error handling
    const unguarded = (src.match(/bot\.sendMessage\([^)]+\)(?!\s*\.catch)/g) || []).length;
    if (unguarded > 5) {
      this.addFinding('MEDIUM', `${unguarded} bot.sendMessage без .catch — рекомендуется добавить safeSend`);
    }

    // Fix 3: Missing city column filter in showCatalog
    if (src.includes("SELECT * FROM models WHERE available=1 ORDER BY id")) {
      this.addFinding('LOW', 'showCatalog query — можно оптимизировать (добавить LIMIT и нужные колонки)');
    }

    this.addFinding('OK', `Bot Fixer проверил ${path.basename(BOT_PATH)}`);

    if (this.patched.length > 0) {
      for (const msg of this.patched) this.addFixed(msg);
    }
  }

  async fixMissingLimits(src) {
    // Add LIMIT to showAdminModels query that fetches ALL models
    let patched = src;
    if (patched.includes("query('SELECT * FROM models ORDER BY id DESC')")) {
      patched = patched.replace(
        "query('SELECT * FROM models ORDER BY id DESC')",
        "query('SELECT * FROM models ORDER BY id DESC LIMIT 500')"
      );
      this.patched.push('showAdminModels: добавлен LIMIT 500');
    }
    if (patched !== src) {
      fs.writeFileSync(BOT_PATH, patched);
    }
  }
}

if (require.main === module) {
  const f = new BotFixer();
  f.run().then(r => {
    console.log(`[BotFixer] findings: ${r.findings.length}, fixed: ${r.fixed.length}, elapsed: ${r.elapsed}s`);
    r.findings.forEach(f => console.log(`  ${f.sev} ${f.msg}`));
    r.fixed.forEach(m => console.log(`  🔧 ${m}`));
    process.exit(0);
  }).catch(e => { console.error(e); process.exit(1); });
}
module.exports = BotFixer;
