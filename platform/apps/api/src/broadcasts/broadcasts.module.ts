import { Module } from '@nestjs/common';
import { BullModule } from '@nestjs/bull';
import { BroadcastsController } from './broadcasts.controller';
import { BroadcastsService } from './broadcasts.service';

@Module({
  imports: [BullModule.registerQueue({ name: 'broadcasts' })],
  controllers: [BroadcastsController],
  providers: [BroadcastsService],
})
export class BroadcastsModule {}
