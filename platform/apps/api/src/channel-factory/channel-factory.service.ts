import { Injectable, BadRequestException } from '@nestjs/common';
import { prisma } from '@platform/db';
import { CreateChannelDto } from './dto/create-channel.dto';
import { BulkCreateChannelsDto } from './dto/bulk-create-channels.dto';
import { MassPublishDto, MassPublishScope } from './dto/mass-publish.dto';

@Injectable()
export class ChannelFactoryService {
  async createChannel(
    dto: CreateChannelDto,
    tenantId: string,
    operatorId: string,
  ) {
    // Verify account belongs to tenant
    const account = await prisma.telegramAccount.findFirst({
      where: { id: dto.accountId, tenantId },
    });
    if (!account) throw new BadRequestException('Account not found');

    // In production: call Telethon microservice to create channel
    // For now: record the operation
    const op = await (prisma as any).operation.create({
      data: {
        tenantId,
        createdById: operatorId,
        name: `Create channel: ${dto.title}`,
        type: 'CHANNEL_CREATE',
        status: 'DRAFT',
        params: { ...dto },
      },
    });

    return {
      operationId: op.id,
      status: 'queued',
      message: 'Channel creation queued. Check operations for status.',
    };
  }

  async bulkCreateChannels(
    dto: BulkCreateChannelsDto,
    tenantId: string,
    operatorId: string,
  ) {
    const account = await prisma.telegramAccount.findFirst({
      where: { id: dto.accountId, tenantId },
    });
    if (!account) throw new BadRequestException('Account not found');

    const ops: string[] = [];
    for (let i = 1; i <= dto.count; i++) {
      const op = await (prisma as any).operation.create({
        data: {
          tenantId,
          createdById: operatorId,
          name: `Bulk create: ${dto.titlePrefix} ${i}`,
          type: 'CHANNEL_CREATE',
          status: 'DRAFT',
          params: {
            accountId: dto.accountId,
            title: `${dto.titlePrefix} ${i}`,
            about: dto.about,
          },
        },
      });
      ops.push(op.id);
    }

    return {
      queued: ops.length,
      operationIds: ops,
      message: `${ops.length} channel creation operations queued.`,
    };
  }

  async massPublish(
    dto: MassPublishDto,
    tenantId: string,
    operatorId: string,
  ) {
    // Get accounts based on scope
    const accountFilter: Record<string, any> = { tenantId };
    if (dto.scope === MassPublishScope.BY_ACCOUNT && dto.accountId) {
      accountFilter['id'] = dto.accountId;
    }

    const accounts = await prisma.telegramAccount.findMany({
      where: accountFilter,
      select: { id: true, phone: true },
    });

    if (dto.dryRun) {
      return {
        dryRun: true,
        estimatedAccounts: accounts.length,
        estimatedChannels: accounts.length * 3, // placeholder
        estimatedDuration: `~${Math.ceil((accounts.length * 3 * dto.delaySeconds) / 60)} min`,
        delaySeconds: dto.delaySeconds,
        text: dto.text,
      };
    }

    // Queue mass publish operation
    const op = await (prisma as any).operation.create({
      data: {
        tenantId,
        createdById: operatorId,
        name: `Mass publish: ${dto.text.substring(0, 50)}${dto.text.length > 50 ? '...' : ''}`,
        type: 'MASS_PUBLISH',
        status: 'DRAFT',
        params: { ...dto, accountIds: accounts.map((a) => a.id) },
      },
    });

    return {
      operationId: op.id,
      status: 'queued',
      accounts: accounts.length,
      message: 'Mass publish operation queued.',
    };
  }
}
