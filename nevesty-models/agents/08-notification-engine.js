/** 🔔 Notification Engine — Endocrine System | Всі сповіщення і тригери */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class NotificationEngine extends Agent {
  constructor() {
    super({ id:'08', name:'Notification Engine', organ:'Endocrine System', emoji:'🔔',
      focus:'All notification triggers, admin + client alerts' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. Сповіщення адміна про нову заявку
    if (!src.includes('notifyNewOrder')) this.addFinding('CRITICAL','notifyNewOrder не викликається — адмін не дізнається про нові заявки!');
    else this.addFinding('OK','Адмін сповіщається про нові заявки');

    // 2. Сповіщення клієнта при зміні статусу
    if (!src.includes('notifyStatusChange')) this.addFinding('HIGH','notifyStatusChange відсутній — клієнт не отримає сповіщень про зміну статусу');
    else this.addFinding('OK','Сповіщення клієнта про зміну статусу є');

    // 3. Сповіщення при вхідному повідомленні від клієнта
    const fwdToAdmin = src.includes('getAdminChatIds') && src.includes('Promise.allSettled');
    if (!fwdToAdmin) this.addFinding('HIGH','Пересилання повідомлень клієнта адміну налаштовано некоректно');
    else this.addFinding('OK','Клієнтські повідомлення пересилаються всім адмінам');

    // 4. sendMessageToClient для відповідей менеджера
    if (!src.includes('sendMessageToClient')) this.addFinding('HIGH','sendMessageToClient відсутній — відповіді менеджера не доходять до клієнта');
    else this.addFinding('OK','sendMessageToClient реалізований');

    // 5. Promise.allSettled для безпечного масового надсилання
    const allSettled = (src.match(/Promise\.allSettled/g)||[]).length;
    if (allSettled < 2) this.addFinding('MEDIUM',`Promise.allSettled використовується лише ${allSettled} раз — надсилання може падати при помилці одного чату`);
    else this.addFinding('OK',`Promise.allSettled використовується ${allSettled} рази — безпечне масове надсилання`);

    // 6. Токен бота перевіряється перед стартом
    if (!src.includes('your_bot_token_here')) this.addFinding('MEDIUM','Перевірка токена бота при старті не знайдена');
    else this.addFinding('OK','Перевірка токена при ініціалізації є');
  }
}

if (require.main === module) new NotificationEngine().run().then(() => process.exit(0));
module.exports = NotificationEngine;
