/** ⚡ Performance Tuner — Mitochondria | Параллельные запросы, кэш, await */
const { Agent, readFile, BOT_PATH, API_PATH } = require('./lib/base');

class PerformanceTuner extends Agent {
  constructor() {
    super({ id:'24', name:'Performance Tuner', organ:'Mitochondria', emoji:'⚡',
      focus:'Sequential vs parallel awaits, DB query efficiency, no N+1' });
  }
  async analyze() {
    const botSrc = readFile(BOT_PATH);
    const apiSrc = readFile(API_PATH);

    // 1. Sequential awaits where parallel is possible (N+1 pattern)
    const seqAwaits = botSrc.match(/await\s+\w+[^;]+;\s*\n\s*const\s+\w+\s*=\s*await\s+\w+/g) || [];
    if (seqAwaits.length > 5) {
      this.addFinding('MEDIUM', `bot.js: ${seqAwaits.length} последовательных await — некоторые можно заменить Promise.all для ускорения`);
    } else {
      this.addFinding('OK', `bot.js: Последовательных await найдено: ${seqAwaits.length} — в норме`);
    }

    // 2. Promise.all usage
    const promiseAll = (botSrc.match(/Promise\.all/g)||[]).length;
    const promiseAllSettled = (botSrc.match(/Promise\.allSettled/g)||[]).length;
    if (promiseAll + promiseAllSettled === 0) {
      this.addFinding('LOW', 'Promise.all/allSettled не используется — параллельный запуск задач не применяется');
    } else {
      this.addFinding('OK', `Параллельное выполнение: Promise.all×${promiseAll}, Promise.allSettled×${promiseAllSettled}`);
    }

    // 3. SELECT * usage (неэффективно)
    const selectStar = (botSrc.match(/SELECT \*/g)||[]).length;
    if (selectStar > 5) {
      this.addFinding('LOW', `bot.js: SELECT * используется ${selectStar} раз — лучше указывать конкретные столбцы`);
    } else {
      this.addFinding('OK', `bot.js: SELECT * в норме (${selectStar} раз)`);
    }

    // 4. N+1 в цикле
    const loopWithAwait = botSrc.match(/for\s*\(.*\)\s*\{[^}]*await\s+db/g) || [];
    if (loopWithAwait.length > 0) {
      this.addFinding('MEDIUM', `bot.js: ${loopWithAwait.length} случаев await DB внутри for-цикла — N+1 проблема`);
    } else {
      this.addFinding('OK', 'N+1 запросов в циклах не обнаружено');
    }

    // 5. Кэширование (простое)
    const hasCache = botSrc.includes('cache') || botSrc.includes('Cache') || botSrc.includes('Map()');
    if (!hasCache) {
      this.addFinding('INFO', 'Кэширование не используется — при высокой нагрузке можно добавить in-memory cache для каталога');
    } else {
      this.addFinding('OK', 'Кэш механизм обнаружен');
    }

    // 6. LIMIT в API запросах
    const apiLimits = (apiSrc.match(/LIMIT \d+/g)||[]).length;
    if (apiLimits < 3) {
      this.addFinding('MEDIUM', `api.js: только ${apiLimits} запросов с LIMIT — без ограничения результаты могут быть огромными`);
    } else {
      this.addFinding('OK', `api.js: LIMIT используется в ${apiLimits} запросах`);
    }

    // 7. Indexes — JOIN без индексов
    const hasJoin = (botSrc.match(/JOIN/g)||[]).length;
    if (hasJoin > 3) {
      this.addFinding('INFO', `bot.js: ${hasJoin} JOIN запросов — убедитесь что FK столбцы имеют индексы (агент 11 проверяет)`);
    }
  }
}

if (require.main === module) new PerformanceTuner().run().then(() => process.exit(0));
module.exports = PerformanceTuner;
