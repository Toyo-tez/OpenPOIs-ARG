#   -------------------------------------------------------------
#   Copyright (c) Henry Spatial Analysis. All rights reserved.
#   Licensed under the MIT License. See LICENSE in project root for information.
#   -------------------------------------------------------------

"""Build OSM snapshot / history extract specs from the active config.

The pipeline can cover any number of Geofabrik extracts.  ``config.yaml`` (or
the selected country profile) declares them once::

    download:
      osm:
        extracts:
          - name: argentina
            snapshot_url: https://download.geofabrik.de/<region>/<country>-latest.osm.pbf
            history_url: https://osm-internal.download.geofabrik.de/<path>
            replication_url: https://download.geofabrik.de/<region>/<country>-updates/

File names are derived by :mod:`openpois.config` and resolved through the
normal ``directories`` blocks, so callers do not need to know whether an
extract is the first (unsuffixed) one or a later (name-prefixed) one.
"""

from __future__ import annotations

from config_versioned import Config

from openpois.config import extract_file_keys, os_extracts
from openpois.io.osm_history_pbf import HistoryExtract
from openpois.io.osm_snapshot import SnapshotExtract


def _extract_url(extract: dict, *keys: str) -> str:
    for key in keys:
        value = extract.get(key)
        if value:
            return str(value)
    raise KeyError(
        f"Extract {extract.get('name')!r} is missing one of {keys}"
    )


def snapshot_extracts(config: Config) -> list[SnapshotExtract]:
    """Return ``SnapshotExtract`` specs for every configured OSM extract."""
    specs: list[SnapshotExtract] = []
    for index, extract in enumerate(os_extracts(config)):
        name = str(extract["name"])
        keys = extract_file_keys(name, index)
        specs.append(SnapshotExtract(
            name=name,
            url=_extract_url(extract, "snapshot_url", "pbf_url"),
            raw_pbf_path=config.get_file_path("snapshot_osm", keys["raw_pbf"]),
            filtered_pbf_path=config.get_file_path(
                "snapshot_osm", keys["filtered_pbf"]
            ),
        ))
    return specs


def residential_extracts(config: Config) -> list[SnapshotExtract]:
    """Like :func:`snapshot_extracts`, but with residential-filtered PBF paths."""
    specs: list[SnapshotExtract] = []
    for index, extract in enumerate(os_extracts(config)):
        name = str(extract["name"])
        keys = extract_file_keys(name, index)
        specs.append(SnapshotExtract(
            name=name,
            url=_extract_url(extract, "snapshot_url", "pbf_url"),
            raw_pbf_path=config.get_file_path("snapshot_osm", keys["raw_pbf"]),
            filtered_pbf_path=config.get_file_path(
                "snapshot_osm", keys["residential_filtered_pbf"]
            ),
        ))
    return specs


def history_extracts(config: Config) -> list[HistoryExtract]:
    """Return ``HistoryExtract`` specs for every configured OSM extract."""
    specs: list[HistoryExtract] = []
    for index, extract in enumerate(os_extracts(config)):
        name = str(extract["name"])
        keys = extract_file_keys(name, index)
        specs.append(HistoryExtract(
            name=name,
            url=_extract_url(extract, "history_url", "history_pbf_url"),
            raw_pbf_path=config.get_file_path(
                "osm_data", keys["raw_history_pbf"]
            ),
            filtered_pbf_path=config.get_file_path(
                "osm_data", keys["filtered_history_pbf"]
            ),
            time_filtered_pbf_path=config.get_file_path(
                "osm_data", keys["time_filtered_history_pbf"]
            ),
            versions_path=config.get_file_path("osm_data", keys["versions"]),
            changes_path=config.get_file_path("osm_data", keys["changes"]),
        ))
    return specs


def replication_urls(config: Config) -> dict[str, str]:
    """Return the Geofabrik diff feed URL for each configured extract."""
    incremental = (
        config.get("download", "osm", "incremental_history",
                   fail_if_none=False) or {}
    )
    configured = incremental.get("replication_urls") or {}
    result: dict[str, str] = {}
    for extract in os_extracts(config):
        name = str(extract["name"])
        url = extract.get("replication_url") or configured.get(name)
        if url:
            result[name] = str(url)
    return result or {str(k): str(v) for k, v in configured.items()}
