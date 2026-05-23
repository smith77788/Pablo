.PHONY: install dev build migrate

install:
	pnpm install

dev:
	pnpm turbo dev

build:
	pnpm turbo build

migrate:
	cd packages/db && pnpm migrate:deploy

generate:
	cd packages/db && pnpm generate

docker-up:
	cd infra && docker-compose up -d postgres redis clickhouse

docker-down:
	cd infra && docker-compose down
