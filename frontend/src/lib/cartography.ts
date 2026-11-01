/**
 * Cartographic conversion for the Environmental Timescape topographic sheet.
 *
 * Every function here is pure and framework-free so the sheet can be unit-tested without a
 * browser, a WebGL context or a network. Nothing in this module invents geography: it only
 * reshapes what the Stage 3A backend actually returned (`/api/v1/landscape`).
 *
 * DATA PROVENANCE
 *   terrain     Copernicus DEM GLO-90 (~90 m) via Open-Meteo - `observation_derived`, and a
 *               Digital SURFACE Model: elevations include buildings and canopy, not bare earth.
 *   osm         OpenStreetMap contributors (ODbL) via Overpass - `mapped`.
 *   land_cover  ESA WorldCover 2021 (v200, 10 m) - `mapped`, often unavailable.
 * A layer the backend reported as unavailable stays unavailable here: it yields an empty
 * FeatureCollection and an explicit reason, never synthesised features.
 */

import { KM_PER_DEG_LAT } from './timescapeMath.ts';

// --------------------------------------------------------------------------- vocabulary

/** Cartographic classes, in the order they are painted (later = on top). */
export const CARTO_CLASSES = [
  'farmland',
  'vegetation',
  'settlement',
  'protected',
  'water_area',
  'wetland',
  'waterway',
  'road',
  'label',
] as const;

export type CartoClass = (typeof CARTO_CLASSES)[number];

/**
 * Dedupe precedence when the same OSM element is returned by several layer queries (a river
 * tagged `natural=water` + `waterway=river` legitimately matches two selectors). Hydrology
 * wins over land use, areas over lines, and labels lose to everything else.
 */
const CLASS_PRECEDENCE: Record<CartoClass, number> = {
  water_area: 8,
  wetland: 7,
  waterway: 6,
  protected: 5,
  vegetation: 4,
  farmland: 3,
  settlement: 2,
  road: 1,
  label: 0,
};

const LINEAR_WATERWAYS = ['river', 'stream', 'canal', 'drain', 'ditch'];
const VEGETATION_LANDUSE = ['forest', 'meadow', 'grass', 'orchard', 'vineyard', 'scrub'];
const FARMLAND_LANDUSE = ['farmland', 'farmyard', 'allotments', 'greenhouse_horticulture'];
const SETTLEMENT_LANDUSE = ['residential', 'industrial', 'commercial', 'retail'];
const ROAD_RANK: Record<string, number> = {
  motorway: 6, trunk: 5, primary: 4, secondary: 3, tertiary: 2, unclassified: 1, residential: 1,
};

/** Survey-of-India-inspired palette. Muted paper, brown contours, blue hydro, green cover. */
export const TOPO_PALETTE = {
  paper: '#f6f1e3',
  paperEdge: '#e8dfc8',
  contour: '#a8763e',
  contourIndex: '#8a5c2b',
  water: '#7fa8cc',
  waterEdge: '#3f7fbf',
  wetland: '#a9c6d6',
  vegetation: '#c3d6a8',
  vegetationEdge: '#8fae6d',
  farmland: '#eae3c2',
  settlement: '#e6d7c3',
  settlementEdge: '#b39a7c',
  protected: '#d9c9a3',
  road: '#8c7a63',
  roadMajor: '#c2553f',
  label: '#4a3f30',
  gridLine: '#d8cdb2',
} as const;

// --------------------------------------------------------------------------- geojson types

export interface GeoPoint { type: 'Point'; coordinates: [number, number] }
export interface GeoLine { type: 'LineString'; coordinates: [number, number][] }
export interface GeoPoly { type: 'Polygon'; coordinates: [number, number][][] }
export type GeoGeometry = GeoPoint | GeoLine | GeoPoly;

export interface CartoFeature {
  type: 'Feature';
  id: string;
  properties: Record<string, unknown>;
  geometry: GeoGeometry;
}

export interface CartoCollection {
  type: 'FeatureCollection';
  features: CartoFeature[];
}

export interface CartoClassLayer {
  class: CartoClass;
  label: string;
  collection: CartoCollection;
  /** Count of features classified into this class, before any geometry was dropped. */
  classified: number;
}

// --------------------------------------------------------------------------- classification

export interface ClassifiedFeature {
  class: CartoClass;
  feature: any;
  /** True for seasonal/intermittent drainage (`intermittent=*` or `seasonal=*`). */
  intermittent: boolean;
  /** Highway rank 1-6, or 0 when the feature is not a road. */
  roadRank: number;
  /** Place rank for label features, or 0. */
  placeRank: number;
}

const PLACE_RANK: Record<string, number> = {
  city: 6, town: 5, suburb: 4, village: 3, hamlet: 2, locality: 1,
};

/**
 * Classifies one mapped feature from its OSM TAGS.
 *
 * Tags - not the backend's `layer`/`kind` label - are authoritative here: the same element can
 * arrive from several layer queries with a different `kind` each time, and only the tags say
 * what it physically is.
 */
export function classifyFeature(tags: Record<string, any> | null | undefined): ClassifiedFeature | null {
  const t = tags || {};
  const waterway = String(t.waterway || '').toLowerCase();
  const natural = String(t.natural || '').toLowerCase();
  const water = String(t.water || '').toLowerCase();
  const landuse = String(t.landuse || '').toLowerCase();
  const leisure = String(t.leisure || '').toLowerCase();
  const boundary = String(t.boundary || '').toLowerCase();
  const highway = String(t.highway || '').toLowerCase();
  const place = String(t.place || '').toLowerCase();
  const intermittent =
    String(t.intermittent || '').toLowerCase() === 'yes' || String(t.seasonal || '').toLowerCase() === 'yes';

  // A mapped riverbank is an area even though it carries a waterway tag.
  if (waterway === 'riverbank' || natural === 'water' || water !== '' ||
      landuse === 'reservoir' || landuse === 'basin') {
    return { class: 'water_area', feature: t, intermittent, roadRank: 0, placeRank: 0 };
  }
  if (natural === 'wetland' || landuse === 'salt_pond') {
    return { class: 'wetland', feature: t, intermittent, roadRank: 0, placeRank: 0 };
  }
  if (LINEAR_WATERWAYS.includes(waterway)) {
    return { class: 'waterway', feature: t, intermittent, roadRank: 0, placeRank: 0 };
  }
  if (boundary === 'protected_area' || leisure === 'nature_reserve' || boundary === 'national_park') {
    return { class: 'protected', feature: t, intermittent: false, roadRank: 0, placeRank: 0 };
  }
  if (natural === 'wood' || VEGETATION_LANDUSE.includes(landuse)) {
    return { class: 'vegetation', feature: t, intermittent: false, roadRank: 0, placeRank: 0 };
  }
  if (FARMLAND_LANDUSE.includes(landuse)) {
    return { class: 'farmland', feature: t, intermittent: false, roadRank: 0, placeRank: 0 };
  }
  if (SETTLEMENT_LANDUSE.includes(landuse)) {
    return { class: 'settlement', feature: t, intermittent: false, roadRank: 0, placeRank: 0 };
  }
  if (highway !== '') {
    return {
      class: 'road', feature: t, intermittent: false,
      roadRank: ROAD_RANK[highway] ?? 1, placeRank: 0,
    };
  }
  if (place !== '') {
    return { class: 'label', feature: t, intermittent: false, roadRank: 0, placeRank: PLACE_RANK[place] ?? 1 };
  }
  return null;
}

/** Stable identity for an OSM element, used to dedupe across layer queries. */
export function featureIdentity(feature: any): string {
  const type = feature?.osm_type ?? 'el';
  const id = feature?.osm_id ?? `${feature?.lat}:${feature?.lon}:${feature?.name}`;
  return `${type}/${id}`;
}

function isClosedRing(coords: [number, number][]): boolean {
  if (coords.length < 4) return false;
  const a = coords[0];
  const b = coords[coords.length - 1];
  return a[0] === b[0] && a[1] === b[1];
}

/**
 * Converts a backend feature into GeoJSON.
 *
 * The backend emits every linear geometry as a `LineString` (`osm.py:_parse_element`), including
 * closed rings, so a ring is recognised here by its first coordinate equalling its last and
 * promoted to a Polygon. Multi-sequence ways arrive flattened into one coordinate array with
 * `sequence_count` recording how many there were; that flattening is the backend's existing
 * behaviour and is surfaced in the feature properties rather than guessed at here.
 */
export function featureToGeometry(feature: any): GeoGeometry | null {
  const geom = feature?.geometry;
  const coords: [number, number][] | undefined = geom?.coordinates;
  if (geom && Array.isArray(coords) && coords.length > 0) {
    const clean = coords.filter(
      (c): c is [number, number] =>
        Array.isArray(c) && c.length >= 2 && Number.isFinite(c[0]) && Number.isFinite(c[1])
    );
    if (clean.length === 0) return null;
    if (geom.type === 'LineString' && clean.length >= 2) {
      return isClosedRing(clean) ? { type: 'Polygon', coordinates: [clean] } : { type: 'LineString', coordinates: clean };
    }
    if (geom.type === 'Point') return { type: 'Point', coordinates: clean[0] };
  }
  // Node features (and ways whose geometry was not requested) still have a representative point.
  const lat = Number(feature?.lat);
  const lon = Number(feature?.lon);
  if (Number.isFinite(lat) && Number.isFinite(lon)) return { type: 'Point', coordinates: [lon, lat] };
  return null;
}

export interface CartographyInput {
  /** `base_layer.osm.features` from /api/v1/landscape with include_geometry=true. */
  features?: any[] | null;
  /** Clip to the site circle (km). Features outside are counted but not drawn. */
  radiusKm?: number;
}

export interface CartographyResult {
  layers: Record<CartoClass, CartoClassLayer>;
  /** Features dropped because the same OSM element was returned by another layer query. */
  deduped: number;
  /** Features classified but with no usable coordinates. */
  dropped: number;
  /** Features outside the site radius. */
  outsideRadius: number;
  total: number;
}

const CLASS_LABELS: Record<CartoClass, string> = {
  water_area: 'Lakes and standing water',
  waterway: 'Rivers, streams, canals and drains',
  wetland: 'Wetland and marsh',
  vegetation: 'Forest, woodland and scrub',
  farmland: 'Agricultural land',
  settlement: 'Settlement and built-up land',
  protected: 'Protected area',
  road: 'Roads and transport',
  label: 'Place names',
};

/**
 * Builds one GeoJSON FeatureCollection per cartographic class from the mapped OSM layer.
 * An unavailable OSM layer (or an empty one) yields empty collections, never invented features.
 */
export function buildCartography(input: CartographyInput): CartographyResult {
  const features = Array.isArray(input.features) ? input.features : [];
  const radiusKm = Number.isFinite(input.radiusKm) ? Number(input.radiusKm) : Infinity;
  const best = new Map<string, { cls: ClassifiedFeature; feature: any }>();
  const layers = {} as Record<CartoClass, CartoClassLayer>;
  for (const c of CARTO_CLASSES) {
    layers[c] = { class: c, label: CLASS_LABELS[c], collection: { type: 'FeatureCollection', features: [] }, classified: 0 };
  }

  let deduped = 0;
  let outsideRadius = 0;
  for (const feature of features) {
    const cls = classifyFeature(feature?.osm_tags);
    if (!cls) continue;
    const dist = Number(feature?.distance_km);
    if (Number.isFinite(dist) && dist > radiusKm + 1e-9) { outsideRadius++; continue; }
    layers[cls.class].classified++;
    const key = featureIdentity(feature);
    const existing = best.get(key);
    if (existing) {
      deduped++;
      if (CLASS_PRECEDENCE[cls.class] > CLASS_PRECEDENCE[existing.cls.class]) best.set(key, { cls, feature });
      continue;
    }
    best.set(key, { cls, feature });
  }

  let dropped = 0;
  for (const { cls, feature } of best.values()) {
    const geometry = featureToGeometry(feature);
    if (!geometry) { dropped++; continue; }
    const tags = feature.osm_tags || {};
    layers[cls.class].collection.features.push({
      type: 'Feature',
      id: featureIdentity(feature),
      properties: {
        name: feature.name ?? null,
        kind: feature.kind ?? null,
        layer: feature.layer ?? null,
        data_class: feature.data_class ?? 'mapped',
        distance_km: feature.distance_km ?? null,
        intermittent: cls.intermittent,
        road_rank: cls.roadRank,
        place_rank: cls.placeRank,
        sequence_count: feature.geometry?.sequence_count ?? null,
        osm_tags: tags,
      },
      geometry,
    });
  }

  for (const c of CARTO_CLASSES) {
    // Deterministic paint order: bigger areas first, then nearer features.
    layers[c].collection.features.sort((a, b) => {
      const da = Number(a.properties.distance_km ?? 0);
      const db = Number(b.properties.distance_km ?? 0);
      return db - da;
    });
  }

  return { layers, deduped, dropped, outsideRadius, total: features.length };
}

// --------------------------------------------------------------------------- contours

export interface ContourResult {
  levels: number[];
  interval_m: number;
  /** GeoJSON MultiLineString per level; empty when the DEM is unavailable. */
  lines: { level: number; geometry: { type: 'MultiLineString'; coordinates: [number, number][][] } }[];
  collection: CartoCollection;
  /** Grid spacing in km between adjacent DEM samples - the resolution the contours were drawn on. */
  sample_spacing_km: number;
  index_interval_m: number;
  note: string;
}

const CONTOUR_NOTE =
  'Contours are interpolated between DEM samples. Interpolation draws a smooth line but does not ' +
  'add native resolution: the underlying samples are Copernicus GLO-90 (~90 m) gridded to the ' +
  'returned grid, and the spacing between samples is published in the legend.';

/**
 * Picks a Survey-of-India-style contour interval for the site's relief.
 *
 * The rule is the finest interval that still yields at most about eight contours, which keeps the
 * progression cartographic (5 m on alluvial plains, 10-20 m on rising ground, 50-500 m in
 * mountains) and - just as important - stops a coarse 20x20 sample grid from implying detail it
 * does not contain. Twenty-two contour lines interpolated across 650 m sample cells would read as
 * precision the DEM does not have.
 */
export const MAX_CONTOURS_PER_SHEET = 8;

export function contourIntervalForRelief(reliefM: number): number {
  const relief = Math.abs(Number.isFinite(reliefM) ? reliefM : 0);
  const candidates = [5, 10, 20, 50, 100, 200, 500];
  for (const interval of candidates) {
    if (relief / interval <= MAX_CONTOURS_PER_SHEET) return interval;
  }
  return 1000;
}

export function contourLevels(minM: number, maxM: number, intervalM: number): number[] {
  if (!Number.isFinite(minM) || !Number.isFinite(maxM) || !(intervalM > 0) || maxM <= minM) return [];
  // Strictly between the extremes: a contour at exactly min or max degenerates along the grid edge.
  const first = Math.floor(minM / intervalM) * intervalM + intervalM;
  const levels: number[] = [];
  for (let level = first; level < maxM; level += intervalM) levels.push(Math.round(level * 100) / 100);
  return levels;
}

function edgePoint(
  a: number, b: number, level: number, ax: number, ay: number, bx: number, by: number
): [number, number] {
  const span = b - a;
  const t = Math.abs(span) < 1e-12 ? 0.5 : (level - a) / span;
  return [ax + (bx - ax) * t, ay + (by - ay) * t];
}

/**
 * Marching-squares contours over the returned DEM sample grid, emitted in lat/lon.
 *
 * Grid convention matches the backend and `sampleElevationMeters`: row 0 = north, column 0 = west.
 * Returns an empty result (and never a synthesised surface) when the grid is missing or invalid.
 */
export function contoursFromGrid(params: {
  values_m?: number[][] | null;
  min_m?: number | null;
  max_m?: number | null;
  span_km?: number | null;
  lat: number;
  lon: number;
}): ContourResult {
  const empty: ContourResult = {
    levels: [], interval_m: 0, lines: [], collection: { type: 'FeatureCollection', features: [] },
    sample_spacing_km: 0, index_interval_m: 0, note: CONTOUR_NOTE,
  };
  const grid = params.values_m;
  if (!Array.isArray(grid) || grid.length < 2) return empty;
  const n = grid.length;
  for (const row of grid) {
    if (!Array.isArray(row) || row.length !== n) return empty;
    for (const v of row) if (!Number.isFinite(v)) return empty;
  }
  const minM = Number(params.min_m ?? Math.min(...grid.flat()));
  const maxM = Number(params.max_m ?? Math.max(...grid.flat()));
  const spanKm = Number(params.span_km);
  if (!Number.isFinite(spanKm) || spanKm <= 0 || !(maxM > minM)) return empty;

  const interval = contourIntervalForRelief(maxM - minM);
  const levels = contourLevels(minM, maxM, interval);
  if (levels.length === 0) return empty;

  const cellKm = spanKm / (n - 1);
  const cosLat = Math.max(1e-6, Math.cos((params.lat * Math.PI) / 180));
  const toLatLon = (gx: number, gy: number): [number, number] => {
    const eastKm = (gx - (n - 1) / 2) * cellKm;
    const northKm = ((n - 1) / 2 - gy) * cellKm;
    return [
      params.lon + eastKm / (KM_PER_DEG_LAT * cosLat),
      params.lat + northKm / KM_PER_DEG_LAT,
    ];
  };

  const features: CartoFeature[] = [];
  const indexInterval = interval * 5;
  for (const level of levels) {
    const segments: [number, number][][] = [];
    for (let r = 0; r < n - 1; r++) {
      for (let c = 0; c < n - 1; c++) {
        const nw = grid[r][c];
        const ne = grid[r][c + 1];
        const se = grid[r + 1][c + 1];
        const sw = grid[r + 1][c];
        let idx = 0;
        if (nw >= level) idx |= 8;
        if (ne >= level) idx |= 4;
        if (se >= level) idx |= 2;
        if (sw >= level) idx |= 1;
        if (idx === 0 || idx === 15) continue;
        const north = () => toLatLon(...edgePoint(nw, ne, level, c, r, c + 1, r));
        const east = () => toLatLon(...edgePoint(ne, se, level, c + 1, r, c + 1, r + 1));
        const south = () => toLatLon(...edgePoint(sw, se, level, c, r + 1, c + 1, r + 1));
        const west = () => toLatLon(...edgePoint(nw, sw, level, c, r, c, r + 1));
        const table: Record<number, (() => [number, number])[][]> = {
          1: [[west, south]], 2: [[south, east]], 3: [[west, east]], 4: [[north, east]],
          5: [[north, east], [west, south]], 6: [[north, south]], 7: [[north, west]],
          8: [[north, west]], 9: [[north, south]], 10: [[north, west], [south, east]],
          11: [[south, east]], 12: [[west, east]], 13: [[west, south]], 14: [[south, east]],
        };
        for (const seg of table[idx] || []) segments.push([seg[0](), seg[1]()]);
      }
    }
    if (segments.length === 0) continue;
    features.push({
      type: 'Feature',
      id: `contour:${level}`,
      properties: { level_m: level, index: Math.abs(level % indexInterval) < 1e-6 },
      geometry: { type: 'MultiLineString', coordinates: segments } as unknown as GeoGeometry,
    });
  }

  return {
    levels,
    interval_m: interval,
    index_interval_m: indexInterval,
    lines: features.map((f) => ({
      level: Number(f.properties.level_m),
      geometry: f.geometry as unknown as { type: 'MultiLineString'; coordinates: [number, number][][] },
    })),
    collection: { type: 'FeatureCollection', features },
    sample_spacing_km: Math.round(cellKm * 10000) / 10000,
    note: CONTOUR_NOTE,
  };
}

/**
 * Elevation tinting as one quad per DEM sample cell.
 *
 * This is deliberately a choropleth of the returned samples rather than a smoothly interpolated
 * raster: each coloured quad is exactly one sample cell, so the sheet cannot imply detail the
 * grid does not have. The contour lines drawn on top are the interpolated product, and the
 * legend states both.
 */
export function elevationTintCells(params: {
  values_m?: number[][] | null;
  min_m?: number | null;
  max_m?: number | null;
  span_km?: number | null;
  lat: number;
  lon: number;
}): { collection: CartoCollection; stops: { stop: number; color: string }[]; band_edges_m: number[]; available: boolean } {
  const grid = params.values_m;
  const unavailable = {
    collection: { type: 'FeatureCollection' as const, features: [] as CartoFeature[] },
    stops: [{ stop: 0, color: TOPO_PALETTE.paper }],
    band_edges_m: [] as number[],
    available: false,
  };
  if (!Array.isArray(grid) || grid.length < 2) return unavailable;
  const n = grid.length;
  for (const row of grid) {
    if (!Array.isArray(row) || row.length !== n) return unavailable;
    for (const v of row) if (!Number.isFinite(v)) return unavailable;
  }
  const minM = Number(params.min_m ?? Math.min(...grid.flat()));
  const maxM = Number(params.max_m ?? Math.max(...grid.flat()));
  const spanKm = Number(params.span_km);
  if (!Number.isFinite(spanKm) || spanKm <= 0 || !(maxM > minM)) return unavailable;

  const bandCount = 6;
  const bandWidth = (maxM - minM) / bandCount;
  const cellKm = spanKm / (n - 1);
  const cosLat = Math.max(1e-6, Math.cos((params.lat * Math.PI) / 180));
  const toLatLon = (gx: number, gy: number): [number, number] => [
    params.lon + ((gx - (n - 1) / 2) * cellKm) / (KM_PER_DEG_LAT * cosLat),
    params.lat + (((n - 1) / 2 - gy) * cellKm) / KM_PER_DEG_LAT,
  ];

  const features: CartoFeature[] = [];
  const bandEdges: number[] = [];
  for (let b = 0; b <= bandCount; b++) bandEdges.push(Math.round((minM + b * bandWidth) * 100) / 100);
  for (let r = 0; r < n - 1; r++) {
    for (let c = 0; c < n - 1; c++) {
      const mean = (grid[r][c] + grid[r][c + 1] + grid[r + 1][c] + grid[r + 1][c + 1]) / 4;
      const band = Math.max(0, Math.min(bandCount - 1, Math.floor((mean - minM) / bandWidth)));
      features.push({
        type: 'Feature',
        id: `tint:${r}:${c}`,
        properties: { band, mean_m: Math.round(mean * 100) / 100 },
        geometry: {
          type: 'Polygon',
          coordinates: [[toLatLon(c, r), toLatLon(c + 1, r), toLatLon(c + 1, r + 1), toLatLon(c, r + 1), toLatLon(c, r)]],
        },
      });
    }
  }
  return {
    collection: { type: 'FeatureCollection', features },
    stops: hypsometricStops(minM, maxM),
    band_edges_m: bandEdges,
    available: true,
  };
}

/**
 * Elevation in metres AMSL at a geographic point, bilinearly interpolated over the returned DEM
 * samples. Returns null when the DEM is unavailable - the readout then says so instead of
 * showing a number that was never measured.
 */
export function elevationAtLatLon(params: {
  lat: number; lon: number;
  values_m?: number[][] | null;
  span_km?: number | null;
  siteLat: number; siteLon: number;
}): number | null {
  const grid = params.values_m;
  if (!Array.isArray(grid) || grid.length < 2) return null;
  const n = grid.length;
  for (const row of grid) {
    if (!Array.isArray(row) || row.length !== n) return null;
    for (const v of row) if (!Number.isFinite(v)) return null;
  }
  const spanKm = Number(params.span_km);
  if (!Number.isFinite(spanKm) || spanKm <= 0) return null;
  const cosLat = Math.max(1e-6, Math.cos((params.siteLat * Math.PI) / 180));
  const eastKm = (params.lon - params.siteLon) * KM_PER_DEG_LAT * cosLat;
  const northKm = (params.lat - params.siteLat) * KM_PER_DEG_LAT;
  const cellKm = spanKm / (n - 1);
  const gx = eastKm / cellKm + (n - 1) / 2;
  const gy = (n - 1) / 2 - northKm / cellKm;
  // A tolerance of 1e-9 grid cells absorbs round-trip lat/lon -> ENU -> grid floating point
  // noise so a point sitting exactly on the sheet edge still reads the edge sample instead of
  // reporting "off sheet". Anything genuinely outside is still rejected.
  const eps = 1e-9;
  if (gx < -eps || gy < -eps || gx > n - 1 + eps || gy > n - 1 + eps) return null;
  const sx = Math.max(0, Math.min(n - 1, gx));
  const sy = Math.max(0, Math.min(n - 1, gy));
  const c0 = Math.floor(sx);
  const r0 = Math.floor(sy);
  const c1 = Math.min(n - 1, c0 + 1);
  const r1 = Math.min(n - 1, r0 + 1);
  const tx = sx - c0;
  const ty = sy - r0;
  const top = grid[r0][c0] * (1 - tx) + grid[r0][c1] * tx;
  const bottom = grid[r1][c0] * (1 - tx) + grid[r1][c1] * tx;
  return Math.round((top * (1 - ty) + bottom * ty) * 10) / 10;
}

/** Illustrative footprint envelope from the declared area - modelled, never a surveyed boundary. */
export function footprintEnvelope(params: {
  lat: number; lon: number; footprintHa?: number | null;
}): { collection: CartoCollection; sideM: number } {
  const empty = { collection: { type: 'FeatureCollection' as const, features: [] as CartoFeature[] }, sideM: 0 };
  const ha = Number(params.footprintHa);
  if (!Number.isFinite(ha) || ha <= 0) return empty;
  const sideM = Math.sqrt(ha * 10000);
  const halfDegLat = sideM / 2 / KM_PER_DEG_LAT / 1000;
  const cosLat = Math.max(1e-6, Math.cos((params.lat * Math.PI) / 180));
  const halfDegLon = sideM / 2 / (KM_PER_DEG_LAT * cosLat) / 1000;
  return {
    sideM: Math.round(sideM),
    collection: {
      type: 'FeatureCollection',
      features: [{
        type: 'Feature',
        id: 'footprint-envelope',
        properties: {
          data_class: 'modelled',
          note: 'Illustrative envelope from the declared footprint area, oriented to the grid. Not a surveyed boundary.',
        },
        geometry: {
          type: 'Polygon',
          coordinates: [[
            [params.lon - halfDegLon, params.lat - halfDegLat],
            [params.lon + halfDegLon, params.lat - halfDegLat],
            [params.lon + halfDegLon, params.lat + halfDegLat],
            [params.lon - halfDegLon, params.lat + halfDegLat],
            [params.lon - halfDegLon, params.lat - halfDegLat],
          ]],
        },
      }],
    },
  };
}

// --------------------------------------------------------------------------- sheet furniture

/** Hypsometric tint stops from the site's own relief range (never a global elevation ramp). */
export function hypsometricStops(minM: number, maxM: number): { stop: number; color: string }[] {
  const ramp = ['#dfe8c8', '#e7e3bb', '#e9dcb4', '#dfc9a0', '#d2b189', '#c39a76'];
  if (!Number.isFinite(minM) || !Number.isFinite(maxM) || maxM <= minM) {
    return [{ stop: 0, color: TOPO_PALETTE.paper }];
  }
  return ramp.map((color, i) => ({
    stop: Math.round((minM + ((maxM - minM) * i) / (ramp.length - 1)) * 100) / 100,
    color,
  }));
}

const METERS_PER_PIXEL_AT_Z0 = 156543.03392;

/** Ground metres per CSS pixel at a zoom level - the basis of the scale bar. */
export function metersPerPixel(zoom: number, lat: number): number {
  return (METERS_PER_PIXEL_AT_Z0 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, zoom);
}

/** Rounds a raw distance to a cartographer-friendly scale-bar length (1, 2 or 5 x 10^n). */
export function niceScaleBarMeters(rawMeters: number): number {
  if (!Number.isFinite(rawMeters) || rawMeters <= 0) return 0;
  const pow = Math.pow(10, Math.floor(Math.log10(rawMeters)));
  const mantissa = rawMeters / pow;
  const nice = mantissa >= 5 ? 5 : mantissa >= 2 ? 2 : 1;
  return nice * pow;
}

export interface ScaleBar { meters: number; pixels: number; text: string }

export function computeScaleBar(zoom: number, lat: number, targetPixels = 120): ScaleBar {
  const meters = niceScaleBarMeters(metersPerPixel(zoom, lat) * targetPixels);
  const pixels = meters > 0 ? meters / Math.max(1e-9, metersPerPixel(zoom, lat)) : 0;
  return {
    meters,
    pixels: Math.round(pixels * 10) / 10,
    text: meters >= 1000 ? `${Math.round((meters / 1000) * 100) / 100} km` : `${Math.round(meters)} m`,
  };
}

// --------------------------------------------------------------------------- availability

export interface LayerAvailability {
  id: 'terrain' | 'osm' | 'land_cover';
  label: string;
  status: string;
  reason: string | null;
  detail: string | null;
  data_class: string | null;
  source: string | null;
  attribution: string | null;
  available: boolean;
}

const LAYER_LABELS: Record<string, string> = {
  terrain: 'Terrain / contours (Copernicus GLO-90)',
  osm: 'Mapped geography (OpenStreetMap)',
  land_cover: 'Land cover (ESA WorldCover 2021)',
};

/**
 * Turns the backend's per-layer status into legend entries. Unavailable layers are reported with
 * their real reason and never replaced by drawn content.
 */
export function layerAvailability(baseLayer: any): LayerAvailability[] {
  const out: LayerAvailability[] = [];
  for (const id of ['terrain', 'osm', 'land_cover'] as const) {
    const layer = baseLayer?.[id];
    if (!layer) continue;
    const status = String(layer.status ?? 'unavailable');
    out.push({
      id,
      label: LAYER_LABELS[id],
      status,
      reason: layer.reason ?? null,
      detail: layer.detail ?? layer.data_label ?? null,
      data_class: layer.data_class ?? null,
      source: layer.source ?? layer.dataset?.citation ?? null,
      attribution: layer.attribution ?? null,
      available: status === 'ok',
    });
  }
  return out;
}

export interface SheetSummary {
  terrainAvailable: boolean;
  terrainReason: string | null;
  dataClass: string | null;
  surfaceModel: string | null;
  minM: number | null;
  maxM: number | null;
  reliefM: number | null;
  resolutionM: number | null;
  spanKm: number | null;
  gridSize: number | null;
  contourIntervalM: number;
  contourCount: number;
  sampleSpacingKm: number;
  mappedFeatures: number;
  layerStatuses: LayerAvailability[];
  /**
   * The backend's aggregate warning when only some OSM layers were retrieved, verbatim. It states
   * that absence of a feature in the missing layers says nothing about the ground - which the
   * per-layer list alone does not convey, so it must be shown rather than summarised away.
   */
  partialCoverageWarning: string | null;
  osmAttribution: string | null;
  landCoverEpoch: string | null;
}

/** One object the sheet header, legend and integrity panel all read from. */
export function summarizeSheet(params: { baseLayer: any; cartography: CartographyResult; contours: ContourResult }): SheetSummary {
  const { baseLayer, cartography, contours } = params;
  const terrain = baseLayer?.terrain || {};
  const osm = baseLayer?.osm || {};
  const landCover = baseLayer?.land_cover || {};
  const terrainAvailable = terrain.status === 'ok' && contours.levels.length > 0;
  const minM = Number.isFinite(terrain.min_m) ? Number(terrain.min_m) : null;
  const maxM = Number.isFinite(terrain.max_m) ? Number(terrain.max_m) : null;
  let mappedFeatures = 0;
  for (const c of CARTO_CLASSES) mappedFeatures += cartography.layers[c].collection.features.length;
  return {
    terrainAvailable,
    terrainReason: terrainAvailable ? null : terrain.reason ?? terrain.status ?? 'unavailable',
    dataClass: terrain.data_class ?? null,
    surfaceModel: terrain.surface_model ?? null,
    minM, maxM,
    reliefM: minM !== null && maxM !== null ? Math.round((maxM - minM) * 100) / 100 : null,
    resolutionM: Number.isFinite(terrain.resolution_m) ? Number(terrain.resolution_m) : null,
    spanKm: Number.isFinite(terrain.span_km) ? Number(terrain.span_km) : null,
    gridSize: Number.isFinite(terrain.grid_size) ? Number(terrain.grid_size) : null,
    contourIntervalM: contours.interval_m,
    contourCount: contours.lines.length,
    sampleSpacingKm: contours.sample_spacing_km,
    mappedFeatures,
    layerStatuses: layerAvailability(baseLayer),
    // Passed through untouched: rewording it would weaken a deliberate integrity statement.
    partialCoverageWarning:
      typeof osm.partial_coverage_warning === 'string' && osm.partial_coverage_warning.trim()
        ? osm.partial_coverage_warning
        : null,
    osmAttribution: osm.attribution ?? null,
    landCoverEpoch: landCover.dataset?.epoch ?? landCover.dataset?.name ?? null,
  };
}

/**
 * Base-geography identity for the temporal-invariance guarantee: switching Present / Development
 * / Future must not change anything the sheet draws. Compared in tests.
 */
export function baseGeographyFingerprint(params: { cartography: CartographyResult; contours: ContourResult }): string {
  const parts: string[] = [];
  for (const c of CARTO_CLASSES) {
    const layer = params.cartography.layers[c];
    parts.push(`${c}:${layer.collection.features.map((f) => f.id).sort().join(',')}`);
  }
  parts.push(`contours:${params.contours.lines.map((l) => l.level).join(',')}`);
  return parts.join('|');
}
