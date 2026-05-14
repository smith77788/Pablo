/** 🗄️ DB Optimizer — Skeletal System | Індекси, запити, продуктивність БД */
const { Agent, readFile, dbAll, BOT_PATH, DB_MOD } = require('./lib/base');

class DBOptimizer extends Agent {
  constructor() {
    super({ id:'11', name:'DB Optimizer', organ:'Skeletal System', emoji:'🗄️',
      focus:'Query efficiency, indexes, N+1 patterns' });
  }
  async analyze() {
    const botSrc = readFile(BOT_PATH);
    const dbSrc  = readFile(DB_MOD);

    // 1. Наявність індексів у БД
    const indexes = ['idx_orders_status','idx_orders_model_id','idx_orders_client_chat','idx_messages_order','idx_models_available'];
    const missing = indexes.filter(i => !dbSrc.includes(i));
    if (missing.length) this.addFinding('HIGH',`Відсутні індекси: ${missing.join(', ')}`);
    else this.addFinding('OK',`Всі ${indexes.length} необхідних індексів присутні`);

    // 2. SELECT * не використовується (вибираємо тільки потрібне)
    const selectStar = (botSrc.match(/SELECT \*/g)||[]).length;
    if (selectStar > 3) this.addFinding('MEDIUM',`SELECT * використовується ${selectStar} разів — краще вибирати конкретні поля`);
    else this.addFinding('OK',`SELECT * мінімально використовується (${selectStar} разів)`);

    // 3. LIMIT на важких запитах
    const queryWithoutLimit = (botSrc.match(/FROM orders(?![\s\S]{0,200}LIMIT)/g)||[]).length;
    if (queryWithoutLimit > 2) this.addFinding('MEDIUM',`${queryWithoutLimit} запитів до orders без LIMIT — при великій БД буде повільно`);
    else this.addFinding('OK','Запити мають LIMIT обмеження');

    // 4. Реальний розмір БД
    try {
      const [orders, models, msgs, logs] = await Promise.all([
        dbAll('SELECT COUNT(*) as n FROM orders'),
        dbAll('SELECT COUNT(*) as n FROM models'),
        dbAll('SELECT COUNT(*) as n FROM messages'),
        dbAll('SELECT COUNT(*) as n FROM agent_logs'),
      ]);
      this.addFinding('INFO',
        `БД: ${orders[0].n} заявок, ${models[0].n} моделей, ${msgs[0].n} повідомлень, ${logs[0].n} логів агентів`
      );
    } catch (e) { this.addFinding('LOW',`Не вдалось перевірити розмір БД: ${e.message}`); }

    // 5. Promise.all для паралельних запитів
    const parallelQueries = (botSrc.match(/Promise\.all\(/g)||[]).length;
    if (parallelQueries < 2) this.addFinding('LOW',`Promise.all використовується лише ${parallelQueries} разів — більше запитів можна паралелізувати`);
    else this.addFinding('OK',`${parallelQueries} паралельних запитів через Promise.all`);
  }
}

if (require.main === module) new DBOptimizer().run().then(() => process.exit(0));
module.exports = DBOptimizer;
