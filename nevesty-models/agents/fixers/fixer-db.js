/**
 * 🔧 DB Fixer — patches database schema automatically
 * Ensures critical indexes exist, verifies FK references, checks for orphaned records
 */
const { Agent, dbRun, dbAll, dbGet } = require('../lib/base');

class DBFixer extends Agent {
  constructor() {
    super({ id: 'DF', name: 'DB Fixer', emoji: '🔧', organ: 'Auto-Surgeon', focus: 'Ensures DB indexes and schema integrity' });
  }

  async analyze() {
    await this.ensureIndexes();
    await this.checkOrphans();
  }

  // ─── Ensure critical indexes ───────────────────────────────────────────────

  async ensureIndexes() {
    const requiredIndexes = [
      {
        name: 'idx_orders_status',
        sql: 'CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)',
        table: 'orders',
        column: 'status',
      },
      {
        name: 'idx_models_available',
        sql: 'CREATE INDEX IF NOT EXISTS idx_models_available ON models(available)',
        table: 'models',
        column: 'available',
      },
      {
        name: 'idx_orders_client_chat_id',
        sql: 'CREATE INDEX IF NOT EXISTS idx_orders_client_chat_id ON orders(client_chat_id)',
        table: 'orders',
        column: 'client_chat_id',
      },
    ];

    for (const idx of requiredIndexes) {
      try {
        // Check if index exists in sqlite_master
        const existing = await dbGet(
          "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
          [idx.name]
        );

        if (!existing) {
          // Verify the table + column exist before creating index
          const tableInfo = await dbAll(`PRAGMA table_info(${idx.table})`).catch(() => []);
          const colExists = tableInfo.some(row => row.name === idx.column);

          if (colExists) {
            await dbRun(idx.sql);
            this.addFixed(`Создан индекс ${idx.name} на ${idx.table}(${idx.column})`);
            this.addFinding('OK', `Индекс ${idx.name} — создан`);
          } else {
            this.addFinding('LOW', `Индекс ${idx.name} не создан — колонка ${idx.column} не найдена в ${idx.table}`);
          }
        } else {
          this.addFinding('OK', `Индекс ${idx.name} — существует`);
        }
      } catch (e) {
        this.addFinding('HIGH', `Ошибка при проверке/создании ${idx.name}: ${e.message}`);
      }
    }
  }

  // ─── Verify FK references and orphaned records ─────────────────────────────

  async checkOrphans() {
    // Check orphaned orders (orders referencing non-existent models)
    try {
      const orphanedOrders = await dbAll(
        `SELECT COUNT(*) as cnt FROM orders o
         WHERE o.model_id IS NOT NULL
           AND o.model_id != ''
           AND NOT EXISTS (SELECT 1 FROM models m WHERE m.id = o.model_id)`
      );
      const cnt = orphanedOrders[0]?.cnt || 0;
      if (cnt > 0) {
        this.addFinding('HIGH', `${cnt} заказов ссылаются на несуществующих моделей (orphaned FK)`);
      } else {
        this.addFinding('OK', 'Orphaned orders — не обнаружено');
      }
    } catch (e) {
      // Table may not have model_id column or tables don't exist yet — skip gracefully
      this.addFinding('INFO', `FK проверка orders→models пропущена: ${e.message}`);
    }

    // Check orphaned messages (messages referencing non-existent orders)
    try {
      const orphanedMsgs = await dbAll(
        `SELECT COUNT(*) as cnt FROM messages msg
         WHERE msg.order_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.id = msg.order_id)`
      );
      const cnt = orphanedMsgs[0]?.cnt || 0;
      if (cnt > 0) {
        this.addFinding('MEDIUM', `${cnt} сообщений ссылаются на несуществующие заказы`);
      } else {
        this.addFinding('OK', 'Orphaned messages — не обнаружено');
      }
    } catch (e) {
      this.addFinding('INFO', `FK проверка messages→orders пропущена: ${e.message}`);
    }

    // Check telegram_sessions for stale entries (older than 7 days)
    try {
      const stale = await dbGet(
        `SELECT COUNT(*) as cnt FROM telegram_sessions
         WHERE updated_at < datetime('now', '-7 days')`
      );
      const cnt = stale?.cnt || 0;
      if (cnt > 5) {
        this.addFinding('LOW', `${cnt} устаревших telegram_sessions (>7 дней) — можно очистить`);
      } else {
        this.addFinding('OK', `Stale sessions — ${cnt} (норма)`);
      }
    } catch (e) {
      this.addFinding('INFO', `Проверка stale sessions пропущена: ${e.message}`);
    }
  }
}

if (require.main === module) {
  const f = new DBFixer();
  f.run().then(r => {
    console.log(`[DBFixer] findings: ${r.findings.length}, fixed: ${r.fixed.length}, elapsed: ${r.elapsed}s`);
    r.findings.forEach(f => console.log(`  ${f.sev} ${f.msg}`));
    r.fixed.forEach(m => console.log(`  🔧 ${m}`));
    process.exit(0);
  }).catch(e => { console.error(e); process.exit(1); });
}
module.exports = DBFixer;
