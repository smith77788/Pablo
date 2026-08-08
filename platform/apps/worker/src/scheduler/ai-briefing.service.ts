import { Injectable, Logger } from '@nestjs/common';
import { prisma } from '@platform/db';
import Anthropic from '@anthropic-ai/sdk';
import axios from 'axios';
import * as https from 'https';

const tgHttp = axios.create({
  httpsAgent: new https.Agent({ rejectUnauthorized: false }),
  timeout: 15_000,
});

@Injectable()
export class AiBriefingService {
  private readonly logger = new Logger(AiBriefingService.name);
  private client: Anthropic | null = null;

  private getClient(): Anthropic | null {
    if (!process.env.ANTHROPIC_API_KEY) return null;
    if (!this.client) this.client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
    return this.client;
  }

  async runDailyBriefing(): Promise<void> {
    const ai = this.getClient();
    if (!ai) {
      this.logger.warn('ANTHROPIC_API_KEY not set — AI briefing пропущен');
      return;
    }

    // Тенанты с OWNER-оператором у которого есть telegramChatId + хотя бы один бот
    const tenants = await prisma.tenant.findMany({
      where: { isActive: true },
      select: {
        id: true,
        name: true,
        operators: {
          where: { role: 'OWNER', isActive: true, telegramChatId: { not: null } },
          select: { telegramChatId: true },
          take: 1,
        },
        bots: {
          where: { isActive: true },
          select: { id: true, token: true, username: true, firstName: true },
          take: 10,
        },
      },
    });

    for (const tenant of tenants) {
      const owner = tenant.operators[0];
      if (!owner?.telegramChatId || !tenant.bots.length) continue;

      try {
        await this.runForTenant(ai, tenant.id, tenant.name, owner.telegramChatId, tenant.bots);
      } catch (err) {
        this.logger.error(`AI briefing failed for tenant ${tenant.id}`, err);
      }
    }
  }

  private async runForTenant(
    ai: Anthropic,
    tenantId: string,
    tenantName: string,
    ownerChatId: string,
    bots: { id: string; token: string; username: string | null; firstName: string | null }[],
  ): Promise<void> {
    const now = new Date();
    const since24h = new Date(now.getTime() - 24 * 3600_000);
    const since7d = new Date(now.getTime() - 7 * 24 * 3600_000);

    const metrics = await Promise.all(
      bots.map(async (bot) => {
        const [totalUsers, newUsers24h, openConvs, closedToday, broadcasts7d] = await Promise.all([
          prisma.telegramUser.count({ where: { firstBotId: bot.id, isBlocked: false } }),
          prisma.telegramUser.count({ where: { firstBotId: bot.id, createdAt: { gte: since24h } } }),
          prisma.conversation.count({ where: { botId: bot.id, status: 'OPEN' } }),
          prisma.conversation.count({ where: { botId: bot.id, status: 'CLOSED', updatedAt: { gte: since24h } } }),
          prisma.broadcast.count({ where: { botId: bot.id, status: 'COMPLETED', completedAt: { gte: since7d } } }),
        ]);
        const name = bot.username ? `@${bot.username}` : (bot.firstName ?? bot.id);
        return { name, totalUsers, newUsers24h, openConvs, closedToday, broadcasts7d };
      }),
    );

    const dataBlock = metrics
      .map((m) =>
        `Бот ${m.name}: всего ${m.totalUsers} пользователей, +${m.newUsers24h} за сутки, ${m.openConvs} открытых диалогов, ${m.closedToday} закрыто сегодня, ${m.broadcasts7d} рассылок за неделю`,
      )
      .join('\n');

    const response = await ai.messages.create({
      model: 'claude-sonnet-4-6',
      max_tokens: 800,
      messages: [
        {
          role: 'user',
          content: `Ты AI-аналитик BotMother. Напиши КРАТКИЙ утренний брифинг (5-7 пунктов) для владельца "${tenantName}". Отформатируй для Telegram с эмодзи. В конце — 1 главная рекомендация на сегодня.\n\nДанные за 24 часа:\n${dataBlock}`,
        },
      ],
    });

    const briefingText = response.content
      .filter((b): b is Anthropic.TextBlock => b.type === 'text')
      .map((b) => b.text)
      .join('\n');

    const date = now.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
    const message = `🌅 <b>Утренний брифинг BotMother — ${date}</b>\n\n${briefingText}`;

    // Отправляем через первый бот тенанта
    const senderBot = bots[0];
    await tgHttp.post(`https://api.telegram.org/bot${senderBot.token}/sendMessage`, {
      chat_id: ownerChatId,
      text: message,
      parse_mode: 'HTML',
    });

    this.logger.log(`AI briefing отправлен для tenant ${tenantId}`);
  }
}
