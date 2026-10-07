FROM node:20-alpine AS deps
RUN npm install -g pnpm@9
WORKDIR /app
COPY package.json pnpm-workspace.yaml turbo.json ./
COPY apps/gateway/package.json ./apps/gateway/
COPY packages/db/package.json ./packages/db/
COPY packages/types/package.json ./packages/types/
COPY packages/config/package.json ./packages/config/
RUN pnpm install --frozen-lockfile

FROM deps AS builder
COPY . .
RUN pnpm --filter @platform/db run db:generate
RUN pnpm --filter @platform/gateway run build

FROM node:20-alpine AS runner
WORKDIR /app
RUN npm install -g pnpm@9
COPY --from=builder /app/apps/gateway/dist ./apps/gateway/dist
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/packages ./packages
ENV NODE_ENV=production
EXPOSE 3001
CMD ["node", "apps/gateway/dist/main"]
