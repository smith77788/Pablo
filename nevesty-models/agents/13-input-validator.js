/** ✅ Input Validator — Digestive Enzymes | Валідація всіх текстових введень */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class InputValidator extends Agent {
  constructor() {
    super({ id:'13', name:'Input Validator', organ:'Digestive Enzymes', emoji:'✅',
      focus:'Phone regex, email regex, name length, all inputs validated' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. Валідація телефону
    if (!src.includes('test(text)') && !src.includes('/^[\\d')) {
      this.addFinding('HIGH','Валідація телефону відсутня — будь-який текст буде прийнятий як номер');
    } else { this.addFinding('OK','Валідація телефону присутня'); }

    // 2. Валідація email
    if (!src.includes('@') || !src.includes('.test(text)')) {
      this.addFinding('MEDIUM','Валідація email відсутня або неповна');
    } else { this.addFinding('OK','Валідація email присутня'); }

    // 3. Мінімальна довжина імені
    if (!src.includes('length < 2') && !src.includes('length<2')) {
      this.addFinding('LOW','Мінімальна довжина імені не перевіряється');
    } else { this.addFinding('OK','Перевірка мінімальної довжини імені є'); }

    // 4. Обробка порожніх введень
    const emptyChecks = (src.match(/!text\b|\.trim\(\)/g)||[]).length;
    if (emptyChecks < 5) this.addFinding('MEDIUM',`Перевірок на порожній ввід лише ${emptyChecks} — деякі поля можуть приймати порожній рядок`);
    else this.addFinding('OK',`${emptyChecks} перевірок на порожній ввід`);

    // 5. Обрізка пробілів (trim)
    const trimCalls = (src.match(/\.trim\(\)/g)||[]).length;
    if (trimCalls < 3) this.addFinding('LOW',`trim() викликається лише ${trimCalls} разів — пробіли можуть зберігатись у БД`);
    else this.addFinding('OK',`trim() використовується ${trimCalls} разів`);

    // 6. Максимальна довжина повідомлення Telegram (4096)
    if (!src.includes('4096') && !src.includes('3900')) {
      this.addFinding('MEDIUM','Ліміт 4096 символів Telegram не перевіряється — довгі повідомлення будуть обрізані з помилкою');
    } else { this.addFinding('OK','Ліміт Telegram 4096 символів перевіряється'); }

    // 7. Ін'єкція @ у telegram username
    if (src.includes(".replace('@','')")) this.addFinding('OK','@ видаляється з Telegram username');
    else this.addFinding('LOW','@ може дублюватись у Telegram username');
  }
}

if (require.main === module) new InputValidator().run().then(() => process.exit(0));
module.exports = InputValidator;
