import {
  Controller, Get, Post, Patch, Delete,
  Param, Body, UseGuards, Req,
} from '@nestjs/common';
import { AutomationsService } from './automations.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { CreateAutomationDto } from './dto/create-automation.dto';
import { UpdateAutomationDto } from './dto/update-automation.dto';

@Controller('automations')
@UseGuards(JwtAuthGuard)
export class AutomationsController {
  constructor(private readonly automations: AutomationsService) {}

  @Get()
  list(@Req() req: any) {
    return this.automations.findAll(req.user.tenantId);
  }

  @Get(':id')
  get(@Req() req: any, @Param('id') id: string) {
    return this.automations.findOne(req.user.tenantId, id);
  }

  @Post()
  create(@Req() req: any, @Body() dto: CreateAutomationDto) {
    return this.automations.create(req.user.tenantId, dto);
  }

  @Patch(':id')
  update(@Req() req: any, @Param('id') id: string, @Body() dto: UpdateAutomationDto) {
    return this.automations.update(req.user.tenantId, id, dto);
  }

  @Delete(':id')
  delete(@Req() req: any, @Param('id') id: string) {
    return this.automations.delete(req.user.tenantId, id);
  }

  @Patch(':id/toggle')
  toggle(@Req() req: any, @Param('id') id: string) {
    return this.automations.toggleActive(req.user.tenantId, id);
  }
}
