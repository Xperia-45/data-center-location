import type { Site } from './api.ts';

export const KM_PER_DEG_LAT = 111.32;
export const TERRAIN_SPAN = 80;
export const ANALYSIS_RADIUS_UNITS = 32;
export const SCENE_MAX_COORD = 38;
export const MAX_SCENE_FEATURES = 30;
export const MIN_PAD_WIDTH = 1.4;
export const MAX_PAD_WIDTH = 6.0;

export const DEFAULT_VERTICAL_EXAGGERATION = 2.5;
export const MAX_SCENE_RELIEF_UNITS = 11.0;

export const SITE_PRESETS = {
  ludhiana: { name: 'Ludhiana node', lat: 30.901, lon: 75.8573 },
  mohali: { name: 'Mohali candidate', lat: 30.7046, lon: 76.7179 },
  hyderabad: { name: 'Hyderabad candidate', lat: 17.385, lon: 78.4867 },
} as const;

export type PresetKey = keyof typeof SITE_PRESETS;

export interface ElevationGridPayload {
  status: 'ok' | 'unavailable' | string;
  reason?: string | null;
  detail?: string;
  lat?: number | null;
  lon?: number | null;
  radius_km?: number | null;
  grid_size?: number;
  batch_count?: number;
  span_km?: number;
  values_m?: number[][] | null;
  min_m?: number | null;
  max_m?: number | null;
  center_m?: number | null;
  relief_m?: number | null;
  resolution_m?: number | null;
  source?: string;
  source_url?: string;
  copernicus_doi_url?: string;
  license?: string;
  attribution?: string | null;
  cache_hit?: string | null;
  data_label?: string;
}

export interface TerrainVerticalParams {
  isMeasured: boolean;
  gridSize: number | null;
  minM: number | null;
  maxM: number | null;
  centerM: number | null;
  reliefM: number | null;
  unitsPerMeter: number;
  verticalExaggeration: number;
}

export interface EnuPointKm {
  eastKm: number;
  northKm: number;
  distanceKm: number;
}

export interface ProjectedFeature {
  feature: any;
  x: number;
  y: number;
  z: number;
  eastKm: number;
  northKm: number;
  distanceKm: number;
  sortDistanceKm: number;
  inBounds: boolean;
  withinRadius: boolean;
}

export interface HallPlacement {
  x: number;
  y: number;
  z: number;
  width: number;
  height: number;
  depth: number;
}

export interface CampusLayout {
  visible: boolean;
  padWidth: number;
  padDepth: number;
  padThickness: number;
  padY: number;
  metricPadWidth: number;
  halls: HallPlacement[];
  mitigationTrees: { x: number; y: number; z: number; radius: number; height: number }[];
  solarCanopy: { x: number; y: number; z: number; width: number; depth: number; tiltX: number } | null;
}

export function sanitizeRadiusKm(radiusKm: number): number {
  return Number.isFinite(radiusKm) && radiusKm > 0 ? radiusKm : 5;
}

export function unitsPerKmForRadius(radiusKm: number): number {
  return ANALYSIS_RADIUS_UNITS / sanitizeRadiusKm(radiusKm);
}

/**
 * Validates that an elevation payload contains a complete, finite numeric DEM grid (>= 2x2)
 * without hardcoding a fixed grid resolution.
 */
export function isValidElevationGrid(
  elevation: ElevationGridPayload | null | undefined
): elevation is ElevationGridPayload & { values_m: number[][] } {
  if (!elevation || elevation.status !== 'ok' || !Array.isArray(elevation.values_m)) {
    return false;
  }
  const rows = elevation.values_m.length;
  if (rows < 2) return false;
  if (elevation.grid_size !== undefined && Number(elevation.grid_size) !== rows) {
    return false;
  }
  for (let r = 0; r < rows; r++) {
    const row = elevation.values_m[r];
    if (!Array.isArray(row) || row.length !== rows) return false;
    for (let c = 0; c < rows; c++) {
      const v = row[c];
      if (typeof v !== 'number' || !Number.isFinite(v) || v < -500 || v > 9000) {
        return false;
      }
    }
  }
  return true;
}

/**
 * Bilinearly interpolates elevation in metres AMSL at Three.js world (x, z) across [-40, +40].
 * Row 0 = North (z = -40), Row N-1 = South (z = +40); Col 0 = West (x = -40), Col N-1 = East (x = +40).
 */
export function sampleElevationMeters(
  x: number,
  z: number,
  elevation: ElevationGridPayload | null | undefined
): number | null {
  if (!isValidElevationGrid(elevation)) return null;
  const grid = elevation.values_m;
  const n = grid.length;
  const halfSpan = TERRAIN_SPAN / 2;
  const uNorm = Math.max(0, Math.min(1, (x + halfSpan) / TERRAIN_SPAN));
  const vNorm = Math.max(0, Math.min(1, (z + halfSpan) / TERRAIN_SPAN));
  const gx = uNorm * (n - 1);
  const gz = vNorm * (n - 1);
  const c0 = Math.floor(gx);
  const c1 = Math.min(n - 1, c0 + 1);
  const tx = gx - c0;
  const r0 = Math.floor(gz);
  const r1 = Math.min(n - 1, r0 + 1);
  const tz = gz - r0;
  const top = grid[r0][c0] * (1 - tx) + grid[r0][c1] * tx;
  const bottom = grid[r1][c0] * (1 - tx) + grid[r1][c1] * tx;
  return top * (1 - tz) + bottom * tz;
}

/**
 * Computes AMSL summary statistics and bounded vertical exaggeration for a site's DEM grid.
 */
export function computeTerrainVerticalParams(
  elevation: ElevationGridPayload | null | undefined,
  radiusKm: number = 5
): TerrainVerticalParams {
  const unitsPerMeter = unitsPerKmForRadius(radiusKm) / 1000;
  if (!isValidElevationGrid(elevation)) {
    return {
      isMeasured: false,
      gridSize: null,
      minM: null,
      maxM: null,
      centerM: null,
      reliefM: null,
      unitsPerMeter,
      verticalExaggeration: 0,
    };
  }
  const grid = elevation.values_m;
  let minM = Infinity;
  let maxM = -Infinity;
  for (const row of grid) {
    for (const v of row) {
      if (v < minM) minM = v;
      if (v > maxM) maxM = v;
    }
  }
  const centerM = sampleElevationMeters(0, 0, elevation) ?? (minM + maxM) / 2;
  const reliefM = Math.max(0, maxM - minM);
  const rawSceneRelief = reliefM * unitsPerMeter;
  let verticalExaggeration = DEFAULT_VERTICAL_EXAGGERATION;
  if (rawSceneRelief * DEFAULT_VERTICAL_EXAGGERATION > MAX_SCENE_RELIEF_UNITS && rawSceneRelief > 1e-6) {
    verticalExaggeration = Math.max(0.25, Math.round((MAX_SCENE_RELIEF_UNITS / rawSceneRelief) * 100) / 100);
  }
  return {
    isMeasured: true,
    gridSize: grid.length,
    minM: Math.round(minM * 100) / 100,
    maxM: Math.round(maxM * 100) / 100,
    centerM: Math.round(centerM * 100) / 100,
    reliefM: Math.round(reliefM * 100) / 100,
    unitsPerMeter,
    verticalExaggeration,
  };
}

/**
 * Hypsometric RGB [0..1] color ramp for measured DEM terrain vertices.
 */
export function elevationColorRgb(
  elevM: number,
  minM: number,
  maxM: number
): [number, number, number] {
  const span = Math.max(25, maxM - minM);
  const t = Math.max(0, Math.min(1, (elevM - minM) / span));
  if (t < 0.45) {
    const k = t / 0.45;
    // Lowland alluvial green (#23593a) -> mid-plain green (#437b4b)
    return [0.137 + k * 0.126, 0.349 + k * 0.133, 0.227 + k * 0.067];
  }
  if (t < 0.8) {
    const k = (t - 0.45) / 0.35;
    // Mid-plain (#437b4b) -> upland olive/ochre (#788048)
    return [0.263 + k * 0.208, 0.482 + k * 0.02, 0.294 - k * 0.012];
  }
  const k = (t - 0.8) / 0.2;
  // Upland (#788048) -> high rocky ridge (#94806c)
  return [0.471 + k * 0.109, 0.502, 0.282 + k * 0.141];
}

/**
 * Local tangent-plane (ENU) conversion with latitude-adjusted longitude scale.
 * +eastKm = East, +northKm = North.
 */
export function latLonToEnuKm(
  lat: number,
  lon: number,
  originLat: number,
  originLon: number
): EnuPointKm {
  const latRad = (originLat * Math.PI) / 180;
  const cosLat = Math.max(1e-6, Math.cos(latRad));
  const eastKm = (lon - originLon) * KM_PER_DEG_LAT * cosLat;
  const northKm = (lat - originLat) * KM_PER_DEG_LAT;
  const distanceKm = Math.hypot(eastKm, northKm);
  return { eastKm, northKm, distanceKm };
}

/**
 * Returns local Three.js scene height Y at world (x, z):
 * - Measured DEM (status === 'ok'): bilinearly interpolated height relative to site center_m,
 *   scaled by unitsPerMeter * verticalExaggeration (so (0, 0) sits at y = 0).
 * - Missing / unavailable DEM: explicit flat reference plane at y = 0 (never fake hills).
 */
export function terrainHeightAt(
  x: number,
  z: number,
  elevation?: ElevationGridPayload | null,
  radiusKm: number = 5
): number {
  if (!isValidElevationGrid(elevation)) {
    return 0;
  }
  const elevM = sampleElevationMeters(x, z, elevation);
  const centerM = sampleElevationMeters(0, 0, elevation);
  if (elevM === null || centerM === null) return 0;
  const params = computeTerrainVerticalParams(elevation, radiusKm);
  return (elevM - centerM) * params.unitsPerMeter * params.verticalExaggeration;
}

/**
 * Projects geographic coordinates (lat, lon) into Three.js local scene coordinates:
 * +X = East, +Y = Up (sampled on DEM or flat fallback), -Z = North, scaled so site.radius_km = ANALYSIS_RADIUS_UNITS.
 */
export function projectLatLonToScene(
  lat: number,
  lon: number,
  site: Pick<Site, 'lat' | 'lon' | 'radius_km'>,
  elevation?: ElevationGridPayload | null
) {
  const radiusKm = sanitizeRadiusKm(site.radius_km);
  const unitsPerKm = unitsPerKmForRadius(radiusKm);
  const { eastKm, northKm, distanceKm } = latLonToEnuKm(lat, lon, site.lat, site.lon);
  const x = eastKm * unitsPerKm;
  const z = -northKm * unitsPerKm;
  const y = terrainHeightAt(x, z, elevation, radiusKm);
  const inBounds = Math.abs(x) <= SCENE_MAX_COORD && Math.abs(z) <= SCENE_MAX_COORD;
  const withinRadius = distanceKm <= radiusKm + 1e-6;
  return { x, y, z, eastKm, northKm, distanceKm, inBounds, withinRadius };
}

/**
 * Defensive component-level guard so Timescape never renders features from a previous site
 * or while an environmental analysis is currently loading.
 */
export function resolveTimescapeFeatures(params: {
  site: Pick<Site, 'id'> | null | undefined;
  features: any[] | null | undefined;
  analysisSiteId?: string | null;
  loading?: boolean;
}): any[] {
  const { site, features, analysisSiteId, loading } = params;
  if (loading || !site || !Array.isArray(features)) return [];
  if (analysisSiteId !== undefined && analysisSiteId !== (site.id ?? null)) return [];
  return features;
}

/**
 * Defensive component-level guard so Timescape never applies cached elevation data
 * belonging to another site or another analysis radius.
 */
export function resolveTimescapeElevation(params: {
  site: Pick<Site, 'id' | 'lat' | 'lon' | 'radius_km'> | null | undefined;
  elevation: ElevationGridPayload | null | undefined;
  analysisSiteId?: string | null;
  loading?: boolean;
}): ElevationGridPayload | null {
  const { site, elevation, analysisSiteId, loading } = params;
  if (loading || !site || !elevation) return null;
  if (analysisSiteId !== undefined && analysisSiteId !== (site.id ?? null)) return null;
  if (elevation.lat != null && Math.abs(Number(elevation.lat) - Number(site.lat)) > 0.002) {
    return null;
  }
  if (elevation.lon != null && Math.abs(Number(elevation.lon) - Number(site.lon)) > 0.002) {
    return null;
  }
  const rKm = sanitizeRadiusKm(site.radius_km);
  if (elevation.radius_km != null && Math.abs(Number(elevation.radius_km) - rKm) > 0.05) {
    return null;
  }
  return elevation;
}

/**
 * Sorts all valid features ascending by distance from the site BEFORE selecting up to maxCount
 * in-bounds features for the 3D scene.
 */
export function selectSceneFeatures(
  features: any[],
  site: Pick<Site, 'lat' | 'lon' | 'radius_km'>,
  maxCount: number = MAX_SCENE_FEATURES,
  elevation?: ElevationGridPayload | null
): ProjectedFeature[] {
  if (!Array.isArray(features) || maxCount <= 0) return [];
  const projected: ProjectedFeature[] = [];
  for (const f of features) {
    if (!f || !Number.isFinite(Number(f.lat)) || !Number.isFinite(Number(f.lon))) continue;
    const p = projectLatLonToScene(Number(f.lat), Number(f.lon), site, elevation);
    if (!p.inBounds) continue;
    const reportedDist = Number(f.distance_km);
    const sortDistanceKm = Number.isFinite(reportedDist) && reportedDist >= 0 ? reportedDist : p.distanceKm;
    projected.push({
      feature: f,
      ...p,
      sortDistanceKm,
    });
  }
  projected.sort((a, b) => {
    if (a.sortDistanceKm !== b.sortDistanceKm) return a.sortDistanceKm - b.sortDistanceKm;
    if (a.distanceKm !== b.distanceKm) return a.distanceKm - b.distanceKm;
    const kindCmp = String(a.feature.kind ?? '').localeCompare(String(b.feature.kind ?? ''));
    if (kindCmp !== 0) return kindCmp;
    return String(a.feature.name ?? '').localeCompare(String(b.feature.name ?? ''));
  });
  return projected.slice(0, maxCount);
}

/**
 * Computes geographically grounded campus dimensions (1 ha = 0.01 km²) bounded so
 * candidate halls, pad, trees, and solar remain compact inside the parcel and never bury nearby features.
 */
export function computeCampusLayout(
  site: Pick<Site, 'footprint_ha' | 'radius_km'>,
  scenario: string,
  temporal: string,
  elevation?: ElevationGridPayload | null
): CampusLayout {
  if (temporal === 'present') {
    return {
      visible: false,
      padWidth: 0,
      padDepth: 0,
      padThickness: 0,
      padY: 0,
      metricPadWidth: 0,
      halls: [],
      mitigationTrees: [],
      solarCanopy: null,
    };
  }
  const radiusKm = sanitizeRadiusKm(site.radius_km);
  const footprintHa = Math.max(0.25, Number(site.footprint_ha) || 10);
  const footprintSideKm = Math.sqrt(footprintHa * 0.01);
  const scenarioFactor = scenario === 'high-growth' ? 1.35 : scenario === 'mitigation' ? 0.9 : 1.0;
  const unitsPerKm = unitsPerKmForRadius(radiusKm);
  const metricPadWidth = footprintSideKm * unitsPerKm * scenarioFactor;
  const padWidth = Math.min(MAX_PAD_WIDTH, Math.max(MIN_PAD_WIDTH, metricPadWidth));
  const padDepth = padWidth * 0.78;
  const padThickness = 0.12;

  // Sample pad footprint corners + center so the graded pad never sinks below sloped DEM ground
  const sampleOffsets = [
    [0, 0],
    [-padWidth * 0.45, -padDepth * 0.45],
    [padWidth * 0.45, -padDepth * 0.45],
    [-padWidth * 0.45, padDepth * 0.45],
    [padWidth * 0.45, padDepth * 0.45],
  ];
  let padBaseY = terrainHeightAt(0, 0, elevation, radiusKm);
  for (const [ox, oz] of sampleOffsets) {
    padBaseY = Math.max(padBaseY, terrainHeightAt(ox, oz, elevation, radiusKm));
  }
  const padY = padBaseY + padThickness / 2;

  const hallCount: number = scenario === 'high-growth' ? 4 : 3;
  const usableSpanX = padWidth * 0.76;
  const hallWidth = (usableSpanX / hallCount) * 0.78;
  const hallDepth = padDepth * 0.52;
  const baseScale = Math.max(0.55, Math.min(1.4, padWidth * 0.35));
  const hallHeight = (temporal === 'future' ? 1.15 : 0.78) * baseScale;

  const halls: HallPlacement[] = [];
  for (let i = 0; i < hallCount; i++) {
    const t = hallCount === 1 ? 0 : i / (hallCount - 1) - 0.5;
    const hx = t * (usableSpanX - hallWidth);
    const hz = 0;
    const groundY = Math.max(padBaseY, terrainHeightAt(hx, hz, elevation, radiusKm));
    halls.push({
      x: hx,
      y: groundY + padThickness + hallHeight / 2,
      z: hz,
      width: hallWidth,
      height: hallHeight,
      depth: hallDepth,
    });
  }

  const mitigationTrees: CampusLayout['mitigationTrees'] = [];
  let solarCanopy: CampusLayout['solarCanopy'] = null;
  if (scenario === 'mitigation') {
    const treeCountPerEdge = 6;
    const treeRadius = Math.max(0.11, Math.min(0.26, padWidth * 0.055));
    const treeHeight = treeRadius * 3.1;
    for (const zSign of [-1, 1]) {
      const tz = zSign * padDepth * 0.42;
      for (let i = 0; i < treeCountPerEdge; i++) {
        const tx = ((i / (treeCountPerEdge - 1)) - 0.5) * padWidth * 0.84;
        const groundY = Math.max(padBaseY, terrainHeightAt(tx, tz, elevation, radiusKm));
        mitigationTrees.push({
          x: tx,
          y: groundY + padThickness + treeHeight / 2,
          z: tz,
          radius: treeRadius,
          height: treeHeight,
        });
      }
    }
    const maxHallTop = halls.reduce((m, h) => Math.max(m, h.y + h.height / 2), padBaseY + padThickness);
    solarCanopy = {
      x: 0,
      y: maxHallTop + 0.08,
      z: 0,
      width: padWidth * 0.72,
      depth: hallDepth * 0.78,
      tiltX: -Math.PI / 12,
    };
  }

  return {
    visible: true,
    padWidth,
    padDepth,
    padThickness,
    padY,
    metricPadWidth,
    halls,
    mitigationTrees,
    solarCanopy,
  };
}
