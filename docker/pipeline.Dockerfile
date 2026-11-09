# syntax=docker/dockerfile:1.7
#
# OpenPOIs data pipeline image.
#
# Ships the pinned conda environment (GDAL/PROJ, osmium, tippecanoe,
# JAX/BlackJAX, DuckDB) plus the pipeline code. It intentionally contains no
# data and no credentials: mount a host data root at
# /home/mambauser/data/openpois at run time.
#
# Build:
#   docker build -f docker/pipeline.Dockerfile -t openpois-pipeline .
#
# Run (mirrors `make <target> COUNTRY=<code>`). --user keeps outputs on the
# mounted volume owned by you rather than the image's UID. Docker sets HOME=/ for
# arbitrary UIDs, so point HOME at the image's writable home:
#   docker run --rm --user "$(id -u):$(id -g)" -e HOME=/home/mambauser \
#     -v "$HOME/data/openpois:/home/mambauser/data/openpois" \
#     openpois-pipeline snapshot_osm COUNTRY=AR
#
# Open a shell:
#   docker run --rm -it --entrypoint bash --user "$(id -u):$(id -g)" \
#     -e HOME=/home/mambauser \
#     -v "$HOME/data/openpois:/home/mambauser/data/openpois" \
#     openpois-pipeline
#
FROM mambaorg/micromamba:2.0.5

# System tools: git (version metadata), make (the public interface), and the
# usual download/certificate helpers. conda supplies GDAL, PROJ, osmium and
# tippecanoe; nothing else is required on the host.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git make \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/openpois
SHELL ["/bin/bash", "-c"]

# Install the pinned environment first so dependency changes don't invalidate
# the code layers. environment.yml is a full `conda env export` for linux-64.
COPY --chown=${MAMBA_USER}:${MAMBA_USER} environment.yml ./

# Adjust the exported pip pins for a clean, compiler-free install:
#   * drop the editable self-install (`openpois`) — installed from source below;
#   * drop `sga-walkability` and `versioned-config`, which are not on PyPI
#     (scripts/remote/openpois-remote.sh applies the same filter);
#   * drop `pyiceberg` and `pyroaring`, which the pipeline does not import;
#   * bump `duckdb` from 1.4.1 to 1.4.3: 1.4.1 predates cp314 wheels, and the
#     patch release is the smallest version that installs without compiling
#     DuckDB from source.
# Also hand the workdir to the runtime user so micromamba can write its
# temporary pip requirements file there.
RUN sed -Ei \
      -e '/^[[:space:]]*-[[:space:]]*(openpois|sga-walkability|versioned-config|pyiceberg|pyroaring)==/d' \
      -e 's/duckdb==1\.4\.1/duckdb==1.4.3/' \
      environment.yml \
 && chown -R ${MAMBA_USER}:${MAMBA_USER} /opt/openpois

USER ${MAMBA_USER}
RUN micromamba create -y -n openpois -f environment.yml \
 && micromamba clean --all --yes \
 && rm -rf /home/mambauser/.cache/pip

# The package itself is installed from this checkout after the environment so
# that editing the code only rebuilds the (fast) install layer.
COPY --chown=${MAMBA_USER}:${MAMBA_USER} pyproject.toml README.md ./
COPY --chown=${MAMBA_USER}:${MAMBA_USER} src ./src
RUN /opt/conda/envs/openpois/bin/python -m pip install \
      --no-deps --no-build-isolation -e . \
 && rm -rf /home/mambauser/.cache/pip

# Pipeline code, config and tests.
COPY --chown=${MAMBA_USER}:${MAMBA_USER} Makefile config.yaml ./
COPY --chown=${MAMBA_USER}:${MAMBA_USER} countries ./countries
COPY --chown=${MAMBA_USER}:${MAMBA_USER} scripts ./scripts
COPY --chown=${MAMBA_USER}:${MAMBA_USER} tests ./tests

# Allow running as an arbitrary host UID (recommended, so outputs are owned by
# you on the host) to write pytest/bytecode caches and HOME-relative files.
# Docker forces HOME=/ for UIDs missing from /etc/passwd, so also make the
# image's home writable in case runtimes pass it as HOME.
RUN chmod -R a+rwX /opt/openpois /home/mambauser

# Make the conda env the default interpreter for `make` and ad-hoc shells.
# OpenPOIS_PYTHON is honoured first by the Makefile's interpreter probe.
# Conda's libstdc++ (6.0.34) provides CXXABI_1.3.15 which the host Debian 12
# system libstdc++ (6.0.30) lacks; prepend it so scipy wheels load.
ENV PATH=/opt/conda/envs/openpois/bin:$PATH \
    OPENPOIS_PYTHON=/opt/conda/envs/openpois/bin/python \
    PYTHONPATH=/opt/openpois/src \
    PROJ_DATA=/opt/conda/envs/openpois/share/proj \
    GDAL_DATA=/opt/conda/envs/openpois/share/gdal \
    LD_LIBRARY_PATH=/opt/conda/envs/openpois/lib:$LD_LIBRARY_PATH \
    PYTHONUNBUFFERED=1

# config.yaml resolves ~/data/openpois against /home/mambauser. Mount the host
# data root there; no VOLUME directive so an unmounted run fails loudly instead
# of writing to an anonymous volume.

# `docker run <image> <target> [VAR=VALUE ...]` == `make <target> ...`
ENTRYPOINT ["make"]
CMD ["test"]
