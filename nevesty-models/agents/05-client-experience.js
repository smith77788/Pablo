/** 🌟 Client Experience — Sensory System | Привабливість повідомлень для клієнта */
const { Agent, readFile, BOT_PATH } = require('./lib/base');
const fs = require('fs');

class ClientExperience extends Agent {
  constructor() {
    super({ id:'05', name:'Client Experience', organ:'Sensory System', emoji:'🌟',
      focus:'Client message quality, CTAs, emoji, clarity' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. Вітальне повідомлення має emoji і ім'я
    if (!src.includes('Добро пожаловать')) this.addFinding('MEDIUM','Вітальне повідомлення відсутнє або змінено');
    if (!src.includes('firstName') && !src.includes('first_name')) this.addFinding('LOW','Ім\'я клієнта не використовується у привітанні');
    else this.addFinding('OK','Персоналізоване привітання з іменем присутнє');

    // 2. Повідомлення про успішне бронювання
    if (!src.includes('Заявка принята') && !src.includes('Заявка оформлена')) {
      this.addFinding('HIGH','Відсутнє підтвердження успішного бронювання для клієнта');
    } else { this.addFinding('OK','Підтвердження бронювання є'); }

    // 3. Сповіщення клієнту про зміну статусу (конкретні повідомлення)
    const statusMsgs = ['подтверждена','принята в работу','завершена','отклонена'];
    const missing = statusMsgs.filter(m => !src.includes(m));
    if (missing.length) this.addFinding('MEDIUM',`Відсутні сповіщення про статуси: ${missing.join(', ')}`);
    else this.addFinding('OK','Всі сповіщення клієнту про статуси присутні');

    // 4. Emoji у клієнтських повідомленнях
    const emojiCount = (src.match(/[\u{1F300}-\u{1F9FF}]/gu)||[]).length;
    if (emojiCount < 30) this.addFinding('LOW',`Мало emoji у повідомленнях (${emojiCount}) — повідомлення виглядають холодно`);
    else this.addFinding('OK',`${emojiCount} emoji використовується — повідомлення живі`);

    // 5. Клієнт може перевірити статус без команд
    if (!src.includes('check_status') && !src.includes('Проверить статус')) {
      this.addFinding('MEDIUM','Немає кнопки перевірки статусу у головному меню — клієнт не знає як перевірити');
    } else { this.addFinding('OK','Перевірка статусу доступна з головного меню'); }

    // 6. Клієнт отримує номер замовлення після бронювання
    if (!src.includes('orderNum') && !src.includes('order_number')) {
      this.addFinding('CRITICAL','Номер замовлення не надсилається клієнту після бронювання!');
    } else { this.addFinding('OK','Клієнт отримує номер замовлення'); }

    // 7. Пропозиція зв'язатись з менеджером
    if (!src.includes('менеджер') && !src.includes('Менеджер')) {
      this.addFinding('LOW','Немає згадки про менеджера — клієнт не знає до кого звертатись');
    }
  }
}

if (require.main === module) new ClientExperience().run().then(() => process.exit(0));
module.exports = ClientExperience;
