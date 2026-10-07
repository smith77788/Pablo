import {
  Controller, Get, Post, Patch,
  Param, Body, Query, UseGuards, Req,
} from '@nestjs/common';
import { OperationsService } from './operations.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { CreateOperationDto } from './dto/create-operation.dto';
import { UpdateOperationDto } from './dto/update-operation.dto';

@Controller('operations')
@UseGuards(JwtAuthGuard)
export class OperationsController {
  constructor(private readonly operations: OperationsService) {}

  @Get()
  list(
    @Req() req: any,
    @Query('status') status?: string,
    @Query('type') type?: string,
  ) {
    return this.operations.findAll(req.user.tenantId, { status, type });
  }

  @Post()
  create(@Req() req: any, @Body() dto: CreateOperationDto) {
    return this.operations.create(req.user.tenantId, dto);
  }

  @Get(':id')
  get(@Req() req: any, @Param('id') id: string) {
    return this.operations.findOne(req.user.tenantId, id);
  }

  @Patch(':id')
  update(@Req() req: any, @Param('id') id: string, @Body() dto: UpdateOperationDto) {
    return this.operations.update(req.user.tenantId, id, dto);
  }

  @Post(':id/approve')
  approve(@Req() req: any, @Param('id') id: string) {
    return this.operations.approve(req.user.tenantId, id);
  }

  @Post(':id/cancel')
  cancel(@Req() req: any, @Param('id') id: string) {
    return this.operations.cancel(req.user.tenantId, id);
  }

  @Post(':id/submit')
  submit(@Req() req: any, @Param('id') id: string) {
    return this.operations.submit(req.user.tenantId, id);
  }

  @Get(':id/steps')
  steps(@Req() req: any, @Param('id') id: string) {
    return this.operations.getSteps(req.user.tenantId, id);
  }
}
