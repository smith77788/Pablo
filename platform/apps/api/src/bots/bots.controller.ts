import { Controller, Get, Post, Delete, Param, Body, UseGuards, Req } from '@nestjs/common';
import { BotsService } from './bots.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { IsString, IsUrl } from 'class-validator';

class AddBotDto { @IsString() token: string; }
class SetWebhookDto { @IsUrl() webhookUrl: string; }

@Controller('bots')
@UseGuards(JwtAuthGuard)
export class BotsController {
  constructor(private readonly bots: BotsService) {}

  @Get()
  list(@Req() req: any) { return this.bots.listBots(req.user.tenantId); }

  @Post()
  add(@Req() req: any, @Body() dto: AddBotDto) {
    return this.bots.addBot(req.user.tenantId, dto.token);
  }

  @Get(':id')
  get(@Req() req: any, @Param('id') id: string) {
    return this.bots.getBot(req.user.tenantId, id);
  }

  @Delete(':id')
  delete(@Req() req: any, @Param('id') id: string) {
    return this.bots.deleteBot(req.user.tenantId, id);
  }

  @Post(':id/webhook')
  setWebhook(@Req() req: any, @Param('id') id: string, @Body() dto: SetWebhookDto) {
    return this.bots.setWebhook(req.user.tenantId, id, dto.webhookUrl);
  }
}
