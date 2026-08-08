import { IsString, IsOptional, IsNotEmpty } from 'class-validator';

export class UpdateBotDto {
  @IsString()
  @IsNotEmpty()
  @IsOptional()
  name?: string;
}
