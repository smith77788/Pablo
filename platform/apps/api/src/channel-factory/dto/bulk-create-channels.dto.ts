import { IsString, IsNumber, IsOptional, Min, Max } from 'class-validator';

export class BulkCreateChannelsDto {
  @IsString()
  accountId: string;

  @IsNumber()
  @Min(1)
  @Max(10)
  count: number;

  @IsString()
  titlePrefix: string;  // "Shop" → channels "Shop 1", "Shop 2"...

  @IsOptional()
  @IsString()
  about?: string;
}
