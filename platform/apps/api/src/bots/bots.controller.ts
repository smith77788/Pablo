import {
  Controller, Get, Post, Patch, Delete,
  Param, Body, UseGuards, Req,
} from '@nestjs/common';
import { BotsService } from './bots.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { CreateBotDto } from './dto/create-bot.dto';
import { UpdateBotDto } from './dto/update-bot.dto';
import { IsString, IsUrl } from 'class-validator';

class SetWebhookDto { @IsUrl() webhookUrl: string; }

@Controller('bots')
@UseGuards(JwtAuthGuard)
export class BotsController {
  constructor(private readonly bots: BotsService) {}

  @Get()
  list(@Req() req: any) {
    return this.bots.findAll(req.user.tenantId);
  }

  @Post()
  create(@Req() req: any, @Body() dto: CreateBotDto) {
    return this.bots.create(req.user.tenantId, dto);
  }

  @Get(':id')
  get(@Req() req: any, @Param('id') id: string) {
    return this.bots.findOne(req.user.tenantId, id);
  }

  @Patch(':id')
  update(@Req() req: any, @Param('id') id: string, @Body() dto: UpdateBotDto) {
    return this.bots.update(req.user.tenantId, id, dto);
  }

  @Delete(':id')
  delete(@Req() req: any, @Param('id') id: string) {
    return this.bots.delete(req.user.tenantId, id);
  }

  @Get(':id/stats')
  stats(@Req() req: any, @Param('id') id: string) {
    return this.bots.getStats(req.user.tenantId, id);
  }

  @Post(':id/webhook')
  setWebhook(@Req() req: any, @Param('id') id: string, @Body() dto: SetWebhookDto) {
    return this.bots.setWebhook(req.user.tenantId, id, dto.webhookUrl);
  }
}
