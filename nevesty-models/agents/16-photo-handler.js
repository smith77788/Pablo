/** 📸 Photo Handler — Visual Cortex | Надсилання фото, галерея, fallback */
const { Agent, readFile, dbAll, BOT_PATH } = require('./lib/base');

class PhotoHandler extends Agent {
  constructor() {
    super({ id:'16', name:'Photo Handler', organ:'Visual Cortex', emoji:'📸',
      focus:'Photo sending, gallery support, text fallback' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. safePhoto з fallback на text
    if (!src.includes('safePhoto')) { this.addFinding('HIGH','safePhoto відсутня — помилки при надсиланні фото будуть crashити бота'); }
    else { this.addFinding('OK','safePhoto з fallback реалізована'); }

    // 2. photo_main перевіряється перед надсиланням
    const photoCheck = src.includes('photo_main') && (src.includes('if (m.photo_main)') || src.includes('m.photo_main ?'));
    if (!photoCheck) this.addFinding('MEDIUM','photo_main не перевіряється перед sendPhoto — помилка якщо фото немає');
    else this.addFinding('OK','Наявність photo_main перевіряється');

    // 3. caption для фото з параметрами
    if (!src.includes('caption')) this.addFinding('MEDIUM','Підпис (caption) не використовується при надсиланні фото — інформація про модель не буде показана');
    else this.addFinding('OK','caption використовується для опису фото моделі');

    // 4. Реальні моделі без фото
    try {
      const noPhoto = await dbAll("SELECT name FROM models WHERE (photo_main IS NULL OR photo_main='') AND available=1");
      if (noPhoto.length > 0) {
        this.addFinding('LOW',`Доступні моделі без фото (${noPhoto.length}): ${noPhoto.map(m=>m.name).join(', ')}`);
      } else {
        this.addFinding('OK','Всі доступні моделі мають фото');
      }
    } catch {}

    // 5. Фото в каталозі (sendPhoto per model card)
    const catalogPhotoSupport = src.includes('showModel') && src.includes('safePhoto');
    if (!catalogPhotoSupport) this.addFinding('MEDIUM','Фото не показуються в картці моделі');
    else this.addFinding('OK','Фото показуються при перегляді картки моделі');
  }
}

if (require.main === module) new PhotoHandler().run().then(() => process.exit(0));
module.exports = PhotoHandler;
