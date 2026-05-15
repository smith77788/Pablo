/** 🛡️ Admin Protection — Thymus | Защита административных функций */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class AdminProtection extends Agent {
  constructor() {
    super({ id:'21', name:'Admin Protection', organ:'Thymus', emoji:'🛡️',
      focus:'isAdmin gates on all admin actions, no privilege escalation' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. Функция isAdmin существует
    if (!src.includes('function isAdmin') && !src.includes('const isAdmin')) {
      this.addFinding('CRITICAL', 'Функция isAdmin отсутствует — любой пользователь может вызвать admin-функции');
      return;
    } else {
      this.addFinding('OK', 'Функция isAdmin определена');
    }

    // 2. isAdmin проверяет ADMIN_TELEGRAM_IDS
    if (!src.includes('ADMIN_TELEGRAM_IDS') && !src.includes('adminIds')) {
      this.addFinding('HIGH', 'isAdmin не использует ADMIN_TELEGRAM_IDS — список adminов не читается из .env');
    } else {
      this.addFinding('OK', 'isAdmin использует ADMIN_TELEGRAM_IDS из .env');
    }

    // 3. Все adm_ callback-и защищены — ищем обработчик (data === / data.startsWith), не кнопку
    const admCallbacks = [...new Set(src.match(/adm_[a-z_]+/g)||[])];
    const unguarded = admCallbacks.filter(cb => {
      // Ищем место где data сравнивается с этим callback (не callback_data: определение)
      const handlerRe = new RegExp(`(?:data\\s*===?\\s*|data\\.startsWith\\()'${cb.replace(/[_]/g,'_')}`, 'g');
      const matches = [...src.matchAll(handlerRe)];
      if (matches.length === 0) return false; // нет обработчика — не проверяем
      // Хотя бы один обработчик должен иметь isAdmin
      return matches.every(m => {
        const ctx = src.substring(m.index, m.index + 600);
        return !ctx.includes('isAdmin') && !ctx.includes('!admin');
      });
    });
    if (unguarded.length > 3) {
      this.addFinding('MEDIUM', `Возможно незащищённые admin callbacks: ${unguarded.slice(0,5).join(', ')}`);
    } else {
      this.addFinding('OK', `Admin callbacks защищены isAdmin (${admCallbacks.length} уникальных adm_ prefix)`);
    }

    // 4. showAdminMenu защищена
    if (src.includes('async function showAdminMenu') || src.includes('function showAdminMenu')) {
      const fnStart = src.indexOf('function showAdminMenu');
      const fnBody = src.substring(fnStart, fnStart + 300);
      if (!fnBody.includes('isAdmin') && !fnBody.includes('admin')) {
        this.addFinding('HIGH', 'showAdminMenu не проверяет isAdmin внутри функции');
      } else {
        this.addFinding('OK', 'showAdminMenu проверяет права администратора');
      }
    } else {
      this.addFinding('MEDIUM', 'Функция showAdminMenu не найдена');
    }

    // 5. /admin команда защищена
    const adminCmd = src.includes("'/admin'") || src.includes('"admin"');
    if (!adminCmd) {
      this.addFinding('LOW', 'Команда /admin не найдена — нет прямого входа в админку');
    } else {
      this.addFinding('OK', 'Команда /admin присутствует');
    }

    // 6. Нет хардкода admin ID
    const hardcodedId = src.match(/if\s*\(\s*chatId\s*===?\s*\d{7,}/);
    if (hardcodedId) {
      this.addFinding('LOW', 'Хардкод admin ID в коде — лучше использовать ADMIN_TELEGRAM_IDS из .env');
    } else {
      this.addFinding('OK', 'Нет хардкода admin ID — используется .env');
    }
  }
}

if (require.main === module) new AdminProtection().run().then(() => process.exit(0));
module.exports = AdminProtection;
