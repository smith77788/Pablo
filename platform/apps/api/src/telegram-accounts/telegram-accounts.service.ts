import { Injectable, NotFoundException } from '@nestjs/common';
import { prisma } from '@platform/db';
import { CreateTelegramAccountDto } from './dto/create-telegram-account.dto';
import { UpdateTelegramAccountDto } from './dto/update-telegram-account.dto';

const SAFE_SELECT = {
  id: true,
  phone: true,
  username: true,
  firstName: true,
  status: true,
  projectId: true,
  clusterId: true,
  proxyId: true,
  tags: true,
  notes: true,
  healthScore: true,
  floodCount: true,
  cooldownUntil: true,
  createdAt: true,
  updatedAt: true,
};

@Injectable()
export class TelegramAccountsService {
  async findAll(tenantId: string, filters: {
    status?: string;
    clusterId?: string;
    search?: string;
  }) {
    const { status, clusterId, search } = filters;
    const where: Record<string, unknown> = { tenantId };
    if (status) where['status'] = status;
    if (clusterId) where['clusterId'] = clusterId;
    if (search) {
      where['OR'] = [
        { phone: { contains: search, mode: 'insensitive' } },
        { username: { contains: search, mode: 'insensitive' } },
        { firstName: { contains: search, mode: 'insensitive' } },
      ];
    }

    return (prisma as any).telegramAccount.findMany({
      where,
      select: SAFE_SELECT,
      orderBy: { createdAt: 'desc' },
    });
  }

  async findOne(tenantId: string, id: string) {
    const account = await (prisma as any).telegramAccount.findFirst({
      where: { id, tenantId },
      select: SAFE_SELECT,
    });
    if (!account) throw new NotFoundException('Telegram account not found');
    return account;
  }

  async create(tenantId: string, dto: CreateTelegramAccountDto) {
    return (prisma as any).telegramAccount.create({
      data: { ...dto, tenantId },
      select: SAFE_SELECT,
    });
  }

  async update(tenantId: string, id: string, dto: UpdateTelegramAccountDto) {
    await this.findOne(tenantId, id);
    return (prisma as any).telegramAccount.update({
      where: { id },
      data: dto,
      select: SAFE_SELECT,
    });
  }

  async archive(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    await (prisma as any).telegramAccount.update({
      where: { id },
      data: { status: 'ARCHIVED' },
    });
    return { ok: true };
  }

  async getHealth(tenantId: string, id: string) {
    const account = await (prisma as any).telegramAccount.findFirst({
      where: { id, tenantId },
      select: {
        id: true,
        healthScore: true,
        floodCount: true,
        cooldownUntil: true,
        status: true,
      },
    });
    if (!account) throw new NotFoundException('Telegram account not found');
    return account;
  }

  async deactivate(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    await (prisma as any).telegramAccount.update({
      where: { id },
      data: { status: 'INACTIVE' },
    });
    return { ok: true };
  }

  async bulkAssignCluster(tenantId: string, accountIds: string[], clusterId: string) {
    await (prisma as any).telegramAccount.updateMany({
      where: { id: { in: accountIds }, tenantId },
      data: { clusterId },
    });
    return { ok: true, updated: accountIds.length };
  }
}
