/** 💬 Message Threading — Neural Network | Зв'язок адмін ↔ клієнт через чат */
const { Agent, readFile, dbAll, BOT_PATH } = require('./lib/base');

class MessageThreading extends Agent {
  constructor() {
    super({ id:'07', name:'Message Threading', organ:'Neural Network', emoji:'💬',
      focus:'Admin↔client messaging, DB storage, forwarding' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. Повідомлення зберігаються у БД
    if (!src.includes("INSERT INTO messages")) this.addFinding('HIGH','Повідомлення не зберігаються у таблицю messages');
    else this.addFinding('OK','Повідомлення зберігаються у БД');

    // 2. sender_type зберігається
    if (!src.includes("sender_type")) this.addFinding('MEDIUM','sender_type не зберігається — неможливо розрізнити відправника');
    else this.addFinding('OK','sender_type (admin/client) зберігається');

    // 3. Стан replying у адміна
    if (!src.includes("'replying'")) this.addFinding('HIGH','Стан replying відсутній — адмін не може відповідати клієнту через бот');
    else this.addFinding('OK','Стан replying для адміна реалізований');

    // 4. Пересилання клієнтських повідомлень адміну
    if (!src.includes('getAdminChatIds')) this.addFinding('HIGH','Пересилання повідомлень адміну відсутнє');
    else this.addFinding('OK','Клієнтські повідомлення пересилаються всім адмінам');

    // 5. Повідомлення від адміна зберігаються з username
    if (!src.includes('username||') && !src.includes("username ||")) this.addFinding('LOW','Username адміна не зберігається у повідомленнях');
    else this.addFinding('OK','Username адміна зберігається у повідомленнях');

    // 6. Реальні повідомлення у БД
    try {
      const msgs = await dbAll('SELECT COUNT(*) as n FROM messages');
      this.addFinding('INFO',`БД: ${msgs[0].n} повідомлень у системі`);
    } catch {}

    // 7. Клієнту показуються останні повідомлення від менеджера
    if (!src.includes('messages WHERE order_id')) this.addFinding('MEDIUM','Клієнт не бачить повідомлення від менеджера у деталях замовлення');
    else this.addFinding('OK','Клієнт бачить останні повідомлення від менеджера');
  }
}

if (require.main === module) new MessageThreading().run().then(() => process.exit(0));
module.exports = MessageThreading;
