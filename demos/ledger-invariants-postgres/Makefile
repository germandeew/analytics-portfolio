# Подключение берётся из окружения: PGHOST, PGPORT, PGUSER (для Docker ещё PGPASSWORD).
PSQL = psql -X -q -v ON_ERROR_STOP=1
DB ?= ledger_demo

.PHONY: test mutate

test:
	$(PSQL) -d postgres -c 'drop database if exists $(DB)' -c 'create database $(DB)'
	$(PSQL) -d $(DB) -f schema.sql -f seed.sql -f tests.sql

mutate:
	python3 mutate.py
