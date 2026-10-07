import { Injectable, NotFoundException } from '@nestjs/common';
import { InjectQueue } from '@nestjs/bull';
import { Queue } from 'bull';
import { prisma } from '@platform/db';

@Injectable()
export class BroadcastsService {
  constructor(@InjectQueue('broadcasts') private readonly queue: Queue) {}

  async create(tenantId: string, dto: {
    botId: string; name: string; message: any; segmentId?: string; scheduledAt?: string;
  }) {
    const bot = await prisma.bot.findFirst({ where: { id: dto.botId, tenantId } });
    if (!bot) throw new NotFoundException('Bot not found');

    return prisma.broadcast.create({
      data: {
        tenantId,
        botId: dto.botId,
        name: dto.name,
        message: dto.message,
        segmentId: dto.segmentId,
        scheduledAt: dto.scheduledAt ? new Date(dto.scheduledAt) : undefined,
        status: dto.scheduledAt ? 'SCHEDULED' : 'DRAFT',
      },
    });
  }

  async launch(tenantId: string, broadcastId: string) {
    const bc = await prisma.broadcast.findFirst({ where: { id: broadcastId, tenantId } });
    if (!bc) throw new NotFoundException();

    await prisma.broadcast.update({
      where: { id: broadcastId },
      data: { status: 'RUNNING', startedAt: new Date() },
    });

    await this.queue.add('run', { broadcastId, tenantId }, {
      attempts: 1,
      removeOnComplete: 50,
    });

    return { ok: true };
  }

  // ─── CRUD ─────────────────────────────────────────────────────────────────

  async findAll(tenantId: string, botId?: string) {
    return prisma.broadcast.findMany({
      where: { tenantId, ...(botId ? { botId } : {}) },
      orderBy: { createdAt: 'desc' },
      take: 50,
      include: { bot: { select: { username: true, firstName: true } } },
    });
  }

  async findOne(tenantId: string, id: string) {
    const bc = await prisma.broadcast.findFirst({
      where: { id, tenantId },
      include: { recipients: { take: 100 } },
    });
    if (!bc) throw new NotFoundException('Broadcast not found');
    return bc;
  }

  // ─── STATS ────────────────────────────────────────────────────────────────

  async getStats(tenantId: string, id: string) {
    const bc = await prisma.broadcast.findFirst({ where: { id, tenantId } });
    if (!bc) throw new NotFoundException('Broadcast not found');

    const total = bc.totalCount;
    const sent = bc.sentCount;
    const failed = bc.failedCount;
    const successRate = total > 0 ? Math.round((sent / total) * 100) : 0;

    return {
      total,
      sent,
      failed,
      status: bc.status,
      successRate,
    };
  }

  // ─── LEGACY ALIASES (kept for backward compatibility) ─────────────────────

  async list(tenantId: string, botId?: string) {
    return this.findAll(tenantId, botId);
  }

  async get(tenantId: string, id: string) {
    return this.findOne(tenantId, id);
  }
}
