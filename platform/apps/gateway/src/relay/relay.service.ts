import { Injectable, Logger, OnModuleInit, OnModuleDestroy } from '@nestjs/common';
import { InjectQueue } from '@nestjs/bull';
import { Queue } from 'bull';
import { prisma } from '@platform/db';
import { TelegramUpdate, TelegramMessage } from '@platform/types';
import { randomUUID } from 'crypto';
import Redis from 'ioredis';

const INBOX_CHANNEL = 'botmother:inbox';

@Injectable()
export class RelayService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(RelayService.name);
  private readonly redis: Redis;

  constructor(
    @InjectQueue('outbound') private readonly outboundQueue: Queue,
    @InjectQueue('automation') private readonly automationQueue: Queue,
  ) {
    this.redis = new Redis(process.env.REDIS_URL ?? 'redis://localhost:6379', { lazyConnect: true });
  }

  async onModuleInit() {
    await this.redis.connect().catch(() => {});
  }

  async onModuleDestroy() {
    await this.redis.quit();
  }

  async processUpdate(botToken: string, update: TelegramUpdate): Promise<void> {
    const bot = await prisma.bot.findUnique({ where: { token: botToken } });
    if (!bot || !bot.isActive) return;

    const msg = update.message ?? update.edited_message;
    if (msg) {
      await this.handleMessage(bot, msg, !!update.edited_message);
      return;
    }

    if (update.my_chat_member) {
      await this.handleChatMember(bot, update);
    }
  }

  private async handleMessage(
    bot: { id: string; tenantId: string; telegramId: bigint },
    msg: TelegramMessage,
    isEdit: boolean,
  ): Promise<void> {
    if (!msg.from || msg.from.is_bot) return;
    if (msg.chat.type !== 'private') return;

    const telegramUserId = BigInt(msg.from.id);

    const user = await prisma.telegramUser.upsert({
      where: { tenantId_telegramId: { tenantId: bot.tenantId, telegramId: telegramUserId } },
      create: {
        tenantId: bot.tenantId,
        telegramId: telegramUserId,
        username: msg.from.username,
        firstName: msg.from.first_name,
        lastName: msg.from.last_name,
        languageCode: msg.from.language_code,
        isPremium: msg.from.is_premium ?? false,
        firstBotId: bot.id,
        lastSeenAt: new Date(),
      },
      update: {
        username: msg.from.username,
        firstName: msg.from.first_name,
        lastName: msg.from.last_name,
        languageCode: msg.from.language_code,
        isPremium: msg.from.is_premium ?? false,
        lastSeenAt: new Date(),
      },
    });

    let conversation = await prisma.conversation.findFirst({
      where: {
        tenantId: bot.tenantId,
        botId: bot.id,
        userId: user.id,
        status: { in: ['OPEN', 'PENDING', 'LOCKED'] },
      },
    });

    if (!conversation) {
      conversation = await prisma.conversation.create({
        data: {
          tenantId: bot.tenantId,
          botId: bot.id,
          userId: user.id,
          status: 'OPEN',
          lastMessageAt: new Date(),
        },
      });
    }

    const { type, text, mediaFileId, mediaUrl } = this.extractMessageContent(msg);

    if (isEdit) {
      await prisma.message.updateMany({
        where: { conversationId: conversation.id, telegramMessageId: msg.message_id },
        data: { text: msg.text ?? msg.caption, isEdited: true, editedAt: new Date() },
      });
      return;
    }

    const messageId = randomUUID();
    await prisma.message.create({
      data: {
        id: messageId,
        tenantId: bot.tenantId,
        conversationId: conversation.id,
        telegramMessageId: msg.message_id,
        direction: 'INBOUND',
        senderType: 'USER',
        senderId: user.id,
        type,
        text,
        mediaFileId,
        sentAt: new Date(msg.date * 1000),
      },
    });

    await prisma.conversation.update({
      where: { id: conversation.id },
      data: { lastMessageAt: new Date(), status: 'OPEN' },
    });

    // Push real-time event to inbox WebSocket consumers
    await this.publishInboxEvent({
      tenantId: bot.tenantId,
      conversationId: conversation.id,
      message: { id: messageId, direction: 'INBOUND', type, text, sentAt: new Date(msg.date * 1000) },
      user: { telegramId: telegramUserId.toString(), username: user.username ?? null, firstName: user.firstName },
    });

    // Trigger matching keyword automations
    if (text) {
      await this.triggerAutomations(bot.tenantId, bot.id, conversation.id, user.id, text);
    }

    this.logger.log(`Message from ${msg.from.username ?? msg.from.id} in bot ${bot.id}`);
  }

  private async publishInboxEvent(payload: Record<string, unknown>): Promise<void> {
    try {
      await this.redis.publish(INBOX_CHANNEL, JSON.stringify(payload));
    } catch (err) {
      this.logger.warn('Redis publish failed (non-fatal)', err);
    }
  }

  private async triggerAutomations(
    tenantId: string,
    botId: string,
    conversationId: string,
    userId: string,
    text: string,
  ): Promise<void> {
    try {
      const automations = await prisma.automation.findMany({
        where: { tenantId, botId, isActive: true },
        select: { id: true, trigger: true, actions: true },
      });

      const lowerText = text.toLowerCase();
      for (const auto of automations) {
        const trigger = auto.trigger as { type?: string; keyword?: string } | null;
        if (!trigger || trigger.type !== 'keyword') continue;
        if (!trigger.keyword || !lowerText.includes(trigger.keyword.toLowerCase())) continue;

        await this.automationQueue.add(
          'execute',
          { automationId: auto.id, actions: auto.actions, event: { conversationId, userId } },
          { attempts: 2, backoff: { type: 'fixed', delay: 2000 }, removeOnComplete: 100 },
        );
      }
    } catch (err) {
      this.logger.warn('Automation trigger failed (non-fatal)', err);
    }
  }

  private extractMessageContent(msg: TelegramMessage): {
    type: string; text: string | null; mediaFileId: string | null; mediaUrl: string | null;
  } {
    if (msg.text) return { type: 'TEXT', text: msg.text, mediaFileId: null, mediaUrl: null };
    if (msg.photo) {
      const best = msg.photo[msg.photo.length - 1];
      return { type: 'PHOTO', text: msg.caption ?? null, mediaFileId: best.file_id, mediaUrl: null };
    }
    if (msg.video) return { type: 'VIDEO', text: msg.caption ?? null, mediaFileId: msg.video.file_id, mediaUrl: null };
    if (msg.voice) return { type: 'VOICE', text: null, mediaFileId: msg.voice.file_id, mediaUrl: null };
    if (msg.audio) return { type: 'AUDIO', text: msg.caption ?? null, mediaFileId: msg.audio.file_id, mediaUrl: null };
    if (msg.document) return { type: 'DOCUMENT', text: msg.caption ?? null, mediaFileId: msg.document.file_id, mediaUrl: null };
    if (msg.sticker) return { type: 'STICKER', text: null, mediaFileId: msg.sticker.file_id, mediaUrl: null };
    if (msg.location) return { type: 'LOCATION', text: null, mediaFileId: null, mediaUrl: null };
    if (msg.contact) return { type: 'CONTACT', text: null, mediaFileId: null, mediaUrl: null };
    return { type: 'TEXT', text: null, mediaFileId: null, mediaUrl: null };
  }

  private async handleChatMember(
    bot: { id: string; tenantId: string },
    update: TelegramUpdate,
  ): Promise<void> {
    const cm = update.my_chat_member!;
    if (cm.chat.type !== 'private') return;
    const isBlocked = cm.new_chat_member.status === 'kicked';
    const telegramId = BigInt(cm.from.id);
    await prisma.telegramUser.updateMany({
      where: { tenantId: bot.tenantId, telegramId },
      data: { isBlocked },
    });
  }

  async sendMessage(
    botToken: string,
    chatId: number,
    text: string,
    options: Record<string, unknown> = {},
  ): Promise<{ ok: boolean; messageId?: number }> {
    await this.outboundQueue.add(
      'send',
      { botToken, chatId, text, options },
      { attempts: 3, backoff: { type: 'exponential', delay: 1000 } },
    );
    return { ok: true };
  }
}
