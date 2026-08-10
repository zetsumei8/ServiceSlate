.PHONY: format format-check check tests security build prod restore-drill

format:
	python -m ruff format src tests scripts
	python -m ruff check --fix src tests scripts

format-check:
	python -m ruff format --check src tests scripts

check:
	python -m ruff check src tests scripts
	python -m ty check src
	python -m basedpyright src

tests:
	PYTHONPATH=src pytest --cov=serviceslate --cov-branch --cov-report=term-missing

security:
	python -m bandit -q -r src -x tests
	python -m pip_audit

build:
	python -m build

restore-drill:
	PYTHONPATH=src python scripts/restore_drill.py

prod: format-check check tests security build restore-drill
