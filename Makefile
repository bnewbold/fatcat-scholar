
SHELL = /bin/bash
.SHELLFLAGS = -o pipefail -c
TODAY ?= $(shell date --iso --utc)

.PHONY: help
help: ## Print info about all commands
	@echo "Commands:"
	@echo
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "    \033[01;32m%-20s\033[0m %s\n", $$1, $$2}'

.PHONY: dep
dep: ## Install dependencies using pipenv
	pipenv install --dev

.PHONY: lint
lint: ## Run lints (eg, flake8, mypy)
	pipenv run flake8 fatcat_scholar/ tests/ --exit-zero
	pipenv run isort -q -c fatcat_scholar/ tests/ || true
	pipenv run mypy fatcat_scholar/ tests/ --ignore-missing-imports

.PHONY: pytype
pytype: ## Run slow pytype type check (not part of dev deps)
	pipenv run pytype fatcat_scholar/

.PHONY: fmt
fmt: ## Run code formatting on all source code
	pipenv run isort --atomic fatcat_scholar/ tests/
	pipenv run black --line-length 88 fatcat_scholar/ tests/

.PHONY: test
test: ## Run all tests and lints
	PIPENV_DONT_LOAD_ENV=1 ENV_FOR_DYNACONF=test pipenv run pytest

.PHONY: coverage
coverage: ## Run all tests with coverage
	PIPENV_DONT_LOAD_ENV=1 ENV_FOR_DYNACONF=test pipenv run pytest --cov --cov-report=term --cov-report=html

.PHONY: serve
serve: ## Run web service locally, with reloading
	ENV_FOR_DYNACONF=development pipenv run uvicorn fatcat_scholar.web:app --reload --port 9819

.PHONY: serve-qa
serve-qa: ## Run web service locally, with reloading, but point search queries to QA search index
	ENV_FOR_DYNACONF=development-qa pipenv run uvicorn fatcat_scholar.web:app --reload --port 9819

.PHONY: serve-gunicorn
serve-gunicorn: ## Run web service under gunicorn
	pipenv run gunicorn fatcat_scholar.web:app -b 127.0.0.1:9819 -w 4 -k uvicorn.workers.UvicornWorker

.PHONY: fetch-works
fetch-works: ## Fetches some works from any release .json in the data dir
	cat data/release_*.json | jq . -c | pipenv run python -m fatcat_scholar.work_pipeline run_releases | pv -l > data/work_intermediate.json

.PHONY: fetch-sim
fetch-sim: ## Fetches some (not all) SIM pages
	pipenv run python -m fatcat_scholar.sim_pipeline run_issue_db --limit 500 | pv -l > data/sim_intermediate.json

.PHONY: dev-index
dev-index: ## Delete/Create DEV elasticsearch fulltext index locally
	http delete ":9200/dev_scholar_fulltext_v01" && true
	http put ":9200/dev_scholar_fulltext_v01?include_type_name=true" < schema/scholar_fulltext.v01.json
	http put ":9200/dev_scholar_fulltext_v01/_alias/dev_scholar_fulltext"
	cat data/sim_intermediate.json data/work_intermediate.json | pipenv run python -m fatcat_scholar.transform run_transform | esbulk -verbose -size 200 -id key -w 4 -index dev_scholar_fulltext_v01 -type _doc

.PHONY: extract-i18n
extract-i18n:  ## Re-extract translation source strings (for weblate)
	pipenv run pybabel extract -F extra/i18n/babel.cfg -o extra/i18n/web_interface.pot fatcat_scholar/

.PHONY: recompile-i18n
recompile-i18n: extract-i18n  ## Re-extract and compile all translation files (bypass weblate)
	pipenv run pybabel update -i extra/i18n/web_interface.pot -d fatcat_scholar/translations
	pipenv run pybabel compile -d fatcat_scholar/translations

data/$(TODAY)/sim_collections.tsv:
	mkdir -p data/$(TODAY)
	pipenv run ia search 'collection:periodicals collection:sim_microfilm mediatype:collection (pub_type:"Scholarly Journals" OR pub_type:"Historical Journals" OR pub_type:"Law Journals")' --itemlist | rg "^pub_" | pv -l > $@.wip
	mv $@.wip $@

data/$(TODAY)/sim_items.tsv:
	mkdir -p data/$(TODAY)
	pipenv run ia search 'collection:periodicals collection:sim_microfilm mediatype:texts !noindex:true (pub_type:"Scholarly Journals" OR pub_type:"Historical Journals" OR pub_type:"Law Journals")' --itemlist | rg "^sim_" | pv -l > $@.wip
	mv $@.wip $@

data/$(TODAY)/sim_collections.json: data/$(TODAY)/sim_collections.tsv
	cat data/$(TODAY)/sim_collections.tsv | pipenv run parallel -j20 ia metadata {} | jq . -c | pv -l > $@.wip
	mv $@.wip $@

data/$(TODAY)/sim_items.json: data/$(TODAY)/sim_items.tsv
	cat data/$(TODAY)/sim_items.tsv | pipenv run parallel -j20 ia metadata {} | jq -c 'del(.histograms, .rotations)' | pv -l > $@.wip
	mv $@.wip $@

data/$(TODAY)/issue_db.sqlite: data/$(TODAY)/sim_collections.json data/$(TODAY)/sim_items.json
	pipenv run python -m fatcat_scholar.issue_db --db-file $@.wip init_db
	cat data/$(TODAY)/sim_collections.json | pv -l | pipenv run python -m fatcat_scholar.issue_db --db-file $@.wip load_pubs -
	cat data/$(TODAY)/sim_items.json | pv -l | pipenv run python -m fatcat_scholar.issue_db --db-file $@.wip load_issues -
	pipenv run python -m fatcat_scholar.issue_db --db-file $@.wip load_counts
	mv $@.wip $@

data/issue_db.sqlite: data/$(TODAY)/issue_db.sqlite
	cp data/$(TODAY)/issue_db.sqlite data/issue_db.sqlite

.PHONY: issue-db
issue-db: data/issue_db.sqlite  ## Build SIM issue database with today's metadata, then move to default location
