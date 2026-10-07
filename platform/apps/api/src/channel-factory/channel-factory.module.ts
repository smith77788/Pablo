import { Module } from '@nestjs/common';
import { ChannelFactoryController } from './channel-factory.controller';
import { ChannelFactoryService } from './channel-factory.service';

@Module({
  controllers: [ChannelFactoryController],
  providers: [ChannelFactoryService],
})
export class ChannelFactoryModule {}
