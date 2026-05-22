import { Module } from '@nestjs/common';
import { BullModule } from '@nestjs/bull';
import { RelayProcessor } from './relay.processor';
import { RelayService } from './relay.service';
import { TelegramClient } from './telegram.client';

@Module({
  imports: [
    BullModule.registerQueue({ name: 'updates' }),
    BullModule.registerQueue({ name: 'outbound' }),
  ],
  providers: [RelayProcessor, RelayService, TelegramClient],
  exports: [RelayService, TelegramClient],
})
export class RelayModule {}
