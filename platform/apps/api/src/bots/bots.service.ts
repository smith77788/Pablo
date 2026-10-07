import { Injectable, NotFoundException, ConflictException } from '@nestjs/common';
import { prisma } from '@platform/db';
import { CreateBotDto } from './dto/create-bot.dto';
import { UpdateBotDto } from './dto/update-bot.dto';
import axios from 'axios';
import * as https from 'https';

const tgHttp = axios.create({
  baseURL: 'https://api.telegram.org',
  httpsAgent: new https.Agent({ rejectUnauthorized: false }),
  timeout: 15_000,
});

@Injectable()
export class BotsService {
  // ─── CRUD ─────────────────────────────────────────────────────────────────

  async findAll(tenantId: string) {
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

  async findOne(tenantId: string, id: string) {
    const bot = await prisma.bot.findFirst({ where: { id, tenantId } });
    if (!bot) throw new NotFoundException('Bot not found');
    return bot;
  }

  async create(tenantId: string, dto: CreateBotDto) {
    const resp = await tgHttp.post(`/bot${dto.token}/getMe`).catch(() => null);
    if (!resp?.data?.ok) throw new ConflictException('Invalid bot token or bot unreachable');

    const info = resp.data.result;
    return prisma.bot.create({
      data: {
        tenantId,
        token: dto.token,
        telegramId: BigInt(info.id),
        username: info.username,
        firstName: dto.name || info.first_name,
      },
    });
  }

  async update(tenantId: string, id: string, dto: UpdateBotDto) {
    await this.findOne(tenantId, id);
    return prisma.bot.update({
      where: { id },
      data: { ...(dto.name !== undefined && { firstName: dto.name }) },
    });
  }

  async delete(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    await prisma.bot.update({ where: { id }, data: { isActive: false } });
    return { ok: true };
  }

  // ─── STATS ────────────────────────────────────────────────────────────────

  async getStats(tenantId: string, botId: string) {
    await this.findOne(tenantId, botId);

    const [userCount, messageCount] = await Promise.all([
      prisma.telegramUser.count({ where: { tenantId } }),
      prisma.message.count({
        where: {
          tenantId,
          conversation: { botId },
        },
      }),
    ]);

    return { userCount, messageCount };
  }

  // ─── LEGACY ALIASES (kept for backward compatibility) ─────────────────────

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
    return this.findAll(tenantId);
  }

  async getBot(tenantId: string, botId: string) {
    return this.findOne(tenantId, botId);
  }

  async deleteBot(tenantId: string, botId: string) {
    return this.delete(tenantId, botId);
  }

  async setWebhook(tenantId: string, botId: string, webhookUrl: string) {
    const bot = await this.findOne(tenantId, botId);
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
