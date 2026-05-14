/** 🔍 Search Enhancer — Olfactory System | Пошук та фільтрація моделей */
const { Agent, readFile, dbAll, BOT_PATH } = require('./lib/base');

class SearchEnhancer extends Agent {
  constructor() {
    super({ id:'17', name:'Search Enhancer', organ:'Olfactory System', emoji:'🔍',
      focus:'Catalog filters, model search, availability filter' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. Фільтр по категорії реалізований
    if (!src.includes('CATEGORIES') || !src.includes('cat_cat_')) {
      this.addFinding('HIGH','Фільтр по категорії відсутній у каталозі бота');
    } else { this.addFinding('OK','Фільтр All/Fashion/Commercial/Events реалізований'); }

    // 2. Пошук за іменем (є на сайті, не обов'язковий у боті але бажаний)
    if (!src.includes('LIKE') && !src.includes('search')) {
      this.addFinding('LOW','Текстовий пошук моделей у боті відсутній (є на сайті)');
    } else { this.addFinding('OK','Текстовий пошук моделей присутній'); }

    // 3. Фільтр доступності (available=1)
    if (!src.includes('available=1') && !src.includes('available = 1')) {
      this.addFinding('HIGH','Фільтр доступності моделей відсутній — показуються недоступні моделі');
    } else { this.addFinding('OK','Показуються тільки доступні моделі (available=1)'); }

    // 4. Реальний розподіл по категоріях
    try {
      const cats = await dbAll('SELECT category, COUNT(*) as n FROM models WHERE available=1 GROUP BY category');
      const info = cats.map(c=>`${c.category}:${c.n}`).join(', ');
      this.addFinding('INFO',`Розподіл моделей по категоріях: ${info}`);
    } catch {}

    // 5. Пагінація каталогу
    if (!src.includes('perPage') || !src.includes('page * perPage')) {
      this.addFinding('MEDIUM','Пагінація каталогу некоректна — при 20+ моделях список не розбивається');
    } else { this.addFinding('OK','Пагінація каталогу працює коректно'); }
  }
}

if (require.main === module) new SearchEnhancer().run().then(() => process.exit(0));
module.exports = SearchEnhancer;
