/** 🐛 Bug Hunter — Antibody | Ищет баги в коде самих агентов и бота */
const { Agent, readFile, BOT_PATH } = require('./lib/base');
const path = require('path');
const fs   = require('fs');

class BugHunter extends Agent {
  constructor() {
    super({ id:'BH', name:'Bug Hunter', organ:'Antibody', emoji:'🐛',
      focus:'Bugs in agent code, bot.js anti-patterns, dangerous patterns' });
  }

  async analyze() {
    const botSrc = readFile(BOT_PATH);

    // === Проверка bot.js ===

    // 1. Не перехваченные Promise (fire and forget без catch)
    const unhandled = (botSrc.match(/bot\.\w+\([^)]+\)(?!\s*\.\s*catch)(?!\s*;?\s*\/\/)/g)||[]).length;
    this.addFinding('INFO', `bot.js: ~${unhandled} вызовов bot.* — проверь наличие .catch() или try/catch`);

    // 2. process.exit в боте (кроме тестов)
    if (botSrc.includes('process.exit(') && !botSrc.includes('// process.exit')) {
      this.addFinding('MEDIUM', 'bot.js: process.exit() обнаружен — аварийное завершение без graceful shutdown');
    } else {
      this.addFinding('OK', 'process.exit() в bot.js не обнаружен');
    }

    // 3. Infinite recursion risk
    const selfCalls = botSrc.match(/async function (\w+)[^{]+\{[^}]*\1\s*\(/g) || [];
    if (selfCalls.length > 0) {
      this.addFinding('HIGH', `bot.js: Рекурсивные вызовы функций — риск stack overflow: ${selfCalls.length} случаев`);
    } else {
      this.addFinding('OK', 'Рекурсия в bot.js не обнаружена');
    }

    // 4. Callback answer timeouts (answerCallbackQuery должен вызываться везде)
    const callbackQueries = (botSrc.match(/on\s*\(\s*'callback_query'/g)||[]).length;
    const answerCalls = (botSrc.match(/answerCallbackQuery/g)||[]).length;
    if (callbackQueries > 0 && answerCalls < 2) {
      this.addFinding('HIGH', `bot.js: answerCallbackQuery вызывается ${answerCalls} раз — Telegram кнопки будут "зависать"`);
    } else {
      this.addFinding('OK', `answerCallbackQuery вызывается ${answerCalls} раз — кнопки не зависают`);
    }

    // 5. Проверка агентов на базовые паттерны
    const agentsDir = path.join(__dirname);
    const agentFiles = fs.readdirSync(agentsDir).filter(f => /^\d+-.+\.js$/.test(f));
    let agentBugs = 0;
    for (const af of agentFiles) {
      const src = fs.readFileSync(path.join(agentsDir, af), 'utf8');
      if (!src.includes('module.exports')) { agentBugs++; }
      if (!src.includes('async analyze()'))  { agentBugs++; }
    }
    if (agentBugs > 0) {
      this.addFinding('MEDIUM', `Агенты: ${agentBugs} файлов без module.exports или analyze() — сломают orchestrator`);
    } else {
      this.addFinding('OK', `Все ${agentFiles.length} агентов имеют правильную структуру`);
    }

    // 6. Необработанные ошибки polling
    if (!botSrc.includes('polling_error') && !botSrc.includes('on(\'error\'')) {
      this.addFinding('HIGH', 'bot.js: Нет обработчика polling_error — сетевые ошибки могут крашить бота');
    } else {
      this.addFinding('OK', 'Обработчик polling_error присутствует');
    }

    // 7. Утечки памяти — глобальные Map без cleanup
    const globalMaps = (botSrc.match(/^(?:const|let)\s+\w+\s*=\s*new Map\(\)/gm)||[]).length;
    if (globalMaps > 3) {
      this.addFinding('LOW', `bot.js: ${globalMaps} глобальных Map — убедись что они очищаются (delete/clear)`);
    } else {
      this.addFinding('OK', `Глобальных Map: ${globalMaps} — в норме`);
    }

    // 8. Длинные функции (>200 строк — сложность, риск багов)
    const functions = botSrc.match(/async function \w+/g) || [];
    this.addFinding('INFO', `bot.js: ${functions.length} async функций — если есть функции >200 строк, рассмотри рефактор`);
  }
}

if (require.main === module) new BugHunter().run().then(() => process.exit(0));
module.exports = BugHunter;
