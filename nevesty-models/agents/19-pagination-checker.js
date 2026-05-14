/** 📄 Pagination Checker — Vertebral Column | Пагінація всіх списків */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class PaginationChecker extends Agent {
  constructor() {
    super({ id:'19', name:'Pagination Checker', organ:'Vertebral Column', emoji:'📄',
      focus:'All lists have pagination, correct bounds, navigation' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // Функції що повертають списки і повинні мати пагінацію
    const listFunctions = {
      'showCatalog':      /perPage|LIMIT.*page/,
      'showAdminOrders':  /LIMIT 8|LIMIT.*8|perPage/,
      'showAdminModels':  /perPage|LIMIT.*page/,
      'showMyOrders':     /LIMIT 10/,
      'showAgentFeed':    /LIMIT 10|LIMIT.*10/,
    };

    for (const [fn, re] of Object.entries(listFunctions)) {
      const fnStart = src.indexOf(`async function ${fn}`);
      if (fnStart === -1) { this.addFinding('LOW',`Функція ${fn} не знайдена`); continue; }
      const fnBody  = src.substring(fnStart, fnStart + 1000);
      if (!re.test(fnBody)) {
        this.addFinding('MEDIUM',`${fn}: пагінація або LIMIT відсутні — при великій кількості записів список стане нечитабельним`);
      } else {
        this.addFinding('OK',`${fn}: пагінація/LIMIT є`);
      }
    }

    // Кнопки навігації ◀️ ▶️
    const prevBtns = (src.match(/◀️/g)||[]).length;
    const nextBtns = (src.match(/▶️/g)||[]).length;
    if (prevBtns < 3 || nextBtns < 3) {
      this.addFinding('LOW',`Кнопок пагінації: ◀️×${prevBtns} ▶️×${nextBtns} — перевір всі списки`);
    } else {
      this.addFinding('OK',`Кнопки пагінації: ◀️×${prevBtns} ▶️×${nextBtns}`);
    }

    // page bounds (page >= 0)
    if (!src.includes('page - 1') && !src.includes('page-1')) {
      this.addFinding('LOW','Від\'ємні сторінки не захищені — page може стати від\'ємним');
    } else {
      this.addFinding('OK','Перевірка нижньої межі сторінки є');
    }
  }
}

if (require.main === module) new PaginationChecker().run().then(() => process.exit(0));
module.exports = PaginationChecker;
