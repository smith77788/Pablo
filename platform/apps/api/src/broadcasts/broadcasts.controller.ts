import { Controller, Get, Post, Param, Body, Query, UseGuards, Req } from '@nestjs/common';
import { BroadcastsService } from './broadcasts.service';
import { JwtAuthGuard } from '../auth/jwt.guard';

@Controller('broadcasts')
@UseGuards(JwtAuthGuard)
export class BroadcastsController {
  constructor(private readonly svc: BroadcastsService) {}

  @Get()
  findAll(@Req() req: any, @Query('botId') botId?: string) {
    return this.svc.findAll(req.user.tenantId, botId);
  }

  @Post()
  create(@Req() req: any, @Body() dto: any) {
    return this.svc.create(req.user.tenantId, dto);
  }

  @Get(':id/stats')
  getStats(@Req() req: any, @Param('id') id: string) {
    return this.svc.getStats(req.user.tenantId, id);
  }

  @Get(':id')
  findOne(@Req() req: any, @Param('id') id: string) {
    return this.svc.findOne(req.user.tenantId, id);
  }

  @Post(':id/launch')
  launch(@Req() req: any, @Param('id') id: string) {
    return this.svc.launch(req.user.tenantId, id);
  }
}
