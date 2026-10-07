import { Injectable, NotFoundException } from '@nestjs/common';
import { prisma } from '@platform/db';
import { CreateAssetDto } from './dto/create-asset.dto';
import { UpdateAssetDto } from './dto/update-asset.dto';

@Injectable()
export class AssetsService {
  async findAll(tenantId: string, filters: {
    type?: string;
    status?: string;
    projectId?: string;
    clusterId?: string;
    search?: string;
    page?: number;
    limit?: number;
  }) {
    const { type, status, projectId, clusterId, search, page = 1, limit = 20 } = filters;
    const where: Record<string, unknown> = { tenantId };
    if (type) where['type'] = type;
    if (status) where['status'] = status;
    if (projectId) where['projectId'] = projectId;
    if (clusterId) where['clusterId'] = clusterId;
    if (search) {
      where['OR'] = [
        { name: { contains: search, mode: 'insensitive' } },
        { username: { contains: search, mode: 'insensitive' } },
      ];
    }

    const skip = (page - 1) * limit;
    const [items, total] = await Promise.all([
      (prisma as any).asset.findMany({ where, skip, take: limit, orderBy: { createdAt: 'desc' } }),
      (prisma as any).asset.count({ where }),
    ]);

    return { items, total, page, limit };
  }

  async findOne(tenantId: string, id: string) {
    const asset = await (prisma as any).asset.findFirst({ where: { id, tenantId } });
    if (!asset) throw new NotFoundException('Asset not found');
    return asset;
  }

  async create(tenantId: string, dto: CreateAssetDto) {
    return (prisma as any).asset.create({ data: { ...dto, tenantId } });
  }

  async update(tenantId: string, id: string, dto: UpdateAssetDto) {
    await this.findOne(tenantId, id);
    return (prisma as any).asset.update({ where: { id }, data: dto });
  }

  async archive(tenantId: string, id: string) {
    await this.findOne(tenantId, id);
    await (prisma as any).asset.update({ where: { id }, data: { status: 'ARCHIVED' } });
    return { ok: true };
  }

  async bulkTag(tenantId: string, assetIds: string[], tags: string[]) {
    await (prisma as any).asset.updateMany({
      where: { id: { in: assetIds }, tenantId },
      data: { tags },
    });
    return { ok: true, updated: assetIds.length };
  }

  async bulkCluster(tenantId: string, assetIds: string[], clusterId: string) {
    await (prisma as any).asset.updateMany({
      where: { id: { in: assetIds }, tenantId },
      data: { clusterId },
    });
    return { ok: true, updated: assetIds.length };
  }
}
