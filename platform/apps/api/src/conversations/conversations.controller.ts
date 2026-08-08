import { Controller, Get, Post, Patch, Param, Body, Query, UseGuards, Req } from '@nestjs/common';
import { ConversationsService } from './conversations.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { IsString, IsOptional } from 'class-validator';

class AssignDto { @IsOptional() @IsString() operatorId: string | null; }
class StatusDto { @IsString() status: string; }
class NoteDto { @IsString() text: string; }
class MessageDto { @IsString() text: string; }

@Controller('conversations')
@UseGuards(JwtAuthGuard)
export class ConversationsController {
  constructor(private readonly svc: ConversationsService) {}

  @Get()
  list(@Req() req: any, @Query() q: any) {
    return this.svc.list(req.user.tenantId, {
      status: q.status, botId: q.botId, assignedToId: q.assignedToId,
      page: parseInt(q.page ?? '1'), limit: parseInt(q.limit ?? '30'),
    });
  }

  @Get(':id')
  get(@Req() req: any, @Param('id') id: string) {
    return this.svc.get(req.user.tenantId, id);
  }

  @Get(':id/messages')
  getMessages(
    @Req() req: any,
    @Param('id') id: string,
    @Query('limit') limit?: string,
  ) {
    return this.svc.getMessages(req.user.tenantId, id, limit ? parseInt(limit, 10) : 50);
  }

  @Patch(':id/assign')
  assign(@Req() req: any, @Param('id') id: string, @Body() dto: AssignDto) {
    return this.svc.assign(req.user.tenantId, id, dto.operatorId);
  }

  @Patch(':id/status')
  status(@Req() req: any, @Param('id') id: string, @Body() dto: StatusDto) {
    return this.svc.updateStatus(req.user.tenantId, id, dto.status);
  }

  @Post(':id/notes')
  note(@Req() req: any, @Param('id') id: string, @Body() dto: NoteDto) {
    return this.svc.addNote(req.user.tenantId, id, req.user.sub, dto.text);
  }

  @Post(':id/messages')
  message(@Req() req: any, @Param('id') id: string, @Body() dto: MessageDto) {
    return this.svc.sendMessage(req.user.tenantId, id, req.user.sub, dto.text);
  }
}
