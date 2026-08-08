import { IsString, IsOptional, IsNumber, IsNotEmpty } from 'class-validator';

export class CreateProxyDto {
  @IsString()
  @IsNotEmpty()
  host: string;

  @IsNumber()
  port: number;

  @IsString()
  @IsOptional()
  type?: string;

  @IsString()
  @IsOptional()
  username?: string;

  @IsString()
  @IsOptional()
  password?: string;

  @IsString()
  @IsOptional()
  region?: string;
}
