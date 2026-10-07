import { IsString, IsNotEmpty } from 'class-validator';

export class CreateBotDto {
  @IsString()
  @IsNotEmpty()
  name: string;

  @IsString()
  @IsNotEmpty()
  token: string;
}
