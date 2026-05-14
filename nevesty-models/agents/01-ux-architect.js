/** 🧠 UX Architect — Cerebral Cortex | Аналізує структуру меню, глибину навігації, кнопки */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class UXArchitect extends Agent {
  constructor() {
    super({ id:'01', name:'UX Architect', organ:'Cerebral Cortex', emoji:'🧠',
      focus:'Menu depth, button layout, navigation completeness' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);
    if (!src) return this.addFinding('HIGH','bot.js не найден');

    // 1. Каждая клавиатура должна иметь кнопку "Назад" или "Меню"
    const keyboards = src.match(/inline_keyboard:\s*\[[\s\S]*?\]/g) || [];
    let noBack = 0;
    keyboards.forEach(kb => {
      const hasBack = /main_menu|Меню|Назад|← /i.test(kb);
      if (!hasBack) noBack++;
    });
    if (noBack > 0) this.addFinding('MEDIUM', `${noBack} клавіатур без кнопки "Назад/Меню" — користувач може загубитись`);
    else this.addFinding('OK','Всі клавіатури мають навігацію');

    // 2. Не більше 4 кнопок в одному рядку
    const rows = src.match(/\[{[^}]+},\s*{[^}]+},\s*{[^}]+},\s*{[^}]+},\s*{/g) || [];
    if (rows.length > 0) this.addFinding('MEDIUM',`${rows.length} рядків з >4 кнопками — перевантаження UI`);

    // 3. Кнопки "Головне меню" у клієнтських меню
    const mainMenuRefs = (src.match(/main_menu/g) || []).length;
    if (mainMenuRefs < 5) this.addFinding('LOW','Мало посилань на головне меню — додай у більше місць');
    else this.addFinding('OK',`Головне меню доступне з ${mainMenuRefs} точок`);

    // 4. Довжина повідомлень
    const longTexts = src.match(/`[^`]{2500,}`/g) || [];
    if (longTexts.length) this.addFinding('LOW',`${longTexts.length} повідомлень >2500 символів — ризик обрізання Telegram`);

    // 5. Кожен крок бронювання показує прогрес
    const stepHeaders = (src.match(/stepHeader/g) || []).length;
    if (stepHeaders < 8) this.addFinding('MEDIUM',`stepHeader викликається лише ${stepHeaders} разів — не всі кроки показують прогрес`);
    else this.addFinding('OK',`Прогрес бронювання показано у ${stepHeaders} кроках`);
  }
}

if (require.main === module) new UXArchitect().run().then(r => { process.exit(0); });
module.exports = UXArchitect;
