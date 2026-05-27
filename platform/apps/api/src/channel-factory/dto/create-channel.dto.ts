import { IsString, IsOptional } from 'class-validator';

export class CreateChannelDto {
  @IsString()
  accountId: string;  // TelegramAccount id in our DB

  @IsString()
  title: string;

  @IsOptional()
  @IsString()
  about?: string;

  @IsOptional()
  @IsString()
  username?: string;

  @IsOptional()
  @IsString()
  clusterId?: string;
}
