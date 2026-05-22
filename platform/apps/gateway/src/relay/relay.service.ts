import { Injectable, Logger } from '@nestjs/common';
import { InjectQueue } from '@nestjs/bull';
import { Queue } from 'bull';
import { prisma } from '@platform/db';
import { TelegramUpdate, TelegramMessage } from '@platform/types';
import { randomUUID } from 'crypto';

@Injectable()
export class RelayService {
  private readonly logger = new Logger(RelayService.name);

  constructor(@InjectQueue('outbound') private readonly outboundQueue: Queue) {}

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

    // Upsert TelegramUser
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

    // Get or create conversation
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

    // Determine message type and content
    const { type, text, mediaFileId, mediaUrl } = this.extractMessageContent(msg);

    if (isEdit) {
      await prisma.message.updateMany({
        where: { conversationId: conversation.id, telegramMessageId: msg.message_id },
        data: { text: msg.text ?? msg.caption, isEdited: true, editedAt: new Date() },
      });
      return;
    }

    // Store inbound message
    await prisma.message.create({
      data: {
        id: randomUUID(),
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

    // Update conversation
    await prisma.conversation.update({
      where: { id: conversation.id },
      data: { lastMessageAt: new Date(), status: 'OPEN' },
    });

    this.logger.log(`Message from ${msg.from.username ?? msg.from.id} in bot ${bot.id}`);
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
