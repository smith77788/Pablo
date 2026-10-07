import { Module } from '@nestjs/common';
import { InboxGateway } from './inbox.gateway';

@Module({
  providers: [InboxGateway],
  exports: [InboxGateway],
})
export class InboxModule {}
