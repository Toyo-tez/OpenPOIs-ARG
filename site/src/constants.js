// Taxonomy arrays are generated from the conflation CSVs by
// scripts/build_taxonomy.py. Only the display-label maps below are
// hand-maintained. Run `python scripts/check_taxonomy_sync.py` to detect drift.
import {
  SHARED_LABELS,
  OSM_KEYS,
  OVERTURE_L0S,
} from './taxonomy.generated.js'
import {
  COUNTRY,
  COUNTRY_CODE,
  COUNTRY_NAME,
  SITE_TITLE,
  SITE_LOCALE,
  SITE_TIMEZONE,
  INITIAL_CENTER,
  INITIAL_ZOOM,
  MAP_BBOX,
  GEOCODER_COUNTRY,
  OSM_PMTILES_URL,
  CONFLATED_PMTILES_URL,
  OVERTURE_PMTILES_URL,
  OVERTURE_CATEGORIES_URL,
} from './country.generated.js'

// Country-specific values are generated from config.yaml + countries/<ISO>.yaml
// by scripts/build_site_config.py. Re-export them so components keep importing
// from this one module.
export {
  COUNTRY,
  COUNTRY_CODE,
  COUNTRY_NAME,
  SITE_TITLE,
  SITE_LOCALE,
  SITE_TIMEZONE,
  INITIAL_CENTER,
  INITIAL_ZOOM,
  MAP_BBOX,
  GEOCODER_COUNTRY,
  OSM_PMTILES_URL,
  CONFLATED_PMTILES_URL,
  OVERTURE_PMTILES_URL,
  OVERTURE_CATEGORIES_URL,
}

// Confidence color ramp (conf_mean 0-1, 1 = stable)
export const COLORS = {
  cluster: '#6366f1',  // indigo for clusters
  geolocation: '#60a5fa', // light blue dot
}

const OSM_KEY_LABELS = {
  amenity: 'Amenity',
  shop: 'Shop',
  leisure: 'Leisure',
  healthcare: 'Healthcare',
  craft: 'Craft',
  historic: 'Historic',
  landuse: 'Landuse',
  office: 'Office',
  tourism: 'Tourism',
}

const OVERTURE_L0_LABELS = {
  food_and_drink: 'Food & Drink',
  shopping: 'Shopping',
  arts_and_entertainment: 'Arts & Entertainment',
  sports_and_recreation: 'Sports & Recreation',
  health_care: 'Health Care',
  services_and_business: 'Services & Business',
  lifestyle_services: 'Lifestyle Services',
  community_and_government: 'Community & Government',
  cultural_and_historic: 'Cultural & Historic',
  education: 'Education',
  travel_and_transportation: 'Travel & Transportation',
  lodging: 'Lodging',
  geographic_entities: 'Geographic Features',
}

export const OSM_FILTER_KEYS = OSM_KEYS.map(key => ({
  key,
  label: OSM_KEY_LABELS[key] ?? key,
}))

export const OVERTURE_CATEGORIES = OVERTURE_L0S.map(key => ({
  key,
  label: OVERTURE_L0_LABELS[key] ?? key,
}))

// OpenFreeMap base map styles
export const BASE_MAP_STYLES = [
  {
    key: 'positron',
    label: 'Positron',
    url: 'https://tiles.openfreemap.org/styles/positron',
  },
  {
    key: 'liberty',
    label: 'Liberty',
    url: 'https://tiles.openfreemap.org/styles/liberty',
  },
  {
    key: 'dark',
    label: 'Dark Matter',
    url: 'https://tiles.openfreemap.org/styles/dark',
  },
]

// Conflated shared_label categories — generated from match_radii.csv.
// "Other *" entries are sorted last; App.vue uses that convention to leave
// them unchecked by default.
export const CONFLATED_LABELS = SHARED_LABELS

// Zoom thresholds — PMTiles min_zoom (site can't zoom out below this).
// PMTiles archives start at z10 and run to z14, or deeper where dense tiles
// would otherwise drop points; past the top zoom ol-pmtiles over-zooms.
// Per-layer point radius scales down at lower zooms (see utils.js).
export const MIN_ZOOM = 10

// Stadia Maps Geocoding
export const STADIA_GEOCODING_URL =
  'https://api.stadiamaps.com/geocoding/v1/search'
