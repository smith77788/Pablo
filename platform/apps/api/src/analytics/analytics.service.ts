import { Injectable } from '@nestjs/common';
import { prisma } from '@platform/db';
import { clickhouse } from '@platform/clickhouse';

@Injectable()
export class AnalyticsService {
  async getDashboard(tenantId: string, botId?: string) {
    const where: any = { tenantId };
    if (botId) where.botId = botId;

    const [totalUsers, activeConversations, totalMessages, openConversations] = await prisma.$transaction([
      prisma.telegramUser.count({ where: { tenantId } }),
      prisma.conversation.count({ where: { ...where, status: { in: ['OPEN', 'PENDING'] } } }),
      prisma.message.count({ where: { ...where, direction: 'INBOUND' } }),
      prisma.conversation.count({ where: { ...where, status: 'OPEN' } }),
    ]);

    // ClickHouse: last 7 days message volume
    let dailyMessages: any[] = [];
    try {
      const result = await clickhouse.query({
        query: `
          SELECT toDate(timestamp) as date, count() as count
          FROM events
          WHERE tenant_id = {tenantId:String}
            AND event_type = 'message_received'
            AND timestamp >= now() - INTERVAL 7 DAY
          GROUP BY date ORDER BY date
        `,
        query_params: { tenantId },
        format: 'JSONEachRow',
      });
      dailyMessages = await result.json();
    } catch { /* ClickHouse may not be available in dev */ }

    return { totalUsers, activeConversations, openConversations, totalMessages, dailyMessages };
  }

  async getBotMetrics(tenantId: string, botId: string, days = 30) {
    let rows: any[] = [];
    try {
      const result = await clickhouse.query({
        query: `
          SELECT
            toDate(timestamp) as date,
            countIf(event_type = 'message_received') as messages_in,
            countIf(event_type = 'message_sent') as messages_out,
            uniqIf(user_id, event_type = 'message_received') as active_users
          FROM events
          WHERE tenant_id = {tenantId:String}
            AND bot_id = {botId:String}
            AND timestamp >= now() - INTERVAL {days:UInt8} DAY
          GROUP BY date ORDER BY date
        `,
        query_params: { tenantId, botId, days },
        format: 'JSONEachRow',
      });
      rows = await result.json();
    } catch { }
    return rows;
  }
}
