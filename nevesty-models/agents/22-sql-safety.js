/** 💉 SQL Safety — Liver | SQL injection patterns across all files */
const { Agent, readFile, BOT_PATH, API_PATH } = require('./lib/base');
const path = require('path');
const fs = require('fs');

class SqlSafety extends Agent {
  constructor() {
    super({ id:'22', name:'SQL Safety', organ:'Liver', emoji:'💉',
      focus:'SQL injection prevention, parameterized queries, no string interpolation in SQL' });
  }
  async analyze() {
    const botSrc = readFile(BOT_PATH);
    const apiSrc = readFile(API_PATH);
    const dbPath = path.join(__dirname, '../database.js');
    const dbSrc  = fs.existsSync(dbPath) ? fs.readFileSync(dbPath, 'utf8') : '';

    const sources = { 'bot.js': botSrc, 'api.js': apiSrc, 'database.js': dbSrc };

    for (const [fname, src] of Object.entries(sources)) {
      if (!src) continue;

      // 1. Template literals с ПРЯМОЙ подстановкой пользовательских данных
      // Безопасно: WHERE ${where} где where = строка из кода, не из req.query/msg.text
      const dangerousPatterns = [
        /(?:query|run|get)\s*\(`[^`]*\$\{(?:req\.query\.|req\.body\.|msg\.|text\b|chatId|userId|username)/g,
        /(?:query|run|get)\s*\(`[^`]*\$\{[a-zA-Z]+(?:Filter|Input|Search|Name|Id)\b/g,
      ];
      let dangerCount = 0;
      for (const re of dangerousPatterns) { dangerCount += (src.match(re)||[]).length; }
      // Также проверяем fieldMap pattern - безопасно только если есть проверка
      const fieldmapUnsafe = (src.match(/SET\s+\$\{[^}]+\}\s*=/g)||[]).filter(m => {
        const idx = src.indexOf(m);
        const before = src.substring(Math.max(0,idx-200), idx);
        return !before.includes('fieldMap[') && !before.includes('ALLOWED_') && !before.includes('whitelist');
      }).length;
      if (dangerCount + fieldmapUnsafe > 0) {
        this.addFinding('CRITICAL', `${fname}: ${dangerCount + fieldmapUnsafe} SQL-запросов с небезопасной интерполяцией пользовательских данных`);
      } else {
        this.addFinding('OK', `${fname}: Небезопасная интерполяция в SQL не обнаружена`);
      }

      // 2. String concatenation in SQL
      const concatSql = src.match(/(?:WHERE|AND|SET)\s+\w+\s*=\s*['"]?\s*\+\s*/g) || [];
      if (concatSql.length > 0) {
        this.addFinding('HIGH', `${fname}: Конкатенация строк в SQL (${concatSql.length} случаев)`);
      }

      // 3. Parameterized queries usage
      const paramCount = (src.match(/\?\s*(?:,|\))/g)||[]).length;
      if (paramCount < 5 && src.includes('SELECT')) {
        this.addFinding('MEDIUM', `${fname}: Мало параметризованных запросов (${paramCount}) — возможна небезопасная подстановка`);
      } else if (paramCount >= 5) {
        this.addFinding('OK', `${fname}: Параметризованные запросы используются (${paramCount} параметров)`);
      }
    }

    // 4. VALID_STATUSES whitelist check in bot.js
    if (!botSrc.includes('VALID_STATUSES')) {
      this.addFinding('HIGH', 'bot.js: VALID_STATUSES whitelist отсутствует — statusFilter не валидируется');
    } else {
      this.addFinding('OK', 'bot.js: VALID_STATUSES whitelist для валидации статусов присутствует');
    }

    // 5. User input sanitization
    if (!botSrc.includes('.trim()')) {
      this.addFinding('MEDIUM', 'bot.js: .trim() не используется — пробелы в данных пользователя не очищаются');
    } else {
      const trimCount = (botSrc.match(/\.trim\(\)/g)||[]).length;
      this.addFinding('OK', `bot.js: .trim() используется ${trimCount} раз`);
    }

    // 6. No eval() or Function() with user input
    if (botSrc.includes('eval(') || botSrc.includes('new Function(')) {
      this.addFinding('CRITICAL', 'bot.js: eval() или new Function() обнаружены — критическая RCE уязвимость');
    } else {
      this.addFinding('OK', 'Нет eval() или new Function() — нет риска RCE');
    }
  }
}

if (require.main === module) new SqlSafety().run().then(() => process.exit(0));
module.exports = SqlSafety;
