# Reproducible runs with Docker

The repository uses two images on purpose:

| Image | Contents | Why separate |
|---|---|---|
| `openpois-pipeline` | Pinned conda env (GDAL/PROJ, osmium, tippecanoe, JAX/BlackJAX, DuckDB), pipeline code, `make` | Large (~8 GB) and only needed for data work |
| `openpois-site` | Node build stage + nginx serving `site/dist` | Small runtime, no Python/conda |

Splitting them keeps the frontend image quick to pull and avoids shipping the
scientific stack to a web server.

## Prerequisites

- Docker with the Compose plugin (`docker compose version`).
- Disk: roughly **10 GB free** to build the pipeline image, **1 GB** for the
  site image. The pipeline image itself is large because of GDAL + JAX.
- A host data directory, by default `~/data/openpois`. It is mounted at
  `/home/mambauser/data/openpois`, which matches the `~/data/openpois` paths in
  `config.yaml`, so no path edits are needed.

## Pipeline image

Build once:

```bash
docker build -f docker/pipeline.Dockerfile -t openpois-pipeline .
```

Run any `make` target exactly as you would locally. The image entrypoint is
`make`, so arguments are passed straight through:

```bash
docker run --rm \
  -v "$HOME/data/openpois:/home/mambauser/data/openpois" \
  openpois-pipeline test

docker run --rm \
  -v "$HOME/data/openpois:/home/mambauser/data/openpois" \
  openpois-pipeline snapshot_osm COUNTRY=AR
```

Get a shell (useful for debugging PATH/PROJ issues):

```bash
docker run --rm -it --entrypoint bash \
  -v "$HOME/data/openpois:/home/mambauser/data/openpois" \
  openpois-pipeline
```

Or use Compose, which keeps the volume wiring in one place:

```bash
docker compose run --rm pipeline snapshot_osm COUNTRY=AR
```

`OPENPOIS_COUNTRY` selects the profile; setting only an ISO code is enough
because missing country profiles are synthesized (see the main README).

## Credentials

Full OSM history (`*-internal.osh.pbf`) requires a Geofabrik OAuth cookie. The
config expects it at `~/data/openpois/.creds/geofabrik_cookies.txt`, so if the
data root is mounted, the cookie is available inside the container with no
extra flags:

```bash
mkdir -p ~/data/openpois/.creds
chmod 700 ~/data/openpois/.creds
# Export from a browser logged in at osm-internal.download.geofabrik.de,
# or use Geofabrik's oauth_cookie_client.py, then:
cp /path/to/cookies.txt ~/data/openpois/.creds/geofabrik_cookies.txt
chmod 600 ~/data/openpois/.creds/geofabrik_cookies.txt
```

Source Cooperative upload credentials are only needed for publishing; mount
the credentials file referenced by `publish.credentials_file` if you upload.

## Site + PMTiles stack

```bash
docker compose up --build
# site:    http://localhost:8080
# PMTiles: http://localhost:8765
```

The `pmtiles` service serves the archives with Range + CORS. It expects this
layout under `$OPENPOIS_DATA_DIR/<COUNTRY>/site-data/`:

```
osm-pmtiles/osm.pmtiles
overture-pmtiles/overture.pmtiles
overture-pmtiles/overture_categories.json
conflated-pmtiles/conflated.pmtiles
```

Build those archives first (`scripts/*/prepare_pmtiles.py`) or copy an
existing release into that layout.

### Build arguments

`docker/site.Dockerfile` bakes the country and PMTiles URL at build time:

| Build arg | Default | Meaning |
|---|---|---|
| `OPENPOIS_COUNTRY` | `AR` | Country profile used for centre, bbox, title |
| `OPENPOIS_PMTILES_BASE_URL` | `http://localhost:8765` | Where the browser fetches archives |
| `VITE_STADIA_API_KEY` | empty | Geocoder key (optional) |

To serve published data instead of local archives:

```bash
docker build -f docker/site.Dockerfile \
  --build-arg OPENPOIS_COUNTRY=US \
  --build-arg OPENPOIS_PMTILES_BASE_URL= \
  -t openpois-site .
```

An empty base URL falls back to `publish.public_base_url` + `versions.source_coop`
from the config.

The URL is resolved **by the browser**, not by the container. `localhost` works
when the site is opened on the same machine as the published `:8765` port; use
the host's address if you access it remotely.

## UID / GID

The upstream micromamba base image creates `mambauser` with UID/GID `57439`,
which usually does **not** match a desktop account. Run the pipeline with your
host UID/GID so files written into the mounted data directory belong to you:

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/home/mambauser \
  -v "$HOME/data/openpois:/home/mambauser/data/openpois" \
  openpois-pipeline snapshot_osm COUNTRY=AR
```

Docker assigns `HOME=/` when the `--user` UID has no `/etc/passwd` entry, so
pass `HOME=/home/mambauser` (the image home is world-writable) to keep tools
that cache under `~` working. The JAX compilation cache also falls back to
`/tmp` automatically when the home cache is not writable.

Compose already does both and defaults to `1000:1000`; override when needed:

```bash
OPENPOIS_UID=$(id -u) OPENPOIS_GID=$(id -g) docker compose up --build
```

`/opt/openpois` and `/home/mambauser` are world-writable inside the image
(`chmod a+rwX`) so pytest and bytecode caches work under any UID. The site
image serves static files via nginx and does not need a UID override.

## What Docker does and does not pin

- **Pinned:** Python and system libraries (`environment.yml`), Node and npm
  dependencies (`site/package-lock.json`), and the Dockerfile layers.
- **Not pinned:** upstream data. Geofabrik extracts, Overture releases and
  Natural Earth move over time. For reproducible *outputs*, pin the snapshot
  dates and versions in `config.yaml` / `countries/<ISO>.yaml`
  (`download.osm.end_date`, `versions.*`) instead of using `latest`.
- Docker does not remove the need for the Geofabrik cookie or for enough
  disk/RAM. Conflation peaks around 2–3 GB RSS for a country the size of
  Argentina; the history PBF and intermediates need several GB of scratch.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker build` fails solving conda packages | The image is linux-64 only; build on an x86-64 Linux host or use `--platform linux/amd64` |
| `PROJ: proj_create_from_database` errors | The image sets `PROJ_DATA`/`GDAL_DATA`; when debugging, run inside the image with `micromamba run -n openpois` or enter via `--entrypoint bash` |
| Geofabrik returns `403` | Cookie missing/expired; refresh `geofabrik_cookies.txt` |
| Site shows blank map layers | `site-data/` is missing or the `pmtiles` service is not running; check `curl -I http://localhost:8765/osm-pmtiles/osm.pmtiles` |
| Permission denied writing data | UID mismatch; see the `--user` note above |
