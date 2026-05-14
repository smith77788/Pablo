/** 🔄 Order Lifecycle — Circulatory System | Перевіряє повний цикл замовлення */
const { Agent, readFile, dbAll, BOT_PATH } = require('./lib/base');

class OrderLifecycle extends Agent {
  constructor() {
    super({ id:'04', name:'Order Lifecycle', organ:'Circulatory System', emoji:'🔄',
      focus:'All 6 order statuses + transitions + notifications' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. Всі 6 статусів
    const statuses = ['new','reviewing','confirmed','in_progress','completed','cancelled'];
    const missing = statuses.filter(s => !src.includes(`'${s}'`));
    if (missing.length) this.addFinding('HIGH',`Статуси відсутні у боті: ${missing.join(', ')}`);
    else this.addFinding('OK','Всі 6 статусів оброблені');

    // 2. Кнопки дій для кожного статусу (адмін)
    const actions = ['adm_confirm_','adm_review_','adm_reject_','adm_complete_'];
    const missingActions = actions.filter(a => !src.includes(a));
    if (missingActions.length) this.addFinding('HIGH',`Відсутні дії: ${missingActions.join(', ')}`);
    else this.addFinding('OK','Всі адмін-дії (confirm/review/reject/complete) присутні');

    // 3. notifyStatusChange викликається при зміні статусу
    const notifyCalls = (src.match(/notifyStatusChange/g)||[]).length;
    if (notifyCalls < 2) this.addFinding('HIGH',`notifyStatusChange викликається лише ${notifyCalls} разів — клієнт не отримає сповіщень`);
    else this.addFinding('OK',`notifyStatusChange викликається ${notifyCalls} разів`);

    // 4. Race condition захист (WHERE status NOT IN)
    if (!src.includes('status NOT IN')) this.addFinding('HIGH','Відсутній захист від race conditions при зміні статусу — подвійне підтвердження можливе');
    else this.addFinding('OK','Захист від race conditions присутній');

    // 5. Реальні дані з БД
    try {
      const orders = await dbAll("SELECT status, COUNT(*) as n FROM orders GROUP BY status");
      const total  = orders.reduce((s,r) => s+r.n, 0);
      this.addFinding('INFO',`БД: ${total} замовлень — ${orders.map(r=>`${r.status}:${r.n}`).join(', ')}`);
    } catch {}

    // 6. generateOrderNumber використовується
    if (!src.includes('generateOrderNumber')) this.addFinding('CRITICAL','generateOrderNumber не викликається — замовлення будуть без номера!');
    else this.addFinding('OK','Унікальні номери замовлень генеруються');
  }
}

if (require.main === module) new OrderLifecycle().run().then(() => process.exit(0));
module.exports = OrderLifecycle;
