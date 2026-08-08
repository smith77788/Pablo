import { Module } from '@nestjs/common';
import { TelegramAccountsController } from './telegram-accounts.controller';
import { TelegramAccountsService } from './telegram-accounts.service';

@Module({
  controllers: [TelegramAccountsController],
  providers: [TelegramAccountsService],
  exports: [TelegramAccountsService],
})
export class TelegramAccountsModule {}
