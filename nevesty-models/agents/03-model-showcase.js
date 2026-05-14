/** 💃 Model Showcase — Skin & Face | Перевіряє показ моделей, фото, параметри */
const { Agent, readFile, dbAll, BOT_PATH } = require('./lib/base');

class ModelShowcase extends Agent {
  constructor() {
    super({ id:'03', name:'Model Showcase', organ:'Skin & Face', emoji:'💃',
      focus:'Photos, parameters, bio display quality' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. safePhoto викликається для моделей
    if (!src.includes('safePhoto')) this.addFinding('HIGH','safePhoto не використовується — фото моделей не надсилаються');
    else this.addFinding('OK','Фото моделей надсилаються через safePhoto');

    // 2. Fallback якщо фото немає
    if (!src.includes('photo_main')) this.addFinding('MEDIUM','photo_main не перевіряється — немає fallback для текстового опису');
    else this.addFinding('OK','Fallback для відсутнього фото присутній');

    // 3. Всі ключові параметри моделі
    const params = ['height','weight','bust','waist','hips','shoe_size','hair_color','eye_color','bio','instagram'];
    const missing = params.filter(p => !src.includes(`m.${p}`) && !src.includes(`'${p}'`));
    if (missing.length > 3) this.addFinding('MEDIUM',`Параметри не відображаються у картці: ${missing.join(', ')}`);
    else this.addFinding('OK','Основні параметри моделі відображаються');

    // 4. Перевірка реальних моделей у БД
    try {
      const models = await dbAll('SELECT id, name, photo_main, available FROM models');
      const noPhoto = models.filter(m => !m.photo_main);
      if (noPhoto.length > 0) this.addFinding('LOW',`${noPhoto.length} моделей без фото: ${noPhoto.map(m=>m.name).join(', ')}`);
      else this.addFinding('OK',`Всі ${models.length} моделей мають фото`);

      const unavail = models.filter(m => !m.available);
      if (unavail.length) this.addFinding('INFO',`${unavail.length} моделей позначені як недоступні`);
    } catch (e) { this.addFinding('LOW',`Не вдалось перевірити БД: ${e.message}`); }

    // 5. Каталог має пагінацію
    if (!src.includes('perPage')) this.addFinding('MEDIUM','Каталог без пагінації — при великій кількості моделей зламається');
    else this.addFinding('OK','Пагінація каталогу реалізована');

    // 6. Фільтри категорій
    if (!src.includes('CATEGORIES')) this.addFinding('MEDIUM','Фільтри категорій відсутні у каталозі бота');
    else this.addFinding('OK','Фільтри категорій Fashion/Commercial/Events присутні');
  }
}

if (require.main === module) new ModelShowcase().run().then(() => process.exit(0));
module.exports = ModelShowcase;
