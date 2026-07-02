import {
  Controller, Get, Post, Delete,
  Param, Body, UseGuards, Req,
} from '@nestjs/common';
import { TagsService } from './tags.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { CreateTagDto } from './dto/create-tag.dto';

@Controller('tags')
@UseGuards(JwtAuthGuard)
export class TagsController {
  constructor(private readonly tags: TagsService) {}

  @Get()
  list(@Req() req: any) {
    return this.tags.findAll(req.user.tenantId);
  }

  @Post()
  create(@Req() req: any, @Body() dto: CreateTagDto) {
    return this.tags.create(req.user.tenantId, dto);
  }

  @Delete(':id')
  delete(@Req() req: any, @Param('id') id: string) {
    return this.tags.delete(req.user.tenantId, id);
  }

  @Post('users/:userId/assign')
  assign(@Req() req: any, @Param('userId') userId: string, @Body() body: { tagId: string }) {
    return this.tags.assignToUser(req.user.tenantId, userId, body.tagId);
  }

  @Delete('users/:userId/tags/:tagId')
  removeFromUser(
    @Req() req: any,
    @Param('userId') userId: string,
    @Param('tagId') tagId: string,
  ) {
    return this.tags.removeFromUser(req.user.tenantId, userId, tagId);
  }
}
