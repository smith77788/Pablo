import { Controller, Post, Get, Body, UseGuards, Req } from '@nestjs/common';
import { BotFactoryService } from './bot-factory.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { ValidateTokensDto } from './dto/validate-tokens.dto';
import { ImportBotsDto } from './dto/import-bots.dto';
import { CloneSettingsDto } from './dto/clone-settings.dto';

@Controller('bot-factory')
@UseGuards(JwtAuthGuard)
export class BotFactoryController {
  constructor(private readonly svc: BotFactoryService) {}

  @Post('validate')
  validate(@Body() dto: ValidateTokensDto) {
    return this.svc.validateTokens(dto);
  }

  @Post('import')
  import(@Body() dto: ImportBotsDto, @Req() req: any) {
    return this.svc.importBots(dto, req.user.tenantId, req.user.sub);
  }

  @Post('clone-settings')
  clone(@Body() dto: CloneSettingsDto, @Req() req: any) {
    return this.svc.cloneSettings(dto, req.user.tenantId);
  }

  @Get('stats')
  stats(@Req() req: any) {
    return this.svc.getBotStats(req.user.tenantId);
  }
}
