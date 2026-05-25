import {
  Controller, Get, Post, Delete,
  Param, Body, UseGuards, Req,
} from '@nestjs/common';
import { ApiKeysService } from './api-keys.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { CreateApiKeyDto } from './dto/create-api-key.dto';

@Controller('api-keys')
@UseGuards(JwtAuthGuard)
export class ApiKeysController {
  constructor(private readonly apiKeys: ApiKeysService) {}

  @Get()
  list(@Req() req: any) {
    return this.apiKeys.findAll(req.user.tenantId);
  }

  @Post()
  create(@Req() req: any, @Body() dto: CreateApiKeyDto) {
    return this.apiKeys.create(req.user.tenantId, dto);
  }

  @Delete(':id')
  revoke(@Req() req: any, @Param('id') id: string) {
    return this.apiKeys.revoke(req.user.tenantId, id);
  }
}
