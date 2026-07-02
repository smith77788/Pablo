import { IsArray, IsString, ArrayMinSize, ArrayMaxSize } from 'class-validator';

export class ValidateTokensDto {
  @IsArray()
  @ArrayMinSize(1)
  @ArrayMaxSize(100)
  @IsString({ each: true })
  tokens: string[];
}
