import { IsArray, IsString, ArrayMinSize } from 'class-validator';

export class ImportBotsDto {
  @IsArray()
  @ArrayMinSize(1)
  @IsString({ each: true })
  tokens: string[];
}
