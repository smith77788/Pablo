import { Injectable, NotFoundException } from '@nestjs/common';
import { prisma } from '@platform/db';

@Injectable()
export class UsersService {
  async list(tenantId: string, opts: { page?: number; limit?: number; search?: string }) {
    const { page = 1, limit = 50, search } = opts;
    const where: any = { tenantId };
    if (search) {
      where.OR = [
        { username: { contains: search, mode: 'insensitive' } },
        { firstName: { contains: search, mode: 'insensitive' } },
      ];
    }
    const [total, items] = await prisma.$transaction([
      prisma.telegramUser.count({ where }),
      prisma.telegramUser.findMany({
        where, skip: (page - 1) * limit, take: limit,
        orderBy: { lastSeenAt: 'desc' },
        include: { userTags: { include: { tag: true } } },
      }),
    ]);
    return { total, page, limit, items };
  }

  async get(tenantId: string, id: string) {
    const user = await prisma.telegramUser.findFirst({
      where: { id, tenantId },
      include: {
        userTags: { include: { tag: true } },
        conversations: { orderBy: { lastMessageAt: 'desc' }, take: 10 },
      },
    });
    if (!user) throw new NotFoundException('User not found');
    return user;
  }

  async addTag(tenantId: string, userId: string, tagId: string) {
    const user = await prisma.telegramUser.findFirst({ where: { id: userId, tenantId } });
    if (!user) throw new NotFoundException();
    return prisma.userTag.create({ data: { userId, tagId } }).catch(() => ({ ok: true }));
  }

  async removeTag(tenantId: string, userId: string, tagId: string) {
    return prisma.userTag.delete({ where: { userId_tagId: { userId, tagId } } });
  }

  async updateCustomFields(tenantId: string, userId: string, fields: Record<string, unknown>) {
    const user = await prisma.telegramUser.findFirst({ where: { id: userId, tenantId } });
    if (!user) throw new NotFoundException();
    const existing = (user.customFields as any) ?? {};
    return prisma.telegramUser.update({
      where: { id: userId },
      data: { customFields: { ...existing, ...fields } },
    });
  }
}
