import { Controller, Post, Body, HttpCode } from '@nestjs/common';
import { AuthService } from './auth.service';
import { IsEmail, IsString, MinLength } from 'class-validator';

class RegisterDto {
  @IsString() tenantName: string;
  @IsEmail() email: string;
  @IsString() @MinLength(8) password: string;
}

class LoginDto {
  @IsEmail() email: string;
  @IsString() password: string;
}

class RefreshDto {
  @IsString() refreshToken: string;
}

@Controller('auth')
export class AuthController {
  constructor(private readonly auth: AuthService) {}

  @Post('register')
  register(@Body() dto: RegisterDto) {
    return this.auth.register(dto.tenantName, dto.email, dto.password);
  }

  @Post('login')
  @HttpCode(200)
  login(@Body() dto: LoginDto) {
    return this.auth.login(dto.email, dto.password);
  }

  @Post('refresh')
  @HttpCode(200)
  refresh(@Body() dto: RefreshDto) {
    return this.auth.refresh(dto.refreshToken);
  }
}
