import { Controller, Get, Post, Delete, Patch, Param, Body, Query, UseGuards, Req } from '@nestjs/common';
import { UsersService } from './users.service';
import { JwtAuthGuard } from '../auth/jwt.guard';

@Controller('users')
@UseGuards(JwtAuthGuard)
export class UsersController {
  constructor(private readonly svc: UsersService) {}

  @Get() list(@Req() req: any, @Query() q: any) {
    return this.svc.list(req.user.tenantId, { page: +q.page || 1, limit: +q.limit || 50, search: q.search });
  }

  @Get(':id') get(@Req() req: any, @Param('id') id: string) {
    return this.svc.get(req.user.tenantId, id);
  }

  @Post(':id/tags') addTag(@Req() req: any, @Param('id') id: string, @Body() b: any) {
    return this.svc.addTag(req.user.tenantId, id, b.tagId);
  }

  @Delete(':id/tags/:tagId') removeTag(@Req() req: any, @Param('id') id: string, @Param('tagId') tagId: string) {
    return this.svc.removeTag(req.user.tenantId, id, tagId);
  }

  @Patch(':id/fields') updateFields(@Req() req: any, @Param('id') id: string, @Body() b: any) {
    return this.svc.updateCustomFields(req.user.tenantId, id, b);
  }
}
