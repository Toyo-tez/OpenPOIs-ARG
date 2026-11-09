# Export the environment to a yml file
export_env:
	@conda env export > environment.yml;

# Build conda environment from the yml file
build_env:
	@conda env create -f environment.yml;

# Install the package to pip
install_package:
	@pip install -e .;

# Run the unit test suite (no network calls, runs in seconds)
test:
	@python -m pytest tests/ -v;

# Lint source code, exploratory scripts, and tests
# Uses the openpois conda/micromamba env's binaries regardless of whether it is
# activated. Override with OPENPOIS_PYTHON=/path/to/python.
REPO_ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
export PYTHONPATH := $(REPO_ROOT)/src$(if $(PYTHONPATH),:$(PYTHONPATH))
CONDA_PYTHON ?= $(shell \
	if [ -n "$$OPENPOIS_PYTHON" ]; then echo "$$OPENPOIS_PYTHON"; \
	elif command -v conda >/dev/null 2>&1 && conda run -n openpois which python >/dev/null 2>&1; then \
		conda run -n openpois which python; \
	elif [ -x "$$HOME/.local/share/mamba/envs/openpois/bin/python" ]; then \
		echo "$$HOME/.local/share/mamba/envs/openpois/bin/python"; \
	elif command -v micromamba >/dev/null 2>&1 && micromamba run -n openpois which python >/dev/null 2>&1; then \
		micromamba run -n openpois which python; \
	else echo python; fi)
CONDA_BIN := $(dir $(CONDA_PYTHON))
# The env's PROJ/GDAL data directories are normally set by activation. Make
# invokes the interpreter directly (no activation), so export them here when
# they exist to keep GDAL/pyproj lookups quiet and reproducible.
ENV_PREFIX := $(shell $(CONDA_PYTHON) -c "import sys; print(sys.prefix)" 2>/dev/null)
ifneq ($(wildcard $(ENV_PREFIX)/share/proj),)
PROJ_DATA ?= $(ENV_PREFIX)/share/proj
GDAL_DATA ?= $(ENV_PREFIX)/share/gdal
export PROJ_DATA GDAL_DATA
endif

# Country selection. `make site_build COUNTRY=AR` (or `export OPENPOIS_COUNTRY=AR`)
# overrides the top-level `country` key in config.yaml for every pipeline step
# and for the generated site config.
COUNTRY ?=
ifneq ($(strip $(COUNTRY)),)
export OPENPOIS_COUNTRY := $(strip $(COUNTRY))
endif

lint:
	@$(CONDA_BIN)flake8 src/ scripts/ tests/
	@$(CONDA_BIN)pylint src/openpois/

# Regenerate the gitignored site inputs before Vite starts:
#   - site/src/taxonomy.generated.js and site/public/taxonomy.html from the
#     conflation taxonomy CSVs (scripts/build_taxonomy.py)
#   - site/src/country.generated.js (country, map view, PMTiles URLs)
site_config: build_taxonomy
	@$(CONDA_PYTHON) scripts/build_site_config.py;

# Install the frontend dependencies on first use. site/node_modules is
# gitignored, so a fresh clone needs this before Vite can run.
site_deps:
	@if [ ! -d site/node_modules ]; then \
		echo "Installing frontend dependencies in site/ ..."; \
		cd site && npm install; \
	fi

# Build the site for production
site_build: site_config site_deps
	@cd site && npm run build;

# Serve the site locally with hot reload
# Note: does not build Sphinx docs; use site_preview for a full build
site_dev: site_config site_deps
	@cd site && npm run dev;

# Generate site/src/taxonomy.generated.js and site/public/taxonomy.html from
# the conflation data CSVs. site_config depends on this, so site_dev,
# site_build and site_preview all regenerate it automatically.
build_taxonomy:
	@$(CONDA_PYTHON) scripts/build_taxonomy.py;

# Full build + local preview: Sphinx docs, Vite production build, then serve
# Mirrors the GitHub Actions workflow; serves at http://localhost:4173
# Requires the openpois conda env to be active (for sphinx-build)
# Uses Python's HTTP server instead of vite preview so /docs/ is served
# correctly (vite preview uses SPA fallback which swallows directory requests)
site_preview: site_config site_deps
	@$(CONDA_BIN)sphinx-build -b html docs docs/_build/html -q
	@cd site && npm run build
	@cp -r docs/_build/html site/dist/docs
	@$(CONDA_PYTHON) -m http.server 4173 --directory site/dist;

# -----------------------------------------------------------------------------
# Conflation pipeline (canonical entry point for all national runs)
#
# `make conflate` runs the five steps that produce the published
# conflated.parquet:
#
#   1. build_ghosts.py            - reconstruct "ghost" POI dataset
#                                    from OSM history (deletions,
#                                    primary-tag removals, lifecycle
#                                    prefixes, substantial renames).
#   2. conflate.py                 - OSM x Overture matching as before,
#                                    written to conflated_baseline.parquet
#                                    so the pre-CD result is archived.
#                                    assign_stable_ids.py then replaces
#                                    its provisional unified_id values
#                                    with release-stable ones, carried
#                                    forward through the prior release's
#                                    id registry (versions.id_registry).
#   3. apply_change_detection.py   - penalize Overture POIs that shadow-
#                                    match a same-entity ghost; writes
#                                    conflated_cd.parquet.
#   4. calibrate                   - fit the three Bayesian fixed-rate
#                                    mixture models (overture, osm,
#                                    matched), export them as grid
#                                    curves (stops if any segment fails
#                                    acceptance), and apply them; writes
#                                    the canonical conflated.parquet,
#                                    then the calibration figures and
#                                    the HT review.
#   5. apply_manual_overrides.py   - hand-curated exclude/include pins
#                                    (Close triage CSV), rewritten in
#                                    place over conflated.parquet. Runs
#                                    LAST so a forced conf_mean is never
#                                    re-scaled by calibration.
#
# Each sub-step tees a per-run log under the configured logs directory
# (usually <paths.data_root>/logs/).
#
# Pass TEST=1 to scope to the Seattle bbox:
#     make conflate            # full country run
#     make conflate TEST=1     # Seattle bbox dry run
# Under TEST=1 the Bayesian fit and export are skipped: the dry run applies
# the curves already in conflation/<version>/calibration/.
#
# In a validation month the run stops after change detection, because the
# validator samples from conflated_cd.parquet and calibration needs that
# round's handoff:
#     make conflate_to_cd      # steps 1-3
#     ... validation round, handoff ...
#     make calibrate apply_manual_overrides
#
# Sub-targets (build_ghosts / conflate_baseline / apply_cd / fit_calibration
# / export_calibration / apply_calibration / calibrate /
# apply_manual_overrides) are exposed for partial re-runs when one stage
# is being iterated on.

TEST ?=
TEST_FLAG := $(if $(TEST),--test,)
# Resolve the country-aware data/conflation paths at make time so AR runs do not
# write into the US namespace.
DATA_ROOT := $(shell $(CONDA_PYTHON) -c \
	"from openpois.config import load_config; print(load_config().get_dir_path('conflation').parent.parent)" \
	2>/dev/null || echo $(HOME)/data/openpois)
CONFLATION_DIR := $(shell $(CONDA_PYTHON) -c \
	"from openpois.config import load_config; print(load_config().get_dir_path('conflation'))" \
	2>/dev/null || echo $(DATA_ROOT)/conflation)
CALIBRATION_ENABLED := $(shell $(CONDA_PYTHON) -c \
	"from openpois.config import load_config; c = load_config().config; print(int(c.get('conflation', {}).get('calibration', {}).get('enabled', True)))" \
	2>/dev/null || echo 1)
ACTIVE_COUNTRY := $(shell $(CONDA_PYTHON) -c \
	"from openpois.config import country_code, load_config; print(country_code(load_config()))" \
	2>/dev/null || echo US)
OSM_SNAPSHOT_PATH := $(shell $(CONDA_PYTHON) -c \
	"from openpois.config import load_config; print(load_config().get_file_path('snapshot_osm','snapshot'))" \
	2>/dev/null || echo "<osm snapshot>")
OVERTURE_SNAPSHOT_PATH := $(shell $(CONDA_PYTHON) -c \
	"from openpois.config import load_config; print(load_config().get_file_path('snapshot_overture','snapshot'))" \
	2>/dev/null || echo "<overture snapshot>")
# assign_stable_ids mints every unified_id when this country has no prior
# release registry (first run). Later runs carry ids forward from the previous
# release named by versions.id_registry.
ID_REGISTRY_PATH := $(shell $(CONDA_PYTHON) -c \
	"from openpois.config import load_config; print(load_config().get_file_path('id_registry','registry'))" \
	2>/dev/null)
BOOTSTRAP_FLAG := $(if $(wildcard $(ID_REGISTRY_PATH)),,--bootstrap)
LOG_DIR := $(DATA_ROOT)/logs
LOG_TS := $(shell date +%Y%m%d_%H%M%S)

.PHONY: snapshot_osm snapshot_overture snapshots \
	download_history check_history rate conflate conflate_to_cd build_ghosts \
	conflate_baseline apply_cd fit_calibration export_calibration apply_calibration \
	calibrate apply_manual_overrides

# Pipeline 1: download the current OSM and Overture POI snapshots for the
# active country (config.yaml + countries/<ISO2>.yaml). Both scripts fetch
# their inputs automatically: OSM from Geofabrik, Overture from public S3, and
# the reference boundary from the URL in download.general.boundary.
snapshot_osm snapshot_overture: SHELL := /bin/bash
snapshot_osm snapshot_overture: .SHELLFLAGS := -o pipefail -c

snapshot_osm:
	@mkdir -p $(LOG_DIR)
	@$(CONDA_PYTHON) -u scripts/osm_snapshot/download.py \
		2>&1 | tee $(LOG_DIR)/osm_snapshot_$(LOG_TS).log

snapshot_overture:
	@mkdir -p $(LOG_DIR)
	@$(CONDA_PYTHON) -u scripts/overture/download.py \
		2>&1 | tee $(LOG_DIR)/overture_snapshot_$(LOG_TS).log

snapshots: snapshot_osm snapshot_overture
	@echo
	@echo "Snapshot downloads complete for $(ACTIVE_COUNTRY)."
	@echo "  OSM:      $(OSM_SNAPSHOT_PATH)"
	@echo "  Overture: $(OVERTURE_SNAPSHOT_PATH)"

# Build versions.osm_data: the full-history download (history_mode: full) or a
# roll-forward of download.osm.incremental_history.base_version with Geofabrik's
# daily diffs (history_mode: incremental). PLAN=1 prints the diff sequences an
# incremental run would fetch and exits.
download_history:
	@mkdir -p $(LOG_DIR)
	@$(CONDA_PYTHON) -u scripts/osm_data/download_history.py \
		$(if $(PLAN),--plan-only,) \
		2>&1 | tee $(LOG_DIR)/osm_history_$(LOG_TS).log

# QA: the history's last state of each snapshot node must match the snapshot.
check_history:
	@mkdir -p $(LOG_DIR)
	@$(CONDA_PYTHON) -u scripts/osm_data/check_history_vs_snapshot.py \
		2>&1 | tee $(LOG_DIR)/check_history_$(LOG_TS).log

# Rate the OSM snapshot with the production random_effects model (per-POI cell
# reconstruction). Uses apply_model.model_stub from config; pass MODEL_VERSION=
# to override. NOTE: this is the correct rater for random_effects — the older
# apply_model.py is per-group only and must not be used for it.
rate:
	@mkdir -p $(LOG_DIR)
	@$(CONDA_PYTHON) -u scripts/osm_snapshot/apply_model_random_effects.py \
		$(if $(MODEL_VERSION),--model-version $(MODEL_VERSION),) $(TEST_FLAG) \
		2>&1 | tee $(LOG_DIR)/rate_$(LOG_TS).log

build_ghosts:
	@mkdir -p $(LOG_DIR)
	@$(CONDA_PYTHON) -u scripts/conflation/build_ghosts.py \
		2>&1 | tee $(LOG_DIR)/build_ghosts_$(LOG_TS).log

# pipefail: a failed conflate.py must not hand a stale baseline to
# assign_stable_ids.py.
conflate_baseline: SHELL := /bin/bash
conflate_baseline: .SHELLFLAGS := -o pipefail -c
conflate_baseline:
	@mkdir -p $(LOG_DIR)
	@$(CONDA_PYTHON) -u scripts/conflation/conflate.py \
		--output-suffix=baseline $(TEST_FLAG) \
		2>&1 | tee $(LOG_DIR)/conflate_baseline_$(LOG_TS).log
	@$(CONDA_PYTHON) -u scripts/conflation/assign_stable_ids.py \
		$(BOOTSTRAP_FLAG) $(TEST_FLAG) \
		2>&1 | tee $(LOG_DIR)/assign_stable_ids_$(LOG_TS).log

apply_cd:
	@mkdir -p $(LOG_DIR)
	@$(CONDA_PYTHON) -u scripts/conflation/apply_change_detection.py \
		--baseline-suffix=baseline --output-suffix=cd $(TEST_FLAG) \
		2>&1 | tee $(LOG_DIR)/apply_cd_$(LOG_TS).log

# Fit the per-segment existence-confidence curves from the validation rounds in
# conflation.calibration.pooled_rounds, then map every POI through them.
# Calibration runs AFTER change detection: the CD penalty multiplies conf_mean,
# so calibrating first would leave a calibrated probability scaled by delta.
#
# fit_calibration runs the three Bayesian fixed-rate mixture fits in parallel
# (run_bayes_phase1.sh MODE=mixture; settings in conflation.calibration.bayes);
# export_calibration (--overwrite: a calibrate run replaces this version's curves)
# turns their draws into grid curves in
# conflation/<version>/calibration/ and exits non-zero, writing nothing
# deployable, if any segment fails the acceptance rule. The v4
# fit_calibration.py is retired. pipefail makes a failing step stop make
# despite the tee.
fit_calibration export_calibration apply_calibration calibrate: SHELL := /bin/bash
fit_calibration export_calibration apply_calibration calibrate: .SHELLFLAGS := -o pipefail -c

fit_calibration:
	@mkdir -p $(LOG_DIR)
ifeq ($(CALIBRATION_ENABLED),0)
	@echo "Calibration disabled in config; skipping the Bayesian fit"
else ifeq ($(TEST),)
	@MODE=mixture bash scripts/conflation/run_bayes_phase1.sh \
		2>&1 | tee $(LOG_DIR)/fit_calibration_$(LOG_TS).log
else
	@echo "TEST=1: skipping the Bayesian fit (applying the existing curves)"
endif

export_calibration:
	@mkdir -p $(LOG_DIR)
ifeq ($(CALIBRATION_ENABLED),0)
	@echo "Calibration disabled in config; skipping the curve export"
else ifeq ($(TEST),)
	@$(CONDA_PYTHON) -u scripts/conflation/export_bayes_curves.py --overwrite \
		2>&1 | tee $(LOG_DIR)/export_calibration_$(LOG_TS).log
else
	@echo "TEST=1: skipping the curve export (applying the existing curves)"
endif

apply_calibration:
	@mkdir -p $(LOG_DIR)
ifeq ($(CALIBRATION_ENABLED),0)
	@cp "$(CONFLATION_DIR)/conflated_cd$(if $(TEST),_test,).parquet" \
		"$(CONFLATION_DIR)/conflated$(if $(TEST),_test,).parquet"
	@echo "Calibration disabled in config; copied conflated_cd -> conflated"
else
	@$(CONDA_PYTHON) -u scripts/conflation/apply_calibration.py \
		--input-suffix=cd --output-suffix="" $(TEST_FLAG) \
		2>&1 | tee $(LOG_DIR)/apply_calibration_$(LOG_TS).log
endif

calibrate: fit_calibration export_calibration apply_calibration
ifeq ($(CALIBRATION_ENABLED),0)
	@echo "Calibration disabled in config; skipping plot_calibration + ht_review"
else
	@$(CONDA_PYTHON) -u scripts/conflation/plot_calibration.py \
		2>&1 | tee $(LOG_DIR)/plot_calibration_$(LOG_TS).log
	@$(CONDA_PYTHON) -u scripts/conflation/ht_review.py \
		2>&1 | tee $(LOG_DIR)/ht_review_$(LOG_TS).log
endif

# Manual exclude/include pins from the Close triage CSV. Must run AFTER
# calibrate: it rewrites conflated.parquet in place and a forced conf_mean
# of 0 / 1 must not be re-scaled by the curves. A missing CSV is a no-op.
apply_manual_overrides:
	@mkdir -p $(LOG_DIR)
	@$(CONDA_PYTHON) -u scripts/conflation/apply_manual_overrides.py \
		$(TEST_FLAG) \
		2>&1 | tee $(LOG_DIR)/apply_manual_overrides_$(LOG_TS).log

# Steps 1-3 only: stop at conflated_cd.parquet, the frame a validation round
# samples from. Run `make calibrate apply_manual_overrides` after the handoff.
conflate_to_cd: build_ghosts conflate_baseline apply_cd
	@echo
	@echo "Conflation through change detection complete."
	@echo "  Output: $(CONFLATION_DIR)/conflated_cd.parquet"
	@echo "  Next, after the validation handoff: make calibrate apply_manual_overrides"

conflate: build_ghosts conflate_baseline apply_cd calibrate apply_manual_overrides
	@echo
	@echo "Conflation pipeline complete."
	@echo "  Canonical output: $(CONFLATION_DIR)/conflated.parquet"
	@echo "  (calibrated + manual overrides applied in place)"
	@echo "  (pre-calibration: conflated_cd.parquet)"
	@echo "  (no-CD archive:   conflated_baseline.parquet)"
	@echo "  Bayesian fits: $(CONFLATION_DIR)/calibration_bayes/"
	@echo "  Grid curves + fit report + HT review: $(CONFLATION_DIR)/calibration/"
	@echo "  Logs under: $(LOG_DIR)/{build_ghosts,conflate_baseline,apply_cd,fit_calibration,export_calibration,apply_calibration,plot_calibration,ht_review,apply_manual_overrides}_$(LOG_TS).log"

# ---------------------------------------------------------------------------
# Reproducible Docker images
# ---------------------------------------------------------------------------
# The pipeline image pins GDAL/PROJ, osmium, tippecanoe and JAX and mounts the
# host data root; the site image bakes a static build for one country. See
# docker/README.md for prerequisites, credentials and caveats.
.PHONY: docker_pipeline docker_site docker_up docker_down

docker_pipeline:
	@docker build -f docker/pipeline.Dockerfile -t openpois-pipeline:local .

docker_site:
	@docker build -f docker/site.Dockerfile \
		--build-arg OPENPOIS_COUNTRY=$(if $(COUNTRY),$(COUNTRY),AR) \
		-t openpois-site:local .

# Start the local site + PMTiles server (builds both images on first run).
docker_up:
	@OPENPOIS_COUNTRY=$(if $(COUNTRY),$(COUNTRY),AR) docker compose up --build

docker_down:
	@docker compose down

# Convenience target to print all of the available targets in this file
# From https://stackoverflow.com/questions/4219255
.PHONY: list
list:
	@LC_ALL=C $(MAKE) -pRrq -f $(lastword $(MAKEFILE_LIST)) : 2>/dev/null | \
		awk -v RS= -F: '/^# File/,/^# Finished Make data base/ \
		{if ($$1 !~ "^[#.]") {print $$1}}' | \
		sort | egrep -v -e '^[^[:alnum:]]' -e '^$@$$'
