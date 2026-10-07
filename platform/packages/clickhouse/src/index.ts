import { createClient } from '@clickhouse/client';

export const clickhouse = createClient({
  host: process.env.CLICKHOUSE_URL || 'http://localhost:8123',
  username: process.env.CLICKHOUSE_USER || 'default',
  password: process.env.CLICKHOUSE_PASSWORD || '',
  database: process.env.CLICKHOUSE_DB || 'tgplatform',
  clickhouse_settings: {
    async_insert: 1,
    wait_for_async_insert: 0,
  },
});

export type PlatformEvent = {
  event_type: string;
  bot_id: string;
  user_id?: string;
  tenant_id: string;
  properties?: Record<string, unknown>;
  timestamp?: Date;
};

export async function insertEvents(events: PlatformEvent[]): Promise<void> {
  if (!events.length) return;
  try {
    await clickhouse.insert({
      table: 'events',
      values: events.map(e => ({
        event_type: e.event_type,
        bot_id: e.bot_id,
        user_id: e.user_id || '',
        tenant_id: e.tenant_id,
        properties: JSON.stringify(e.properties || {}),
        timestamp: (e.timestamp || new Date()).toISOString().replace('T', ' ').replace('Z', ''),
      })),
      format: 'JSONEachRow',
    });
  } catch (err) {
    console.error('[ClickHouse] insert failed:', err);
  }
}
