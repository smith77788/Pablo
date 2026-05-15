/** ✨ Response Formatter — Speech Center | Форматування, emoji, читабельність */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class ResponseFormatter extends Agent {
  constructor() {
    super({ id:'18', name:'Response Formatter', organ:'Speech Center', emoji:'✨',
      focus:'Message formatting consistency, emoji, readability' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. Emoji у іконках статусів
    const statusLabels = src.match(/STATUS_LABELS\s*=\s*\{([^}]+)\}/)?.[1] || '';
    const statusEmojis = (statusLabels.match(/[🆕🔍✅▶️🏁❌]/gu)||[]).length;
    if (statusEmojis < 4) this.addFinding('MEDIUM','Emoji у STATUS_LABELS відсутні або неповні');
    else this.addFinding('OK',`Emoji статусів: ${statusEmojis}`);

    // 2. Однаковий стиль заголовків (жирний або зірочки)
    const boldHeaders = (src.match(/\*\*[^*]+\*\*/g)||[]).length;
    const singleBold  = (src.match(/\*[^*]+\*/g)||[]).length;
    if (boldHeaders > 0) this.addFinding('LOW',`Використовується **double bold** (${boldHeaders}) — у Telegram це некоректно, потрібен *single*`);
    else this.addFinding('OK',`Жирний шрифт через *single asterisk*: ${singleBold} вживань`);

    // 3. Порожні рядки між блоками для читабельності
    const doubleNewlines = (src.match(/\\n\\n/g)||[]).length;
    if (doubleNewlines < 10) this.addFinding('LOW',`Лише ${doubleNewlines} подвійних переносів — повідомлення можуть виглядати "злипшись"`);
    else this.addFinding('OK',`${doubleNewlines} подвійних переносів — хороше розділення блоків`);

    // 4. stepHeader функція для прогресу бронювання
    if (src.includes('stepHeader')) this.addFinding('OK','Прогрес-індикатор бронювання (●○○○) реалізований');
    else this.addFinding('MEDIUM','Немає візуального прогресу кроків бронювання');

    // 5. Единицы измерения — русские/украинские буквы не нужно экранировать в MarkdownV2
    // Экранировать нужно только: _ * [ ] ( ) ~ ` > # + - = | { } . !
    // Проверяем только потенциально проблемные паттерны: число + точка + единица
    const dangerousUnits = (src.match(/\d+\.\s*(?:кг|см|м|ч|мин|руб|₽)/g)||[]).filter(m => !m.includes('\\.'));
    if (dangerousUnits.length > 3) {
      this.addFinding('LOW', `MarkdownV2: ${dangerousUnits.length} паттернов "число.единица" без экранирования точки`);
    } else {
      this.addFinding('OK', 'Единицы измерения используются безопасно');
    }
  }
}

if (require.main === module) new ResponseFormatter().run().then(() => process.exit(0));
module.exports = ResponseFormatter;
