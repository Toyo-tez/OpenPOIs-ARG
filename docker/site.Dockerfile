# syntax=docker/dockerfile:1.7
#
# OpenPOIs static frontend image.
#
# Multi-stage: Node + a tiny Python venv build `site/dist`, then nginx serves
# the result. Country-specific values (map centre, PMTiles URLs) are baked in
# at build time from `config.yaml` + the selected country profile.
#
# Build (defaults to Argentina and a local PMTiles server):
#   docker build -f docker/site.Dockerfile -t openpois-site .
#
# Or point at a published Source Cooperative release by leaving the base URL
# empty and setting the country:
#   docker build -f docker/site.Dockerfile \
#     --build-arg OPENPOIS_COUNTRY=US \
#     --build-arg OPENPOIS_PMTILES_BASE_URL= \
#     -t openpois-site .
#
# Run:
#   docker run --rm -p 8080:80 openpois-site   # http://localhost:8080
#
FROM node:22-bookworm-slim AS build

# Python is only needed at build time for the taxonomy/country generators.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Frontend dependencies are cached until the lockfile changes.
COPY site/package.json site/package-lock.json ./site/
RUN cd site && npm ci

# The generators need pandas + PyYAML + config-versioned (same minimal set as
# .github/workflows/deploy-site.yml).
RUN pip install --no-cache-dir --disable-pip-version-check \
      pandas pyyaml "config-versioned==0.2.0"

COPY . .

ARG OPENPOIS_COUNTRY=AR
# Empty => use publish.public_base_url + versions.source_coop from the config.
ARG OPENPOIS_PMTILES_BASE_URL=http://localhost:8765
ARG VITE_STADIA_API_KEY=
ENV OPENPOIS_COUNTRY=$OPENPOIS_COUNTRY \
    OPENPOIS_PMTILES_BASE_URL=$OPENPOIS_PMTILES_BASE_URL \
    VITE_STADIA_API_KEY=$VITE_STADIA_API_KEY

# Regenerate the gitignored taxonomy + country modules, then build.
RUN PYTHONPATH=src python scripts/build_taxonomy.py \
 && PYTHONPATH=src python scripts/build_site_config.py \
 && cd site && npm run build

FROM nginx:1.27-alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/site/dist /usr/share/nginx/html

EXPOSE 80
