/** 🔗 Deeplink Handler — Corpus Callosum | Deep links, start params, Mini App */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class DeeplinkHandler extends Agent {
  constructor() {
    super({ id:'23', name:'Deeplink Handler', organ:'Corpus Callosum', emoji:'🔗',
      focus:'Deep link parsing, start parameters, Telegram Mini App integration' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. /start обрабатывается (onText regex или command handler)
    const startHandler = src.includes("'/start'") || src.includes('"start"') ||
      src.includes('/\\/start/') || src.includes("onText(/\\/start") || src.includes('onText(/\/start');
    if (!startHandler) {
      this.addFinding('HIGH', 'Команда /start не найдена');
      return;
    }
    this.addFinding('OK', 'Команда /start зарегистрирована');

    // 2. msg.text.split(' ')[1] или startParam
    const hasStartParam = src.includes("split(' ')[1]") || src.includes('startParam') || src.includes('start_param');
    if (!hasStartParam) {
      this.addFinding('MEDIUM', 'Deep link параметр (/start model_123) не парсится — ссылки на конкретные модели не работают');
    } else {
      this.addFinding('OK', 'Deep link параметр /start <param> обрабатывается');
    }

    // 3. Mini App кнопка есть
    const hasMiniApp = src.includes('web_app') || src.includes('WebApp') || src.includes('webapp');
    if (!hasMiniApp) {
      this.addFinding('LOW', 'Telegram Mini App кнопка не настроена — сайт не открывается внутри Telegram');
    } else {
      this.addFinding('OK', 'Mini App / web_app кнопка настроена');
    }

    // 4. SITE_URL используется для Mini App
    if (!src.includes('SITE_URL')) {
      this.addFinding('MEDIUM', 'SITE_URL не используется — Mini App URL хардкодится или отсутствует');
    } else {
      this.addFinding('OK', 'SITE_URL из .env используется для Mini App и ссылок');
    }

    // 5. Кнопка каталога в Mini App
    const catalogDeeplink = src.includes('/catalog') || src.includes('catalog.html');
    if (!catalogDeeplink) {
      this.addFinding('LOW', 'Ссылка на каталог в Mini App отсутствует');
    } else {
      this.addFinding('OK', 'Ссылка на каталог в Mini App присутствует');
    }

    // 6. Bot username для ссылок
    if (!src.includes('BOT_USERNAME') && !src.includes('BOT_NAME')) {
      this.addFinding('LOW', 'BOT_USERNAME не используется — ссылки t.me/<bot>?start= не формируются');
    } else {
      this.addFinding('OK', 'BOT_USERNAME используется для формирования deep links');
    }

    // 7. bk_start deeplink для бронирования
    if (!src.includes('bk_start')) {
      this.addFinding('MEDIUM', 'Deep link на бронирование (bk_start) отсутствует');
    } else {
      this.addFinding('OK', 'Deep link на начало бронирования (bk_start) реализован');
    }
  }
}

if (require.main === module) new DeeplinkHandler().run().then(() => process.exit(0));
module.exports = DeeplinkHandler;
