import { Module } from '@nestjs/common';
import { BullModule } from '@nestjs/bull';
import { BroadcastProcessor } from './broadcast.processor';
import { BroadcastSender } from './broadcast.sender';

@Module({
  imports: [BullModule.registerQueue({ name: 'broadcasts' })],
  providers: [BroadcastProcessor, BroadcastSender],
})
export class BroadcastModule {}
