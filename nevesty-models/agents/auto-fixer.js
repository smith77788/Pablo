/** 🔧 Auto Fixer — Bone Marrow | Автоматически исправляет найденные проблемы */
const { Agent, dbRun, dbAll, dbGet, logAgent, tgSend } = require('./lib/base');
const path = require('path');
const fs   = require('fs');

class AutoFixer extends Agent {
  constructor() {
    super({ id:'AF', name:'Auto Fixer', organ:'Bone Marrow', emoji:'🔧',
      focus:'Auto-fix stale sessions, missing indexes, DB integrity, known patterns' });
  }

  async analyze() {
    let fixCount = 0;

    // ── Fix 1: Stale sessions (>1 час) ──────────────────────────────────────
    try {
      const stale = await dbAll(
        "SELECT chat_id FROM telegram_sessions WHERE state != 'idle' AND updated_at < datetime('now', '-1 hour')"
      );
      if (stale.length > 0) {
        await dbRun(
          "UPDATE telegram_sessions SET state='idle', data='{}', updated_at=CURRENT_TIMESTAMP WHERE state != 'idle' AND updated_at < datetime('now', '-1 hour')"
        );
        this.addFixed(`Очищено ${stale.length} зависших сессий`);
        fixCount += stale.length;
      } else {
        this.addFinding('OK', 'Зависших сессий нет');
      }
    } catch (e) { this.addFinding('LOW', `Сессии: ошибка — ${e.message}`); }

    // ── Fix 2: Missing DB indexes ──────────────────────────────────────────
    const indexes = [
      { name: 'idx_orders_status',      sql: 'CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)' },
      { name: 'idx_orders_chat_id',     sql: 'CREATE INDEX IF NOT EXISTS idx_orders_chat_id ON orders(client_chat_id)' },
      { name: 'idx_orders_number',      sql: 'CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(order_number)' },
      { name: 'idx_sessions_chat_id',   sql: 'CREATE INDEX IF NOT EXISTS idx_sessions_chat_id ON telegram_sessions(chat_id)' },
      { name: 'idx_agent_logs_created', sql: 'CREATE INDEX IF NOT EXISTS idx_agent_logs_created ON agent_logs(created_at)' },
      { name: 'idx_messages_order_id',  sql: 'CREATE INDEX IF NOT EXISTS idx_messages_order_id ON messages(order_id)' },
    ];
    for (const idx of indexes) {
      try {
        await dbRun(idx.sql);
        this.addFixed(`Индекс ${idx.name} создан/проверен`);
        fixCount++;
      } catch (e) { this.addFinding('LOW', `Индекс ${idx.name}: ${e.message}`); }
    }

    // ── Fix 3: Cleanup old agent logs (>7 дней) ────────────────────────────
    try {
      const old = await dbGet("SELECT COUNT(*) as n FROM agent_logs WHERE created_at < datetime('now', '-7 days')");
      if (old && old.n > 0) {
        await dbRun("DELETE FROM agent_logs WHERE created_at < datetime('now', '-7 days')");
        this.addFixed(`Удалено ${old.n} устаревших логов агентов (>7 дней)`);
        fixCount++;
      } else {
        this.addFinding('OK', 'Старых логов агентов нет');
      }
    } catch (e) { this.addFinding('LOW', `Очистка логов: ${e.message}`); }

    // ── Fix 4: Orders без order_number ─────────────────────────────────────
    try {
      const noNum = await dbAll("SELECT id FROM orders WHERE order_number IS NULL OR order_number=''");
      for (const row of noNum) {
        const num = 'NM-' + Date.now() + '-' + row.id;
        await dbRun('UPDATE orders SET order_number=? WHERE id=?', [num, row.id]);
        fixCount++;
      }
      if (noNum.length > 0) this.addFixed(`Присвоены номера ${noNum.length} заказам без номера`);
      else this.addFinding('OK', 'Все заказы имеют номер');
    } catch (e) { this.addFinding('LOW', `order_number fix: ${e.message}`); }

    // ── Fix 5: Orphaned messages (без order) ───────────────────────────────
    try {
      const orphan = await dbGet(
        "SELECT COUNT(*) as n FROM messages WHERE order_id NOT IN (SELECT id FROM orders)"
      );
      if (orphan && orphan.n > 0) {
        await dbRun("DELETE FROM messages WHERE order_id NOT IN (SELECT id FROM orders)");
        this.addFixed(`Удалено ${orphan.n} сообщений без заказа`);
        fixCount++;
      } else {
        this.addFinding('OK', 'Осиротевших сообщений нет');
      }
    } catch {}

    // ── Fix 6: Uploads dir ─────────────────────────────────────────────────
    const uploadsDir = path.join(__dirname, '../uploads');
    if (!fs.existsSync(uploadsDir)) {
      fs.mkdirSync(uploadsDir, { recursive: true });
      this.addFixed('Создана папка uploads/');
      fixCount++;
    } else {
      this.addFinding('OK', 'Папка uploads/ существует');
    }

    if (fixCount === 0) {
      this.addFinding('OK', 'Нечего исправлять — система в норме');
    }
  }
}

if (require.main === module) new AutoFixer().run().then(() => process.exit(0));
module.exports = AutoFixer;
