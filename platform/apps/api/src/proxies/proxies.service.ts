import { Injectable, NotFoundException } from '@nestjs/common';
import { prisma } from '@platform/db';
import { CreateProxyDto } from './dto/create-proxy.dto';
import { UpdateProxyDto } from './dto/update-proxy.dto';

@Injectable()
export class ProxiesService {
  async findAll(tenantId: string) {
    return (prisma as any).proxy.findMany({
      where: { tenantId },
      orderBy: { createdAt: 'desc' },
    });
  }

  async findOne(tenantId: string, id: string) {
    const proxy = await (prisma as any).proxy.findFirst({ where: { id, tenantId } });
    if (!proxy) throw new NotFoundException('Proxy not found');
    return proxy;
  }

  async create(tenantId: string, dto: CreateProxyDto) {
    return (prisma as any).proxy.create({
      data: { type: 'SOCKS5', ...dto, tenantId },
    });
  }

  async update(tenantId: string, id: string, dto: UpdateProxyDto) {
    await this.findOne(tenantId, id);
    return (prisma as any).proxy.update({ where: { id }, data: dto });
  }

  async delete(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    await (prisma as any).proxy.delete({ where: { id } });
    return { ok: true };
  }

  async check(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    await (prisma as any).proxy.update({
      where: { id },
      data: { lastCheckedAt: new Date() },
    });
    return { ok: true, checkedAt: new Date() };
  }
}
