import {
  Controller, Get, Post, Patch, Delete,
  Param, Body, Query, UseGuards, Req,
} from '@nestjs/common';
import { TelegramAccountsService } from './telegram-accounts.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { CreateTelegramAccountDto } from './dto/create-telegram-account.dto';
import { UpdateTelegramAccountDto } from './dto/update-telegram-account.dto';

class BulkAssignClusterDto { accountIds: string[]; clusterId: string; }

@Controller('telegram-accounts')
@UseGuards(JwtAuthGuard)
export class TelegramAccountsController {
  constructor(private readonly accounts: TelegramAccountsService) {}

  @Get()
  list(
    @Req() req: any,
    @Query('status') status?: string,
    @Query('clusterId') clusterId?: string,
    @Query('search') search?: string,
  ) {
    return this.accounts.findAll(req.user.tenantId, { status, clusterId, search });
  }

  @Post()
  create(@Req() req: any, @Body() dto: CreateTelegramAccountDto) {
    return this.accounts.create(req.user.tenantId, dto);
  }

  @Get(':id')
  get(@Req() req: any, @Param('id') id: string) {
    return this.accounts.findOne(req.user.tenantId, id);
  }

  @Patch(':id')
  update(@Req() req: any, @Param('id') id: string, @Body() dto: UpdateTelegramAccountDto) {
    return this.accounts.update(req.user.tenantId, id, dto);
  }

  @Delete(':id')
  archive(@Req() req: any, @Param('id') id: string) {
    return this.accounts.archive(req.user.tenantId, id);
  }

  @Get(':id/health')
  health(@Req() req: any, @Param('id') id: string) {
    return this.accounts.getHealth(req.user.tenantId, id);
  }

  @Post(':id/deactivate')
  deactivate(@Req() req: any, @Param('id') id: string) {
    return this.accounts.deactivate(req.user.tenantId, id);
  }

  @Post('bulk-assign-cluster')
  bulkAssignCluster(@Req() req: any, @Body() dto: BulkAssignClusterDto) {
    return this.accounts.bulkAssignCluster(req.user.tenantId, dto.accountIds, dto.clusterId);
  }
}
