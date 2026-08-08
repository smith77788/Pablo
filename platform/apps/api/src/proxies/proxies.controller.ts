import {
  Controller, Get, Post, Patch, Delete,
  Param, Body, UseGuards, Req,
} from '@nestjs/common';
import { ProxiesService } from './proxies.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { CreateProxyDto } from './dto/create-proxy.dto';
import { UpdateProxyDto } from './dto/update-proxy.dto';

@Controller('proxies')
@UseGuards(JwtAuthGuard)
export class ProxiesController {
  constructor(private readonly proxies: ProxiesService) {}

  @Get()
  list(@Req() req: any) {
    return this.proxies.findAll(req.user.tenantId);
  }

  @Post()
  create(@Req() req: any, @Body() dto: CreateProxyDto) {
    return this.proxies.create(req.user.tenantId, dto);
  }

  @Patch(':id')
  update(@Req() req: any, @Param('id') id: string, @Body() dto: UpdateProxyDto) {
    return this.proxies.update(req.user.tenantId, id, dto);
  }

  @Delete(':id')
  delete(@Req() req: any, @Param('id') id: string) {
    return this.proxies.delete(req.user.tenantId, id);
  }

  @Post(':id/check')
  check(@Req() req: any, @Param('id') id: string) {
    return this.proxies.check(req.user.tenantId, id);
  }
}
