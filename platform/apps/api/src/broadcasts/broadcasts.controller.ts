import { Controller, Get, Post, Param, Body, Query, UseGuards, Req } from '@nestjs/common';
import { BroadcastsService } from './broadcasts.service';
import { JwtAuthGuard } from '../auth/jwt.guard';

@Controller('broadcasts')
@UseGuards(JwtAuthGuard)
export class BroadcastsController {
  constructor(private readonly svc: BroadcastsService) {}

  @Get() list(@Req() req: any, @Query('botId') botId?: string) {
    return this.svc.list(req.user.tenantId, botId);
  }

  @Post() create(@Req() req: any, @Body() dto: any) {
    return this.svc.create(req.user.tenantId, dto);
  }

  @Get(':id') get(@Req() req: any, @Param('id') id: string) {
    return this.svc.get(req.user.tenantId, id);
  }

  @Post(':id/launch') launch(@Req() req: any, @Param('id') id: string) {
    return this.svc.launch(req.user.tenantId, id);
  }
}
