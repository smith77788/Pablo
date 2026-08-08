import { IsString, IsArray, IsOptional } from 'class-validator';

export class CloneSettingsDto {
  @IsString()
  sourceBotId: string;

  @IsArray()
  @IsString({ each: true })
  targetBotIds: string[];

  @IsArray()
  @IsOptional()
  fields?: string[];  // 'name' | 'description' | 'short_description' | 'commands'
}
