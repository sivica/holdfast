.PHONY: paper paper-live test eval lint serve ci

export PYTHONPATH := src:$(PYTHONPATH)

paper:
	python3 -m hlguard run --scenario btc_short_approved

paper-live:
	python3 -m hlguard run --live --ticker BTC

test:
	python3 -m pytest

eval:
	python3 -m hlguard eval --fail-under-baseline

lint:
	python3 -m ruff check src tests
	python3 -m ruff format --check src tests

serve:
	python3 -m hlguard serve --host 0.0.0.0 --port 8741 --demo

ci: lint test eval
