import { Injectable, NotFoundException } from '@nestjs/common';
import { prisma } from '@platform/db';
import { CreateOperationDto } from './dto/create-operation.dto';
import { UpdateOperationDto } from './dto/update-operation.dto';

@Injectable()
export class OperationsService {
  async findAll(tenantId: string, filters: { status?: string; type?: string }) {
    const { status, type } = filters;
    const where: Record<string, unknown> = { tenantId };
    if (status) where['status'] = status;
    if (type) where['type'] = type;

    return (prisma as any).operation.findMany({
      where,
      orderBy: { createdAt: 'desc' },
    });
  }

  async findOne(tenantId: string, id: string) {
    const operation = await (prisma as any).operation.findFirst({
      where: { id, tenantId },
      include: { steps: true },
    });
    if (!operation) throw new NotFoundException('Operation not found');
    return operation;
  }

  async create(tenantId: string, dto: CreateOperationDto) {
    return (prisma as any).operation.create({
      data: { ...dto, status: 'DRAFT', tenantId },
    });
  }

  async update(tenantId: string, id: string, dto: UpdateOperationDto) {
    await this.findOne(tenantId, id);
    return (prisma as any).operation.update({ where: { id }, data: dto });
  }

  async approve(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    return (prisma as any).operation.update({
      where: { id },
      data: { status: 'APPROVED' },
    });
  }

  async cancel(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    await (prisma as any).operation.update({
      where: { id },
      data: { status: 'CANCELLED' },
    });
    return { ok: true };
  }

  async submit(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    return (prisma as any).operation.update({
      where: { id },
      data: { status: 'PENDING_APPROVAL' },
    });
  }

  async getSteps(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    return (prisma as any).operationStep.findMany({
      where: { operationId: id },
      orderBy: { order: 'asc' },
    });
  }
}
