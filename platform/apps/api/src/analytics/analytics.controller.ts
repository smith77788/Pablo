import { Controller, Get, Query, UseGuards, Req } from '@nestjs/common';
import { AnalyticsService } from './analytics.service';
import { JwtAuthGuard } from '../auth/jwt.guard';

@Controller('analytics')
@UseGuards(JwtAuthGuard)
export class AnalyticsController {
  constructor(private readonly svc: AnalyticsService) {}

  @Get('dashboard')
  dashboard(@Req() req: any, @Query('botId') botId?: string) {
    return this.svc.getDashboard(req.user.tenantId, botId);
  }

  @Get('bots/:botId/metrics')
  botMetrics(@Req() req: any, @Query('botId') botId: string, @Query('days') days?: string) {
    return this.svc.getBotMetrics(req.user.tenantId, botId, parseInt(days ?? '30'));
  }
}
