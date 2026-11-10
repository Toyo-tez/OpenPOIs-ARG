#!/usr/bin/env bash
#
# run_pipeline.sh - One-shot OpenPOIs pipeline runner
#
# Runs the full pipeline end-to-end from a single command:
#   download_history → format_tabular → osm_turnover → snapshot_osm →
#   snapshot_overture → rate → build_ghosts → conflate_baseline →
#   apply_cd → calibrate → apply_manual_overrides → build PMTiles →
#   deploy to site-data → restart pmtiles + site
#
# Features:
#   * Prompts for the Geofabrik cookie file if not present at the default path
#   * Skips steps whose output already exists (safe to re-run)
#   * Retries each docker command at least once on failure
#   * Prompts the user if any pipeline step failed after retries
#
# Usage:
#   ./run_pipeline.sh [COUNTRY]
#   OPENPOIS_COUNTRY=AR ./run_pipeline.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
cd "$REPO_ROOT"

COUNTRY="${OPENPOIS_COUNTRY:-${1:-AR}}"
export OPENPOIS_COUNTRY="$COUNTRY"

DATA_HOST_ROOT="${HOME}/data/openpois"
COUNTRY_ROOT="${DATA_HOST_ROOT}/${COUNTRY}"
# Geofabrik history downloads are OAuth-protected; the cookie jar lives at the
# openpois data root (not under the country subdirectory).
COOKIE_DEFAULT="${DATA_HOST_ROOT}/.creds/geofabrik_cookies.txt"

# Version defaults (from countries/${COUNTRY}.yaml / config.yaml); overridable
# via environment variables for re-runs with different versions.
OSM_DATA_VERSION="${OSM_DATA_VERSION:-20261105}"
OVERTURE_VERSION="${OVERTURE_VERSION:-20261021}"
CONFLATION_VERSION="${CONFLATION_VERSION:-20261105}"
MODEL_OUTPUT_VERSION="${MODEL_OUTPUT_VERSION:-20261105_by_shared_label}"
GHOST_VERSION="${GHOST_VERSION:-20261105}"

MAX_ATTEMPTS=2
FAILED_STEPS=()
SKIPPED_STEPS=()

###############################################################################
# Helpers
###############################################################################
log()  { echo "[$(date +'%H:%M:%S')] $*"; }
warn() { echo "[$(date +'%H:%M:%S')] !!! $*" >&2; }

# --- Ensure the Geofabrik cookie file exists; prompt the user if missing ---
ensure_cookie() {
    if [[ -f "$COOKIE_DEFAULT" ]]; then
        log "Geofabrik cookie found: $COOKIE_DEFAULT"
        return 0
    fi
    warn "Geofabrik cookie file not found at: $COOKIE_DEFAULT"
    echo
    echo "The OSM history download (osm-internal.download.geofabrik.de) requires"
    echo "an authenticated cookie jar (Netscape / Mozilla format). Generate one by:"
    echo "  1. Logging in at https://osm-internal.download.geofabrik.de/"
    echo "  2. Exporting cookies (browser extension), or"
    echo "  3. Running Geofabrik's oauth_cookie_client.py"
    echo
    read -rp "Enter path to your cookie jar (or leave blank to skip auth): " cookie_path
    echo
    if [[ -n "$cookie_path" && -f "$cookie_path" ]]; then
        mkdir -p "$(dirname "$COOKIE_DEFAULT")"
        cp -f "$cookie_path" "$COOKIE_DEFAULT"
        log "Cookie copied to $COOKIE_DEFAULT"
    elif [[ -n "$cookie_path" ]]; then
        warn "Cookie path not found: $cookie_path (history download may fail without auth)"
    else
        warn "No cookie provided; unauthenticated downloads will be attempted"
    fi
}

# --- Skip check: true (and record) if output already exists on the host ---
step_done() {
    local file="$1"
    local desc="$2"
    if [[ -f "$file" ]]; then
        log "SKIP: $desc — output already exists: $file"
        SKIPPED_STEPS+=("$desc")
        return 0
    fi
    return 1
}

# --- Run a make-target step inside the pipeline container (entrypoint: make) #
# Each step is retried up to MAX_ATTEMPTS times. Returns the docker exit code. #
run_step() {
    local desc="$1"; shift
    local logfile="/tmp/openpois_${desc// /_}.log"
    local attempt=1
    while true; do
        log "STEP: $desc (attempt $attempt/$MAX_ATTEMPTS) [log: $logfile]"
        if docker compose run --rm --env OPENPOIS_COUNTRY="$COUNTRY" pipeline "$@" COUNTRY="$COUNTRY" 2>&1 | tee "$logfile"; then
            log "DONE: $desc"
            return 0
        fi
        local rc=${PIPESTATUS[0]}
        warn "FAILED: $desc (exit $rc)"
        if [[ $attempt -lt $MAX_ATTEMPTS ]]; then
            attempt=$((attempt + 1))
            log "Retrying $desc in 5s..."
            sleep 5
        else
            warn "$desc failed after $MAX_ATTEMPTS attempts"
            FAILED_STEPS+=("$desc")
            return $rc
        fi
    done
}

# --- Run a raw Python script inside the pipeline container (--entrypoint python) #
run_python_step() {
    local desc="$1"; shift
    local logfile="/tmp/openpois_${desc// /_}.log"
    local attempt=1
    while true; do
        log "STEP: $desc (attempt $attempt/$MAX_ATTEMPTS) [log: $logfile]"
        if OPENPOIS_COUNTRY="$COUNTRY" docker compose run --rm --env OPENPOIS_COUNTRY="$COUNTRY" --entrypoint python pipeline "$@" 2>&1 | tee "$logfile"; then
            log "DONE: $desc"
            return 0
        fi
        local rc=${PIPESTATUS[0]}
        warn "FAILED: $desc (exit $rc)"
        if [[ $attempt -lt $MAX_ATTEMPTS ]]; then
            attempt=$((attempt + 1))
            log "Retrying $desc in 5s..."
            sleep 5
        else
            warn "$desc failed after $MAX_ATTEMPTS attempts"
            FAILED_STEPS+=("$desc")
            return $rc
        fi
    done
}

# --- Prompt the user when any step failed after retries ----------------------
prompt_on_failure() {
    if [[ ${#FAILED_STEPS[@]} -eq 0 ]]; then return 0; fi
    echo
    warn "The following pipeline step(s) failed after retries:"
    for s in "${FAILED_STEPS[@]}"; do
        echo "  - $s"
    done
    echo
    read -rp "One or more pipelines failed. Continue anyway? [y/N] " answer
    echo
    case "$answer" in
        [yY]|[yY][eE][sS]) log "Continuing despite failures..." ;;
        *) warn "Aborting per user request."; exit 1 ;;
    esac
}

###############################################################################
# Main
###############################################################################
main() {
    log "=== OpenPOIs pipeline runner (country=$COUNTRY) ==="
    log "Data root: $COUNTRY_ROOT"

    # 0. Cookie prompt (Geofabrik history is OAuth-protected)
    ensure_cookie

    # 1. Build the pipeline image (cached if unchanged)
    log "Building pipeline image..."
    if ! docker compose build pipeline >/tmp/openpois_build.log 2>&1; then
        warn "docker compose build pipeline failed; see /tmp/openpois_build.log"
        exit 1
    fi
    log "Pipeline image ready."

    # 2. download_history (needs cookie for osm-internal Geofabrik)
    if ! step_done "${COUNTRY_ROOT}/osm_data/${OSM_DATA_VERSION}/osm_versions.parquet" "download_history"; then
        run_step download_history download_history
    fi

    # 3. format_tabular (raw Python, --entrypoint python)
    if ! step_done "${COUNTRY_ROOT}/osm_data/${OSM_DATA_VERSION}/osm_observations.parquet" "format_tabular"; then
        run_python_step format_tabular scripts/osm_data/format_tabular.py
    fi

    # 4. osm_turnover (raw Python)
    if ! step_done "${COUNTRY_ROOT}/osm_turnover_model/${MODEL_OUTPUT_VERSION}/fitted_params.csv" "osm_turnover"; then
        run_python_step osm_turnover scripts/models/osm_turnover.py
    fi

    # 5. snapshot_osm
    if ! step_done "${COUNTRY_ROOT}/snapshots/osm/${OSM_DATA_VERSION}/osm_snapshot.parquet" "snapshot_osm"; then
        run_step snapshot_osm snapshot_osm
    fi

    # 6. snapshot_overture (retry logic lives in src/openpois/io/overture.py)
    if ! step_done "${COUNTRY_ROOT}/snapshots/overture/${OVERTURE_VERSION}/overture_snapshot.parquet" "snapshot_overture"; then
        run_step snapshot_overture snapshot_overture
    fi

    # 7. rate OSM snapshot
    if ! step_done "${COUNTRY_ROOT}/snapshots/osm/${OSM_DATA_VERSION}/osm_snapshot_rated.parquet" "rate"; then
        run_step rate rate
    fi

    # 8. build_ghosts (from OSM history)
    if ! step_done "${COUNTRY_ROOT}/ghost_osm/${GHOST_VERSION}/ghosts.parquet" "build_ghosts"; then
        run_step build_ghosts build_ghosts
    fi

    # 9. conflate_baseline (OSM × Overture matching)
    if ! step_done "${COUNTRY_ROOT}/conflation/${CONFLATION_VERSION}/conflated_baseline.parquet" "conflate_baseline"; then
        run_step conflate_baseline conflate_baseline
    fi

    # 10. apply_cd (change detection / shadow matching)
    if ! step_done "${COUNTRY_ROOT}/conflation/${CONFLATION_VERSION}/conflated_cd.parquet" "apply_cd"; then
        run_step apply_cd apply_cd
    fi

    # 11. calibrate (skipped for AR — calibration disabled in countries/AR.yaml)
    if ! step_done "${COUNTRY_ROOT}/conflation/${CONFLATION_VERSION}/conflated.parquet" "calibrate"; then
        run_step calibrate calibrate
    fi

    # 12. apply_manual_overrides (in-place over conflated.parquet)
    if ! step_done "${COUNTRY_ROOT}/conflation/${CONFLATION_VERSION}/conflated.parquet" "apply_manual_overrides"; then
        run_step apply_manual_overrides apply_manual_overrides
    fi

    prompt_on_failure

    # 13. Build conflated PMTiles
    if ! step_done "${COUNTRY_ROOT}/conflation/${CONFLATION_VERSION}/conflated.pmtiles" "conflated_pmtiles"; then
        run_python_step conflated_pmtiles scripts/conflation/prepare_pmtiles.py
    fi

    # 14. Build OSM PMTiles
    if ! step_done "${COUNTRY_ROOT}/snapshots/osm/${OSM_DATA_VERSION}/osm_snapshot.pmtiles" "osm_pmtiles"; then
        run_python_step osm_pmtiles scripts/osm_snapshot/prepare_pmtiles.py
    fi

    # 15. Build Overture PMTiles
    if ! step_done "${COUNTRY_ROOT}/snapshots/overture/${OVERTURE_VERSION}/overture_snapshot.pmtiles" "overture_pmtiles"; then
        run_python_step overture_pmtiles scripts/overture/prepare_pmtiles.py
    fi

    prompt_on_failure

    # 16. Deploy PMTiles + categories JSON to site-data (absolute container paths)
    log "Deploying PMTiles to site-data..."
    if ! docker compose run --rm --env OPENPOIS_COUNTRY="$COUNTRY" --entrypoint bash pipeline -c "
        set -e
        mkdir -p /home/mambauser/data/openpois/${COUNTRY}/site-data/{osm-pmtiles,conflated-pmtiles,overture-pmtiles}
        cp /home/mambauser/data/openpois/${COUNTRY}/snapshots/osm/${OSM_DATA_VERSION}/osm_snapshot.pmtiles /home/mambauser/data/openpois/${COUNTRY}/site-data/osm-pmtiles/osm.pmtiles
        cp /home/mambauser/data/openpois/${COUNTRY}/conflation/${CONFLATION_VERSION}/conflated.pmtiles /home/mambauser/data/openpois/${COUNTRY}/site-data/conflated-pmtiles/conflated.pmtiles
        cp /home/mambauser/data/openpois/${COUNTRY}/snapshots/overture/${OVERTURE_VERSION}/overture_snapshot.pmtiles /home/mambauser/data/openpois/${COUNTRY}/site-data/overture-pmtiles/overture.pmtiles
        cp /home/mambauser/data/openpois/${COUNTRY}/snapshots/overture/${OVERTURE_VERSION}/overture_categories.json /home/mambauser/data/openpois/${COUNTRY}/site-data/overture-pmtiles/overture_categories.json
        ls -la /home/mambauser/data/openpois/${COUNTRY}/site-data/osm-pmtiles/ /home/mambauser/data/openpois/${COUNTRY}/site-data/conflated-pmtiles/ /home/mambauser/data/openpois/${COUNTRY}/site-data/overture-pmtiles/
    " 2>&1 | tee /tmp/openpois_deploy.log; then
        warn "Deploy failed; see /tmp/openpois_deploy.log"
        FAILED_STEPS+=("deploy_pmtiles")
        prompt_on_failure
        exit 1
    fi
    log "PMTiles deployed to site-data/"

    # 17. Start / restart the site + pmtiles stack
    log "Starting site + pmtiles stack (site:8080, pmtiles:8765)..."
    if ! docker compose up -d >/tmp/openpois_up.log 2>&1; then
        warn "docker compose up -d failed; see /tmp/openpois_up.log"
        FAILED_STEPS+=("docker_compose_up")
        prompt_on_failure
        exit 1
    fi
    log "Stack is up."

    # Summary
    echo
    echo "=============================================="
    echo " OpenPOIs pipeline complete (country=$COUNTRY)"
    echo "=============================================="
    echo " Site:        http://localhost:8080  (zoom to z10+)"
    echo " PMTiles:     http://localhost:8765"
    echo " Data root:   $COUNTRY_ROOT"
    if [[ ${#SKIPPED_STEPS[@]} -gt 0 ]]; then
        echo " Skipped (already done): ${SKIPPED_STEPS[*]}"
    fi
    if [[ ${#FAILED_STEPS[@]} -gt 0 ]]; then
        warn " Failed steps: ${FAILED_STEPS[*]}"
    else
        echo " All steps succeeded."
    fi
    echo "=============================================="
}

main "$@"
