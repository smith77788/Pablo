import { Injectable, NotFoundException } from '@nestjs/common';
import { prisma } from '@platform/db';
import { CreateTemplateDto } from './dto/create-template.dto';
import { UpdateTemplateDto } from './dto/update-template.dto';

@Injectable()
export class TemplatesService {
  async findAll(tenantId: string) {
    return prisma.template.findMany({
      where: { tenantId },
      orderBy: { createdAt: 'desc' },
    });
  }

  async findOne(tenantId: string, id: string) {
    const template = await prisma.template.findFirst({ where: { id, tenantId } });
    if (!template) throw new NotFoundException('Template not found');
    return template;
  }

  async create(tenantId: string, dto: CreateTemplateDto) {
    return prisma.template.create({
      data: {
        tenantId,
        name: dto.name,
        content: dto.content,
        ...(dto.category !== undefined && { category: dto.category }),
      },
    });
  }

  async update(tenantId: string, id: string, dto: UpdateTemplateDto) {
    await this.findOne(tenantId, id);
    return prisma.template.update({
      where: { id },
      data: {
        ...(dto.name !== undefined && { name: dto.name }),
        ...(dto.content !== undefined && { content: dto.content }),
        ...(dto.category !== undefined && { category: dto.category }),
      },
    });
  }

  async delete(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    await prisma.template.delete({ where: { id } });
    return { ok: true };
  }
}
