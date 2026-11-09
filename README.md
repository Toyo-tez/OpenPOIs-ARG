# ¡DISCLAIMER!
This fork is vibe slop coded

# OpenPOIs

A unified, confidence-scored open dataset of points of interest, configurable
by country. This fork defaults to Argentina (`country: "AR"`); the original
U.S. coverage — the 50 states, DC, and the 5 inhabited U.S. territories
(Puerto Rico, US Virgin Islands, Guam, Northern Mariana Islands, American
Samoa) — remains available through the `US` profile. Built from
[OpenStreetMap](https://www.openstreetmap.org) and
[Overture Maps](https://overturemaps.org).

![OpenPOIs interactive map](docs/_static/hero.png)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Data: ODbL](https://img.shields.io/badge/Data-ODbL%20v1.0-orange.svg)](https://opendatacommons.org/licenses/odbl/1-0/)
[![Python](https://img.shields.io/badge/python-3.10%E2%80%933.14-blue)](pyproject.toml)
[![Site deploy](https://github.com/henryspatialanalysis/openpois/actions/workflows/deploy-site.yml/badge.svg)](https://github.com/henryspatialanalysis/openpois/actions/workflows/deploy-site.yml)

- 🌐 **Live map:** <https://openpois.org>
- 📘 **Python API docs:** <https://openpois.org/docs/>
- 🗄️ **Dataset on Source Cooperative:** <https://source.coop/henryspatialanalysis/openpois>


## What is OpenPOIs?

OpenPOIs conflates points of interest from OpenStreetMap and Overture Maps
into a single unified dataset, then attaches a per-POI confidence score
estimating the probability that the place still exists. Confidence comes from
a Bayesian turnover model fit on OSM tag-edit history. The published dataset
covers the United States and its 5 inhabited territories, and is refreshed
monthly, following the Overture Maps monthly release cycle.

This repository contains the Python library used to produce the data, the
end-to-end pipelines that download and conflate sources, and the Vue
front-end that powers the live map.


## Quickstart — read the data

No install required. The dataset is hosted anonymously on Source Cooperative;
read it straight from S3:

```python
import pyarrow.dataset as ds
import pyarrow.fs as pafs

BASE = "us-west-2.opendata.source.coop/henryspatialanalysis/openpois"
VERSION = "latest"   # or pin a dated folder, e.g. "2026-04-23-v0"

fs = pafs.S3FileSystem(anonymous = True, region = "us-west-2")
pois = ds.dataset(
    f"{BASE}/{VERSION}/conflated-parquet/",
    filesystem = fs,
    format = "parquet",
    partitioning = "hive",
)
print(pois.schema)
print(f"{pois.count_rows():,} POIs")
```

GeoPandas, DuckDB, and PMTiles examples live in the
[dataset README on Source Cooperative](https://source.coop/henryspatialanalysis/openpois).


## Python package

The full OpenPOIs package API — I/O adapters, the turnover model, conflation
primitives — is documented at <https://openpois.org/docs/>.

### Installation

This package can be installed from source:

```bash
git clone https://github.com/henryspatialanalysis/openpois.git
cd openpois
make build_env          # conda env from environment.yml
conda activate openpois
make install_package    # pip install -e .
```

`environment.yml` is a full, pinned `conda env export` for linux-64, so the
conda path is deterministic on Linux but does not solve on macOS/ARM. For a
host-independent environment use the Docker images described in
[docker/README.md](docker/README.md): they pin GDAL/PROJ, osmium, tippecanoe,
JAX and Node, and only require a mounted data directory.

### Repository layout

| Path | Purpose |
|---|---|
| [src/openpois/](src/openpois/) | Library source: I/O, models, conflation, publishing |
| [scripts/](scripts/) | End-to-end pipelines using `config.yaml` |
| [site/](site/) | Vue 3 + Vite frontend powering openpois.org |
| [docker/](docker/) | Reproducible pipeline + site images (see [docker/README.md](docker/README.md)) |
| [docs/](docs/) | Sphinx documentation source |
| [tests/](tests/) | Unit tests |

### Country selection

This fork is configurable by country. The base [config.yaml](config.yaml)
holds shared pipeline settings. Select a country with its **ISO 3166-1
alpha-2 code**:

```yaml
# config.yaml
country: "AR"     # or "US", "GB", "DE", ...
```

That single value is enough. For every ISO code with a Geofabrik extract, the
loader synthesizes a complete profile from the bundled
[`src/openpois/data/countries.csv`](src/openpois/data/countries.csv) index:

- OSM snapshot, full-history and replication URLs from Geofabrik;
- the Natural Earth admin-0 boundary and its ISO filter;
- `paths.data_root: ~/data/openpois/<ISO2>`;
- non-US indicator, model-term and calibration defaults;
- map centre/extent and Source Cooperative namespace defaults.

No OSM PBF, boundary file or Overture file has to be supplied manually.
`countries/<ISO2>.yaml` remains available as an **optional override** for
country-specific tweaks (the US and Argentina profiles pin dated releases and
the US Census boundary, for example).

Selection precedence is:

1. `OPENPOIS_COUNTRY` (also set by `make COUNTRY=...`)
2. the top-level `country` key in `config.yaml`

For example:

```bash
OPENPOIS_COUNTRY=US python scripts/osm_snapshot/download.py
make snapshots COUNTRY=AR    # download OSM + Overture snapshots for Argentina
make conflate COUNTRY=US
make site_build COUNTRY=US
```

Everything country-specific is read through `openpois.config.load_config()`:
the data root (`paths.data_root`), OSM extracts
(`download.osm.extracts[].snapshot_url` / `history_url`), the reference
boundary, Source Cooperative bucket/repo, and front-end defaults. Data
directories under `~/data/openpois` are rebased onto the profile's
`data_root`, so the US profile (`~/data/openpois`) and the Argentina profile
(`~/data/openpois/AR`) can share one checkout.

To use a country, set its ISO code and run the pipeline — for example
`make snapshots COUNTRY=GB`. The auto-generated defaults already disable the
US Census indicators, the `msa` / `amenity_msa` / `urbanicity` model terms and
Bayesian calibration. If you need a pinned boundary, a specific Geofabrik
extract, dated `versions.*` folders, or a translated site, add
`countries/<ISO2>.yaml`; copy [countries/AR.yaml](countries/AR.yaml) as a
starting point.

A few Geofabrik extracts bundle several countries (for example
`haiti-and-domrep` and `israel-and-palestine`). The auto profile uses the
bundled extract as-is, so its OSM snapshot spans the whole extract; supply a
profile with a national extract, or clip downstream, if you need a strict
single-country OSM subset.

Regenerate the bundled index after Geofabrik adds or renames an extract with:

```bash
python scripts/build_country_index.py
```

The front end reads generated values from `site/src/country.generated.js`:

```bash
make site_config COUNTRY=AR   # regenerate only the country module
```

`make site_dev`, `make site_build`, and `make site_preview` run this
automatically.

Several settings are overridable by environment variable without editing a
profile:

- `OPENPOIS_CONFIG` — use a different `config.yaml`.
- `OPENPOIS_CREDENTIALS_FILE` — Source Cooperative credential JSON (defaults
  to `publish.credentials_file`).
- `OPENPOIS_CALIBRATION_DIR` — validation input directory (defaults to the
  repo's gitignored `data/calibration`).
- `OPENPOIS_SOURCE_COOP_ACCOUNT` — Source Cooperative account for both the
  upload bucket and the public PMTiles base URL.

### Reproduce the dataset yourself

The data is produced by four pipelines under [scripts/](scripts/), each
driven by [config.yaml](config.yaml):

1. Snapshot downloads (OSM + Overture)
2. OSM history download and Bayesian turnover-model fit
3. Apply model to OSM snapshot to get per-POI confidence
4. Conflate OSM × Overture, partition, publish to Source Cooperative

Each pipeline and its scripts are documented in the workflows reference at
<https://openpois.org/docs/workflows.html>.

### Web map

The interactive map at <https://openpois.org> is a Vue 3 + Vite app rendering
PMTiles archives over MapLibre GL. To run it locally:

```bash
make site_dev      # http://localhost:5173, hot reload
make site_build    # production build to site/dist/
```

The first run installs the frontend dependencies in `site/` automatically
(equivalent to `cd site && npm install`). Both targets also regenerate the
gitignored `site/src/taxonomy.generated.js`, `site/public/taxonomy.html`, and
`site/src/country.generated.js`. The frontend needs Node.js 20+; run
`npm install` from `site/`, not the repository root.

To preview locally built PMTiles instead of the published Source Cooperative
copy, stage the archives in the same layout and serve them with the bundled
range server, then point the site at it:

```bash
python scripts/serve_pmtiles.py --root ~/data/openpois/AR/site-data --port 8765
OPENPOIS_PMTILES_BASE_URL=http://localhost:8765 make site_dev
```

The server is needed because PMTiles uses HTTP Range requests; `python -m
http.server` ignores them.

The site auto-deploys to GitHub Pages via
[.github/workflows/deploy-site.yml](.github/workflows/deploy-site.yml) on
every push to `main` that touches `site/`, `src/`, `docs/`, or `scripts/`.

### Development

```bash
pytest               # run the test suite
make lint            # flake8 + pylint
make export_env      # rewrite environment.yml after adding deps
```

## Licensing

OpenPOIs is dual-licensed:

- **Code** — [MIT License](LICENSE). You can use, modify, and redistribute the
  Python package, scripts, and front-end freely.
- **Data** — [Open Database License (ODbL) v1.0](https://opendatacommons.org/licenses/odbl/1-0/).
  The published parquet and PMTiles archives are derivative works of
  OpenStreetMap and Overture Maps and inherit ODbL terms. Any public use must
  attribute OpenPOIs, [OpenStreetMap contributors](https://www.openstreetmap.org/copyright),
  and the [Overture Maps Foundation](https://docs.overturemaps.org/attribution/).
  Derivative databases must be released under the same license.

## Citation

If you use OpenPOIs in research, please cite:

> Henry, N. (2026). *OpenPOIs: a unified, confidence-scored dataset of U.S. points of interest.* Henry Spatial Analysis. <https://openpois.org>

A machine-readable citation is provided in [CITATION.cff](CITATION.cff);
GitHub renders it as a "Cite this repository" button on the repo home page.

## Contact

Bug reports, feature requests, and contributions are welcome via
[GitHub issues](https://github.com/henryspatialanalysis/openpois/issues).
