import { Injectable, NotFoundException, ForbiddenException } from '@nestjs/common';
import { prisma } from '@platform/db';

@Injectable()
export class ConversationsService {
  async list(tenantId: string, filters: {
    status?: string; botId?: string; assignedToId?: string; page?: number; limit?: number;
  }) {
    const { status, botId, assignedToId, page = 1, limit = 30 } = filters;
    const where: any = { tenantId };
    if (status) where.status = status;
    if (botId) where.botId = botId;
    if (assignedToId) where.assignedToId = assignedToId;

    const [total, items] = await prisma.$transaction([
      prisma.conversation.count({ where }),
      prisma.conversation.findMany({
        where,
        orderBy: { lastMessageAt: 'desc' },
        skip: (page - 1) * limit,
        take: limit,
        include: {
          user: { select: { id: true, telegramId: true, username: true, firstName: true, lastName: true } },
          bot: { select: { id: true, username: true, firstName: true } },
          assignedTo: { select: { id: true, name: true, avatarUrl: true } },
          messages: { orderBy: { sentAt: 'desc' }, take: 1 },
          _count: { select: { messages: true } },
        },
      }),
    ]);

    return { total, page, limit, items };
  }

  async get(tenantId: string, id: string) {
    const conv = await prisma.conversation.findFirst({
      where: { id, tenantId },
      include: {
        user: true,
        bot: { select: { id: true, username: true, firstName: true, token: true } },
        assignedTo: { select: { id: true, name: true, avatarUrl: true } },
        messages: { orderBy: { sentAt: 'asc' }, take: 100 },
        notes: { include: { operator: { select: { id: true, name: true } } }, orderBy: { createdAt: 'asc' } },
        convTags: { include: { tag: true } },
      },
    });
    if (!conv) throw new NotFoundException('Conversation not found');
    return conv;
  }

  async assign(tenantId: string, id: string, operatorId: string | null) {
    const conv = await prisma.conversation.findFirst({ where: { id, tenantId } });
    if (!conv) throw new NotFoundException();
    return prisma.conversation.update({
      where: { id },
      data: { assignedToId: operatorId },
    });
  }

  async updateStatus(tenantId: string, id: string, status: string) {
    const conv = await prisma.conversation.findFirst({ where: { id, tenantId } });
    if (!conv) throw new NotFoundException();
    return prisma.conversation.update({
      where: { id },
      data: {
        status: status as any,
        resolvedAt: status === 'RESOLVED' ? new Date() : undefined,
      },
    });
  }

  async addNote(tenantId: string, id: string, operatorId: string, text: string) {
    const conv = await prisma.conversation.findFirst({ where: { id, tenantId } });
    if (!conv) throw new NotFoundException();
    return prisma.internalNote.create({
      data: { conversationId: id, operatorId, text },
      include: { operator: { select: { id: true, name: true } } },
    });
  }

  async sendMessage(tenantId: string, id: string, operatorId: string, text: string) {
    const conv = await prisma.conversation.findFirst({
      where: { id, tenantId },
      include: { bot: true, user: true },
    });
    if (!conv) throw new NotFoundException();

    // Store message in DB
    const message = await prisma.message.create({
      data: {
        tenantId,
        conversationId: id,
        operatorId,
        direction: 'OUTBOUND',
        senderType: 'OPERATOR',
        senderId: operatorId,
        type: 'TEXT',
        text,
        sentAt: new Date(),
      },
    });

    // Send via Telegram (non-blocking fire-and-forget to keep response fast)
    const { default: axios } = await import('axios');
    const https = await import('https');
    const agent = new https.Agent({ rejectUnauthorized: false });
    axios.post(
      `https://api.telegram.org/bot${conv.bot.token}/sendMessage`,
      { chat_id: conv.user.telegramId.toString(), text, parse_mode: 'HTML' },
      { httpsAgent: agent, timeout: 15_000 },
    ).then(async (resp: any) => {
      if (resp.data?.ok) {
        await prisma.message.update({
          where: { id: message.id },
          data: { telegramMessageId: resp.data.result.message_id, deliveredAt: new Date() },
        });
      }
    }).catch(() => {});

    await prisma.conversation.update({
      where: { id },
      data: { lastMessageAt: new Date(), firstReplyAt: conv.firstReplyAt ?? new Date() },
    });

    return message;
  }
}
