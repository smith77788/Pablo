/** 📋 Booking Completeness — Digestive System | Перевіряє що всі поля сайту є у боті */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class BookingCompleteness extends Agent {
  constructor() {
    super({ id:'02', name:'Booking Completeness', organ:'Digestive System', emoji:'📋',
      focus:'All website booking fields mirrored in bot wizard' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);
    if (!src) return this.addFinding('HIGH','bot.js не найден');

    // Обов'язкові поля з сайту (booking.js)
    const required = {
      'event_type':     /event_type/,
      'event_date':     /event_date/,
      'event_duration': /event_duration/,
      'location':       /location/,
      'budget':         /budget/,
      'comments':       /comments/,
      'client_name':    /client_name/,
      'client_phone':   /client_phone/,
      'client_email':   /client_email/,
      'client_telegram':/client_telegram/,
      'model_id':       /model_id/,
    };

    let missing = [];
    for (const [field, re] of Object.entries(required)) {
      if (!re.test(src)) missing.push(field);
    }
    if (missing.length) this.addFinding('CRITICAL', `Відсутні поля: ${missing.join(', ')}`);
    else this.addFinding('OK','Всі поля сайту присутні в боті');

    // Перевірка 4 кроків
    const steps = ['bkStep1','bkStep2','bkStep3','bkStep4'];
    const missSteps = steps.filter(s => !src.includes(s));
    if (missSteps.length) this.addFinding('CRITICAL',`Відсутні функції кроків: ${missSteps.join(', ')}`);
    else this.addFinding('OK','Всі 4 кроки бронювання реалізовані');

    // Підтвердження заявки у 4 кроці
    if (!src.includes('bk_submit')) this.addFinding('CRITICAL','Callback bk_submit відсутній — заявки не відправляються');
    else this.addFinding('OK','Submit callback присутній');

    // INSERT INTO orders присутній
    if (!src.includes('INSERT INTO orders')) this.addFinding('CRITICAL','INSERT INTO orders відсутній у боті!');
    else this.addFinding('OK','Запис заявок у БД присутній');

    // Прив'язка client_chat_id
    if (!src.includes('client_chat_id')) this.addFinding('HIGH','client_chat_id не зберігається — клієнт не отримає сповіщення');
    else this.addFinding('OK','client_chat_id зберігається при бронюванні');
  }
}

if (require.main === module) new BookingCompleteness().run().then(() => process.exit(0));
module.exports = BookingCompleteness;
