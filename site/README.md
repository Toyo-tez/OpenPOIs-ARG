# Vue 3 + Vite

This template should help get you started developing with Vue 3 in Vite. The template uses Vue 3 `<script setup>` SFCs, check out the [script setup docs](https://v3.vuejs.org/api/sfc-script-setup.html#sfc-script-setup) to learn more.

Learn more about IDE Support for Vue in the [Vue Docs Scaling up Guide](https://vuejs.org/guide/scaling-up/tooling.html#ide-support).

## Maintenance

### Taxonomy

The OSM / Overture / conflated filters derive from arrays in
`src/taxonomy.generated.js`, which is regenerated from
`src/openpois/conflation/data/*.csv` by `scripts/build_taxonomy.py`. The file
is gitignored. Use the `sync-taxonomy` skill (or run `build_taxonomy.py`
followed by `check_taxonomy_sync.py`) whenever the CSVs change. Only the
display-label maps in `src/constants.js` are hand-maintained.

### PMTiles URLs

Public data is hosted on Source Cooperative. The URLs are generated into
`src/country.generated.js` by `scripts/build_site_config.py` from
`config.yaml` + the selected country profile:

- base: `publish.public_base_url`
- version folder: `versions.source_coop`
- Overture release: `download.overture.release_date`

Bump those config values for a new release; `make site_config`,
`make site_dev`, and `make site_build` regenerate the module automatically.
