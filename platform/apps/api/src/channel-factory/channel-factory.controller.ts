import { Controller, Post, Body, UseGuards, Req } from '@nestjs/common';
import { ChannelFactoryService } from './channel-factory.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { CreateChannelDto } from './dto/create-channel.dto';
import { BulkCreateChannelsDto } from './dto/bulk-create-channels.dto';
import { MassPublishDto } from './dto/mass-publish.dto';

@Controller('channel-factory')
@UseGuards(JwtAuthGuard)
export class ChannelFactoryController {
  constructor(private readonly svc: ChannelFactoryService) {}

  @Post('create')
  create(@Body() dto: CreateChannelDto, @Req() req: any) {
    return this.svc.createChannel(dto, req.user.tenantId, req.user.sub);
  }

  @Post('bulk-create')
  bulkCreate(@Body() dto: BulkCreateChannelsDto, @Req() req: any) {
    return this.svc.bulkCreateChannels(dto, req.user.tenantId, req.user.sub);
  }

  @Post('mass-publish')
  massPublish(@Body() dto: MassPublishDto, @Req() req: any) {
    return this.svc.massPublish(dto, req.user.tenantId, req.user.sub);
  }
}
