import { Injectable, UnauthorizedException, ConflictException } from '@nestjs/common';
import { JwtService } from '@nestjs/jwt';
import * as bcrypt from 'bcryptjs';
import { prisma } from '@platform/db';
import { randomUUID } from 'crypto';

@Injectable()
export class AuthService {
  constructor(private readonly jwt: JwtService) {}

  async register(tenantName: string, email: string, password: string) {
    const slug = tenantName.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '');
    const existing = await prisma.tenant.findUnique({ where: { slug } });
    if (existing) throw new ConflictException('Tenant slug already exists');

    const tenant = await prisma.tenant.create({
      data: { name: tenantName, slug },
    });

    const passwordHash = await bcrypt.hash(password, 12);
    const operator = await prisma.operator.create({
      data: {
        tenantId: tenant.id,
        email,
        passwordHash,
        name: email.split('@')[0],
        role: 'OWNER',
      },
    });

    return this.generateTokens(operator);
  }

  async login(email: string, password: string) {
    const operator = await prisma.operator.findFirst({
      where: { email, isActive: true },
    });
    if (!operator) throw new UnauthorizedException('Invalid credentials');

    const valid = await bcrypt.compare(password, operator.passwordHash);
    if (!valid) throw new UnauthorizedException('Invalid credentials');

    await prisma.operator.update({
      where: { id: operator.id },
      data: { lastSeenAt: new Date() },
    });

    return this.generateTokens(operator);
  }

  async refresh(token: string) {
    const stored = await prisma.refreshToken.findUnique({ where: { token } });
    if (!stored || stored.expiresAt < new Date()) {
      throw new UnauthorizedException('Invalid or expired refresh token');
    }
    const operator = await prisma.operator.findUnique({ where: { id: stored.operatorId } });
    if (!operator) throw new UnauthorizedException();

    await prisma.refreshToken.delete({ where: { token } });
    return this.generateTokens(operator);
  }

  private async generateTokens(operator: { id: string; tenantId: string; email: string; role: string }) {
    const payload = { sub: operator.id, tenantId: operator.tenantId, email: operator.email, role: operator.role };
    const accessToken = this.jwt.sign(payload);
    const refreshToken = randomUUID();
    const expiresAt = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);

    await prisma.refreshToken.create({
      data: { operatorId: operator.id, token: refreshToken, expiresAt },
    });

    return { accessToken, refreshToken, operatorId: operator.id, tenantId: operator.tenantId };
  }
}
