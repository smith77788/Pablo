import { Injectable, NotFoundException, ConflictException } from '@nestjs/common';
import { prisma } from '@platform/db';
import axios from 'axios';
import * as https from 'https';

const tgHttp = axios.create({
  baseURL: 'https://api.telegram.org',
  httpsAgent: new https.Agent({ rejectUnauthorized: false }),
  timeout: 15_000,
});

@Injectable()
export class BotsService {
  async addBot(tenantId: string, token: string) {
    const resp = await tgHttp.post(`/bot${token}/getMe`).catch(() => null);
    if (!resp?.data?.ok) throw new ConflictException('Invalid bot token or bot unreachable');

    const info = resp.data.result;
    return prisma.bot.create({
      data: {
        tenantId,
        token,
        telegramId: BigInt(info.id),
        username: info.username,
        firstName: info.first_name,
      },
    });
  }

  async listBots(tenantId: string) {
    return prisma.bot.findMany({
      where: { tenantId, isActive: true },
      orderBy: { createdAt: 'desc' },
      select: {
        id: true, username: true, firstName: true, telegramId: true,
        isActive: true, webhookSet: true, createdAt: true,
        _count: { select: { conversations: true } },
      },
    });
  }

  async getBot(tenantId: string, botId: string) {
    const bot = await prisma.bot.findFirst({ where: { id: botId, tenantId } });
    if (!bot) throw new NotFoundException('Bot not found');
    return bot;
  }

  async deleteBot(tenantId: string, botId: string) {
    const bot = await this.getBot(tenantId, botId);
    await prisma.bot.update({ where: { id: bot.id }, data: { isActive: false } });
    return { ok: true };
  }

  async setWebhook(tenantId: string, botId: string, webhookUrl: string) {
    const bot = await this.getBot(tenantId, botId);
    const secret = process.env.TELEGRAM_WEBHOOK_SECRET ?? '';
    const url = `${webhookUrl}/webhook/${bot.token}`;
    await tgHttp.post(`/bot${bot.token}/setWebhook`, {
      url,
      secret_token: secret,
      allowed_updates: ['message', 'edited_message', 'callback_query', 'my_chat_member'],
    });
    await prisma.bot.update({ where: { id: bot.id }, data: { webhookUrl: url, webhookSet: true } });
    return { ok: true, url };
  }
}
