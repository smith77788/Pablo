import { Injectable, NotFoundException } from '@nestjs/common';
import { prisma } from '@platform/db';
import { CreateClusterDto } from './dto/create-cluster.dto';
import { UpdateClusterDto } from './dto/update-cluster.dto';

@Injectable()
export class ClustersService {
  async findAll(tenantId: string) {
    return (prisma as any).cluster.findMany({
      where: { tenantId },
      orderBy: { createdAt: 'desc' },
      include: { _count: { select: { assets: true } } },
    });
  }

  async findOne(tenantId: string, id: string) {
    const cluster = await (prisma as any).cluster.findFirst({
      where: { id, tenantId },
      include: { _count: { select: { assets: true } } },
    });
    if (!cluster) throw new NotFoundException('Cluster not found');
    return cluster;
  }

  async create(tenantId: string, dto: CreateClusterDto) {
    return (prisma as any).cluster.create({ data: { ...dto, tenantId } });
  }

  async update(tenantId: string, id: string, dto: UpdateClusterDto) {
    await this.findOne(tenantId, id);
    return (prisma as any).cluster.update({ where: { id }, data: dto });
  }

  async delete(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    await (prisma as any).cluster.delete({ where: { id } });
    return { ok: true };
  }

  async getAssets(tenantId: string, clusterId: string) {
    await this.findOne(tenantId, clusterId);
    return (prisma as any).asset.findMany({
      where: { clusterId, tenantId },
      orderBy: { createdAt: 'desc' },
    });
  }

  async addAsset(tenantId: string, clusterId: string, assetId: string) {
    await this.findOne(tenantId, clusterId);
    return (prisma as any).asset.update({
      where: { id: assetId },
      data: { clusterId },
    });
  }

  async removeAsset(tenantId: string, clusterId: string, assetId: string) {
    await this.findOne(tenantId, clusterId);
    await (prisma as any).asset.update({
      where: { id: assetId },
      data: { clusterId: null },
    });
    return { ok: true };
  }
}
