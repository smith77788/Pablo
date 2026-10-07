import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module';

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  const port = parseInt(process.env.WORKER_PORT ?? '3003', 10);
  await app.listen(port, '0.0.0.0');
  console.log(`Worker running on port ${port}`);
}
bootstrap();
