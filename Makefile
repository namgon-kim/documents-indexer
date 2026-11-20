UV ?= uv
NPM ?= npm
ENV_FILE ?= .env
HOST ?= 127.0.0.1
PORT ?= 8000

.PHONY: setup build run check

setup:
	$(UV) sync --locked
	$(NPM) ci --prefix apps/web
	@test -e "$(ENV_FILE)" || cp .env.example "$(ENV_FILE)"

build:
	$(NPM) run build --prefix apps/web

run:
	$(UV) run --locked --env-file "$(ENV_FILE)" uvicorn apps.api.main:create_app --factory --host "$(HOST)" --port "$(PORT)"

check:
	$(UV) run --locked pytest -q
	$(UV) run --locked ruff check apps/api packages tests scripts
