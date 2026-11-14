.DEFAULT_GOAL := run
PYTHON ?= python3
COMPOSE = docker compose --env-file .env.local
.PHONY: run rund services stop gen load setup teardown test
run:
	$(COMPOSE) up -d --build --wait
rund:
	$(COMPOSE) up --build
services:
	$(COMPOSE) ps
stop:
	$(COMPOSE) down
gen:
	$(PYTHON) -m scripts.generate
load:
	$(PYTHON) -m scripts.manage load
setup:
	$(PYTHON) -m scripts.manage schema
	$(MAKE) gen
	$(MAKE) load
teardown:
	$(PYTHON) -m scripts.manage teardown
test:
	$(PYTHON) -m unittest discover -s tests -v
