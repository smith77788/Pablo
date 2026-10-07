import { Injectable, NotFoundException } from '@nestjs/common';
import { prisma } from '@platform/db';
import { CreateAutomationDto } from './dto/create-automation.dto';
import { UpdateAutomationDto } from './dto/update-automation.dto';

@Injectable()
export class AutomationsService {
  async findAll(tenantId: string) {
    return prisma.automation.findMany({
      where: { tenantId },
      orderBy: { createdAt: 'desc' },
    });
  }

  async findOne(tenantId: string, id: string) {
    const automation = await prisma.automation.findFirst({ where: { id, tenantId } });
    if (!automation) throw new NotFoundException('Automation not found');
    return automation;
  }

  async create(tenantId: string, dto: CreateAutomationDto) {
    return prisma.automation.create({
      data: {
        tenantId,
        name: dto.name,
        trigger: { type: dto.triggerType, ...(dto.keyword !== undefined && { keyword: dto.keyword }) },
        actions: [{ type: dto.actionType, payload: dto.actionPayload }],
        isActive: dto.isActive ?? true,
      },
    });
  }

  async update(tenantId: string, id: string, dto: UpdateAutomationDto) {
    const existing = await this.findOne(tenantId, id);

    const trigger =
      dto.triggerType !== undefined
        ? { type: dto.triggerType, ...(dto.keyword !== undefined && { keyword: dto.keyword }) }
        : (existing.trigger as object);

    const actions =
      dto.actionType !== undefined || dto.actionPayload !== undefined
        ? [
            {
              type: dto.actionType ?? (existing.actions as Array<{ type: string; payload: string }>)[0]?.type,
              payload: dto.actionPayload ?? (existing.actions as Array<{ type: string; payload: string }>)[0]?.payload,
            },
          ]
        : (existing.actions as object);

    return prisma.automation.update({
      where: { id },
      data: {
        ...(dto.name !== undefined && { name: dto.name }),
        trigger,
        actions,
        ...(dto.isActive !== undefined && { isActive: dto.isActive }),
      },
    });
  }

  async delete(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    await prisma.automation.delete({ where: { id } });
    return { ok: true };
  }

  async toggleActive(tenantId: string, id: string) {
    const automation = await this.findOne(tenantId, id);
    return prisma.automation.update({
      where: { id },
      data: { isActive: !automation.isActive },
    });
  }
}
