"""
One-off analysis: per-contributing-dataset breakdown of the POIs that actually
enter the conflated shared-label pool.

The conflated parquet only records `source` ∈ {osm, overture, matched}; it drops
Overture's per-dataset `sources[].dataset` provenance at snapshot-build time. So
to attribute pool POIs to Meta / Microsoft / Foursquare / etc. we go back to the
pinned raw Overture release on S3, restrict to the exact overture_ids present in
the pool, unnest the `sources` list, and tally distinct POIs per dataset.

A single POI can carry several contributing datasets, so the per-dataset shares
sum to >100%. The `Overture` pseudo-dataset (a property/confidence override the
foundation adds to nearly every record) is reported but flagged separately.

Outputs land under the configured ``logs`` directory as a CSV + printed summary.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from openpois.config import load_config
from openpois.io.boundary import get_boundary
from openpois.io.overture import (
    _build_bbox_predicate,
    _build_taxonomy_predicate,
    _list_overture_part_keys,
)

# --- Inputs (resolved from config.yaml + the active country profile) ----------
config = load_config()

RELEASE_DATE = config.get("download", "overture", "release_date")
BUCKET = config.get("download", "overture", "s3_bucket")
S3_REGION = config.get("download", "overture", "s3_region")
CONFLATED = config.get_file_path("conflation", "conflated")
TAXONOMY_ALLOWLIST = config.get("download", "overture", "taxonomy_allowlist")
BOUNDARY_CFG = config.get("download", "general", "boundary")

OUT_DIR = config.get_dir_path("logs")
POOL_IDS = OUT_DIR / "pool_overture_ids.parquet"
PAIRS = OUT_DIR / "pool_source_pairs.parquet"
RESULT_CSV = OUT_DIR / "pool_source_breakdown.csv"


def _session(conn: duckdb.DuckDBPyConnection, temp_dir: Path) -> None:
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute(f"SET s3_region = '{S3_REGION}';")
    conn.execute("SET memory_limit = '6GB';")
    conn.execute("SET threads TO 4;")
    conn.execute(f"SET temp_directory = '{temp_dir.as_posix()}';")
    conn.execute("SET preserve_insertion_order = false;")
    conn.execute("SET enable_external_file_cache = false;")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    temp_dir = OUT_DIR / "_duckdb_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    bbox_pred = _build_bbox_predicate(get_boundary(
        source_url = BOUNDARY_CFG["source_url"],
        cache_dir = config.get_dir_path("boundary"),
        coastline_buffer_m = BOUNDARY_CFG.get("coastline_buffer_m", 100),
        cache_name = BOUNDARY_CFG.get("cache_name"),
        zip_name = BOUNDARY_CFG.get("zip_name"),
        shp_name = BOUNDARY_CFG.get("shp_name"),
        filter_field = BOUNDARY_CFG.get("filter_field"),
        filter_values = BOUNDARY_CFG.get("filter_values"),
    )[1])
    tax_pred = _build_taxonomy_predicate(TAXONOMY_ALLOWLIST)

    # 1. Export the pool's overture_ids (the exact set of Overture-derived POIs
    #    that survived conflation -> shared-label pool).
    print("Step 1: exporting pool overture_ids ...", flush=True)
    con = duckdb.connect()
    _session(con, temp_dir)
    con.execute(
        f"""
        COPY (
            SELECT DISTINCT overture_id
            FROM read_parquet('{CONFLATED.as_posix()}')
            WHERE overture_id IS NOT NULL
        ) TO '{POOL_IDS.as_posix()}' (FORMAT parquet)
        """
    )
    n_pool = con.execute(
        f"SELECT count(*) FROM read_parquet('{POOL_IDS.as_posix()}')"
    ).fetchone()[0]
    con.close()
    print(f"  pool overture POIs: {n_pool:,}", flush=True)

    # 2. Per-part remote scan: distinct (id, dataset) pairs limited to pool ids.
    part_keys = _list_overture_part_keys(release_date=RELEASE_DATE, bucket=BUCKET)
    print(f"Step 2: scanning {len(part_keys)} Overture parts ...", flush=True)

    if PAIRS.exists():
        PAIRS.unlink()
    parts_dir = OUT_DIR / "_pairs_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    for i, key in enumerate(part_keys):
        out = parts_dir / f"pairs_{i:05d}.parquet"
        if out.exists() and out.stat().st_size > 0:
            print(f"  [{i+1}/{len(part_keys)}] {out.name} cached; skip", flush=True)
            continue
        uri = f"s3://{BUCKET}/{key}"
        con = duckdb.connect()
        _session(con, temp_dir)
        con.execute(
            f"""
            COPY (
                SELECT DISTINCT f.overture_id, src.dataset AS dataset
                FROM (
                    SELECT p.id AS overture_id, p.sources AS sources
                    FROM read_parquet('{uri}') p
                    JOIN read_parquet('{POOL_IDS.as_posix()}') ids
                        ON p.id = ids.overture_id
                    WHERE {bbox_pred} AND {tax_pred}
                ) f,
                UNNEST(f.sources) AS t(src)
            ) TO '{out.as_posix()}' (FORMAT parquet)
            """
        )
        con.close()
        print(f"  [{i+1}/{len(part_keys)}] wrote {out.name}", flush=True)

    # 3. Aggregate. Each id lives in exactly one part, so distinct (id,dataset)
    #    across parts needs no further dedup, but we count distinct id defensively.
    print("Step 3: aggregating ...", flush=True)
    con = duckdb.connect()
    _session(con, temp_dir)
    glob = (parts_dir / "pairs_*.parquet").as_posix()
    rows = con.execute(
        f"""
        SELECT dataset,
               count(DISTINCT overture_id) AS n_pois,
               100.0 * count(DISTINCT overture_id) / {n_pool} AS pct_of_pool
        FROM read_parquet('{glob}')
        GROUP BY dataset
        ORDER BY n_pois DESC
        """
    ).fetchall()
    con.execute(
        f"""
        COPY (
            SELECT dataset,
                   count(DISTINCT overture_id) AS n_pois,
                   100.0 * count(DISTINCT overture_id) / {n_pool} AS pct_of_pool
            FROM read_parquet('{glob}')
            GROUP BY dataset
            ORDER BY n_pois DESC
        ) TO '{RESULT_CSV.as_posix()}' (FORMAT csv, HEADER)
        """
    )
    con.close()

    print(f"\nPool Overture POIs: {n_pool:,}")
    print("Per-contributing-dataset breakdown (sums >100%; multi-source POIs):")
    print(f"{'dataset':<22}{'n_pois':>14}{'pct_of_pool':>14}")
    for ds, n, pct in rows:
        print(f"{ds:<22}{n:>14,}{pct:>13.2f}%")
    print(f"\nCSV: {RESULT_CSV}")


if __name__ == "__main__":
    main()
