import { Injectable, NotFoundException } from '@nestjs/common';
import { prisma } from '@platform/db';
import * as crypto from 'crypto';
import { CreateApiKeyDto } from './dto/create-api-key.dto';

@Injectable()
export class ApiKeysService {
  async findAll(tenantId: string) {
    return prisma.apiKey.findMany({
      where: { tenantId },
      orderBy: { createdAt: 'desc' },
      select: {
        id: true,
        name: true,
        // prefix is derived from keyHash prefix — we store it separately via name convention
        // The actual prefix (first 8 chars) is stored as part of keyHash field for display
        keyHash: true,
        createdAt: true,
        expiresAt: true,
        lastUsedAt: true,
      },
    });
  }

  async create(tenantId: string, dto: CreateApiKeyDto) {
    // Generate a 32-byte random key, encode as hex (64 chars)
    const rawKey = crypto.randomBytes(32).toString('hex');
    const prefix = rawKey.slice(0, 8);
    const keyHash = crypto.createHash('sha256').update(rawKey).digest('hex');

    const record = await prisma.apiKey.create({
      data: {
        tenantId,
        name: dto.name,
        keyHash,
        expiresAt: dto.expiresAt ? new Date(dto.expiresAt) : null,
      },
      select: {
        id: true,
        name: true,
        createdAt: true,
        expiresAt: true,
      },
    });

    // Return the raw key only once, along with metadata
    return {
      ...record,
      prefix,
      key: rawKey, // returned only once, not stored
    };
  }

  async revoke(tenantId: string, id: string) {
    const existing = await prisma.apiKey.findFirst({ where: { id, tenantId } });
    if (!existing) throw new NotFoundException('API key not found');
    await prisma.apiKey.delete({ where: { id } });
    return { ok: true };
  }
}
