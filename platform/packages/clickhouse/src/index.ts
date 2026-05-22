import { createClient } from '@clickhouse/client';

export const clickhouse = createClient({
  url: process.env.CLICKHOUSE_URL ?? 'http://localhost:8123',
  username: process.env.CLICKHOUSE_USER ?? 'default',
  password: process.env.CLICKHOUSE_PASSWORD ?? '',
  database: process.env.CLICKHOUSE_DATABASE ?? 'tgplatform',
  clickhouse_settings: {
    async_insert: 1,
    wait_for_async_insert: 0,
  },
});

export interface EventRow {
  tenant_id: string;
  bot_id: string;
  user_id: string;
  telegram_id: bigint;
  event_type: string;
  session_id: string;
  conversation_id?: string;
  properties?: Record<string, unknown>;
  timestamp: Date;
}

export async function insertEvents(events: EventRow[]): Promise<void> {
  await clickhouse.insert({
    table: 'events',
    values: events.map((e) => ({
      ...e,
      telegram_id: e.telegram_id.toString(),
      properties: JSON.stringify(e.properties ?? {}),
      timestamp: e.timestamp.toISOString(),
    })),
    format: 'JSONEachRow',
  });
}
