import { Injectable, BadRequestException } from '@nestjs/common';
import { prisma } from '@platform/db';
import axios from 'axios';
import { ValidateTokensDto } from './dto/validate-tokens.dto';
import { ImportBotsDto } from './dto/import-bots.dto';
import { CloneSettingsDto } from './dto/clone-settings.dto';

@Injectable()
export class BotFactoryService {
  async validateTokens(dto: ValidateTokensDto) {
    const results = await Promise.allSettled(
      dto.tokens.map(async (token) => {
        try {
          const resp = await axios.get(
            `https://api.telegram.org/bot${token}/getMe`,
            { timeout: 5000 },
          );
          return { token, valid: true, bot: resp.data.result };
        } catch (e: any) {
          return {
            token,
            valid: false,
            error: e.response?.data?.description || 'Invalid token',
          };
        }
      }),
    );

    const valid: any[] = [];
    const invalid: any[] = [];
    for (const r of results) {
      if (r.status === 'fulfilled') {
        if (r.value.valid) valid.push(r.value);
        else invalid.push(r.value);
      } else {
        invalid.push({ token: 'unknown', valid: false, error: r.reason });
      }
    }
    return { valid, invalid, total: dto.tokens.length };
  }

  async importBots(dto: ImportBotsDto, tenantId: string, operatorId: string) {
    const validation = await this.validateTokens({ tokens: dto.tokens });
    const imported: any[] = [];
    for (const v of validation.valid) {
      try {
        const bot = await prisma.bot.create({
          data: {
            tenantId,
            token: v.token, // In prod: encrypt before storing
            telegramId: BigInt(v.bot.id),
            firstName: v.bot.first_name,
            username: v.bot.username,
          },
        });
        imported.push({ botId: bot.id, username: v.bot.username });
      } catch {
        // Skip duplicates
      }
    }
    return {
      imported: imported.length,
      skipped: validation.valid.length - imported.length,
      invalid: validation.invalid.length,
      bots: imported,
    };
  }

  async cloneSettings(dto: CloneSettingsDto, tenantId: string) {
    const source = await prisma.bot.findFirst({
      where: { id: dto.sourceBotId, tenantId },
    });
    if (!source) throw new BadRequestException('Source bot not found');

    const fields = dto.fields ?? ['name', 'description', 'short_description'];
    const results: { botId: string; status: string }[] = [];

    for (const targetId of dto.targetBotIds) {
      const updateData: Record<string, any> = {};
      if (fields.includes('name') && source.firstName)
        updateData.firstName = source.firstName;
      if (fields.includes('description') && source.description)
        updateData.description = source.description;

      try {
        await prisma.bot.update({
          where: { id: targetId, tenantId },
          data: updateData,
        });
        results.push({ botId: targetId, status: 'ok' });
      } catch {
        results.push({ botId: targetId, status: 'error' });
      }
    }

    return { cloned: results.filter((r) => r.status === 'ok').length, results };
  }

  async getBotStats(tenantId: string) {
    const [total, bots] = await Promise.all([
      prisma.bot.count({ where: { tenantId } }),
      prisma.bot.findMany({
        where: { tenantId },
        select: {
          id: true,
          firstName: true,
          username: true,
          _count: { select: { conversations: true } },
        },
      }),
    ]);

    const totalConversations = bots.reduce(
      (sum, b) => sum + b._count.conversations,
      0,
    );
    return { totalBots: total, totalConversations, bots };
  }
}
