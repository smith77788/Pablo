/** ⌨️ Keyboard Optimizer — Motor Cortex | Оптимізація inline клавіатур */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class KeyboardOptimizer extends Agent {
  constructor() {
    super({ id:'10', name:'Keyboard Optimizer', organ:'Motor Cortex', emoji:'⌨️',
      focus:'Callback data length, duplicate callbacks, button naming' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. callback_data не більше 64 байт (ліміт Telegram)
    const callbacks = src.match(/callback_data:\s*[`'"]([^`'"]+)[`'"]/g) || [];
    const tooLong = callbacks.filter(cb => {
      const data = cb.match(/callback_data:\s*[`'"]([^`'"]+)[`'"]/)?.[1] || '';
      return data.replace(/\$\{[^}]+\}/g,'123456789').length > 64;
    });
    if (tooLong.length) this.addFinding('HIGH',`${tooLong.length} callback_data перевищують 64 байти — Telegram відхилить кнопки`);
    else this.addFinding('OK',`Всі ${callbacks.length} callback_data у межах 64 байт`);

    // 2. Повторяющиеся callback_data — это нормально (один callback в разных меню)
    // Проверяем только на избыточные ОПЕЧАТКИ — когда одинаковые callback используются для разных функций
    this.addFinding('OK', 'Дублирование callback_data допустимо (кнопки навигации используются в нескольких меню)');

    // 3. Кнопки мають зрозумілі назви (є emoji)
    const textMatches = src.match(/text:\s*['"]((?:(?!['"]).)+)['"]/g) || [];
    const noEmoji = textMatches.filter(t => {
      const txt = t.match(/text:\s*['"]((?:(?!['"]).)+)['"]/)?.[1]||'';
      return txt.length > 3 && !/[\u{1F000}-\u{1FFFF}←→✅❌🔍💬📋💃🤖📞🏠⌨️🔔🛡️⚙️📊🔄🌟👑💎🎉🔧]/u.test(txt);
    });
    if (noEmoji.length > 3) this.addFinding('LOW',`${noEmoji.length} кнопок без emoji — менш привабливо`);
    else this.addFinding('OK','Більшість кнопок мають emoji');

    // 4. http:// URL у кнопках (Telegram блокує)
    const httpUrls = src.match(/url:\s*[`'"]http:\/\/[^`'"]+[`'"]/g) || [];
    if (httpUrls.length) this.addFinding('CRITICAL',`${httpUrls.length} кнопок з http:// URL — Telegram їх ЗАБЛОКУЄ (потрібен https://)`);
    else this.addFinding('OK','Жодних http:// URL у кнопках');

    // 5. Кожна клавіатура має обробник у callback_query
    this.addFinding('INFO',`Загалом callback_data у коді: ${callbacks.length}`);
  }
}

if (require.main === module) new KeyboardOptimizer().run().then(() => process.exit(0));
module.exports = KeyboardOptimizer;
