/** 🔁 Session Manager — Lymphatic System | Сесії, TTL, очищення */
const { Agent, readFile, dbAll, dbRun, BOT_PATH } = require('./lib/base');

class SessionManager extends Agent {
  constructor() {
    super({ id:'12', name:'Session Manager', organ:'Lymphatic System', emoji:'🔁',
      focus:'Session TTL, orphan cleanup, state machine integrity' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. clearSession викликається після завершення дій
    const clearCalls = (src.match(/clearSession/g)||[]).length;
    if (clearCalls < 3) this.addFinding('MEDIUM',`clearSession викликається лише ${clearCalls} разів — сесії можуть залишатись активними`);
    else this.addFinding('OK',`clearSession викликається ${clearCalls} разів`);

    // 2. Перевірка стану 'replying' — адмін не застрягне
    if (!src.includes("state === 'replying'")) this.addFinding('MEDIUM','Стан replying не обробляється у message handler');
    else this.addFinding('OK','Стан replying оброблюється');

    // 3. Реальні старі сесії у БД
    try {
      const old = await dbAll(
        "SELECT COUNT(*) as n FROM telegram_sessions WHERE state!='idle' AND updated_at < datetime('now','-1 hour')"
      );
      if (old[0].n > 0) {
        this.addFinding('MEDIUM',`${old[0].n} застряглих сесій старших 1 години — очищую...`);
        await dbRun("UPDATE telegram_sessions SET state='idle',data='{}' WHERE state!='idle' AND updated_at < datetime('now','-1 hour')");
        this.addFixed(`Очищено ${old[0].n} застряглих сесій`);
      } else {
        this.addFinding('OK','Застряглих сесій немає');
      }

      const total = await dbAll('SELECT COUNT(*) as n FROM telegram_sessions');
      this.addFinding('INFO',`Активних сесій у БД: ${total[0].n}`);
    } catch (e) { this.addFinding('LOW',`Не вдалось перевірити сесії: ${e.message}`); }

    // 4. getSession використовується перед зчитуванням даних
    const getSessionCalls = (src.match(/getSession/g)||[]).length;
    if (getSessionCalls < 5) this.addFinding('LOW',`getSession викликається лише ${getSessionCalls} разів`);
    else this.addFinding('OK',`getSession використовується ${getSessionCalls} разів`);

    // 5. sessionData (parsing) захищений від JSON помилок
    if (!src.includes('try {') || !src.includes("JSON.parse")) this.addFinding('LOW','JSON.parse сесії не захищений try/catch');
    else this.addFinding('OK','Парсинг даних сесії захищений від помилок');
  }
}

if (require.main === module) new SessionManager().run().then(() => process.exit(0));
module.exports = SessionManager;
