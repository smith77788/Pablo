/** 👑 Admin Experience — Frontal Lobe | Повнота адмін-панелі у боті */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class AdminExperience extends Agent {
  constructor() {
    super({ id:'06', name:'Admin Experience', organ:'Frontal Lobe', emoji:'👑',
      focus:'Admin panel completeness, all actions available' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. Адмін бачить нові заявки з лічильником
    if (!src.includes('badge') && !src.includes('newO')) this.addFinding('MEDIUM','Лічильник нових заявок у меню адміна відсутній');
    else this.addFinding('OK','Лічильник нових заявок у меню адміна є');

    // 2. Фільтри заявок по статусу
    const filters = ['adm_orders_new','adm_orders_confirmed','adm_orders_completed'];
    const missing = filters.filter(f => !src.includes(f));
    if (missing.length) this.addFinding('MEDIUM',`Відсутні фільтри: ${missing.join(', ')}`);
    else this.addFinding('OK','Фільтри заявок по статусу присутні');

    // 3. Адмін може надіслати повідомлення клієнту
    if (!src.includes('adm_contact_')) this.addFinding('HIGH','Кнопка "Написати клієнту" відсутня у адмін-панелі');
    else this.addFinding('OK','Зв\'язок адмін → клієнт через бота є');

    // 4. Статистика доступна
    if (!src.includes('showAdminStats')) this.addFinding('LOW','Статистика недоступна адміну через бота');
    else this.addFinding('OK','Статистика доступна в адмін-меню');

    // 5. Фід агентів доступний
    if (!src.includes('agent_feed')) this.addFinding('LOW','Фід агентів відсутній у меню адміна');
    else this.addFinding('OK','Фід агентів доступний адміну');

    // 6. Управління моделями
    if (!src.includes('adm_toggle_')) this.addFinding('MEDIUM','Перемикання доступності моделей відсутнє у боті');
    else this.addFinding('OK','Управління доступністю моделей є');

    // 7. /msg команда для швидкої відповіді
    if (!src.includes('/msg')) this.addFinding('LOW','Команда /msg для швидкої відповіді клієнту відсутня');
    else this.addFinding('OK','Команда /msg присутня');
  }
}

if (require.main === module) new AdminExperience().run().then(() => process.exit(0));
module.exports = AdminExperience;
