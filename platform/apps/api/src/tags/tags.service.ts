import { Injectable, NotFoundException } from '@nestjs/common';
import { prisma } from '@platform/db';
import { CreateTagDto } from './dto/create-tag.dto';

@Injectable()
export class TagsService {
  async findAll(tenantId: string) {
    return prisma.tag.findMany({
      where: { tenantId },
      orderBy: { name: 'asc' },
    });
  }

  async create(tenantId: string, dto: CreateTagDto) {
    return prisma.tag.create({
      data: {
        tenantId,
        name: dto.name,
        ...(dto.color !== undefined && { color: dto.color }),
      },
    });
  }

  async delete(tenantId: string, id: string) {
    const tag = await prisma.tag.findFirst({ where: { id, tenantId } });
    if (!tag) throw new NotFoundException('Tag not found');
    await prisma.tag.delete({ where: { id } });
    return { ok: true };
  }

  async assignToUser(tenantId: string, telegramUserId: string, tagId: string) {
    // Verify the user belongs to this tenant
    const user = await prisma.telegramUser.findFirst({
      where: { id: telegramUserId, tenantId },
    });
    if (!user) throw new NotFoundException('User not found');

    // Verify the tag belongs to this tenant
    const tag = await prisma.tag.findFirst({ where: { id: tagId, tenantId } });
    if (!tag) throw new NotFoundException('Tag not found');

    return prisma.userTag.upsert({
      where: { userId_tagId: { userId: telegramUserId, tagId } },
      create: { userId: telegramUserId, tagId },
      update: {},
    });
  }

  async removeFromUser(tenantId: string, telegramUserId: string, tagId: string) {
    // Verify the user belongs to this tenant
    const user = await prisma.telegramUser.findFirst({
      where: { id: telegramUserId, tenantId },
    });
    if (!user) throw new NotFoundException('User not found');

    await prisma.userTag.deleteMany({
      where: { userId: telegramUserId, tagId },
    });
    return { ok: true };
  }
}
