/**
 * Drapes the mapped geography onto the Three.js terrain using exactly the same projected
 * coordinates the 2D sheet draws, so the two views can never disagree about where a river is.
 *
 * These functions are pure and Three.js-free: they return flat xyz arrays that the component
 * turns into BufferGeometry. That keeps the projection testable without a WebGL context.
 *
 * Nothing here invents geography. When the DEM is unavailable, `terrainHeightAt` returns 0 and
 * every feature is draped on the explicitly labelled flat reference plane - the features are
 * still the real mapped ones, and no elevation is fabricated for them.
 */
import {
  type ElevationGridPayload,
  SCENE_MAX_COORD,
  projectLatLonToScene,
  sanitizeRadiusKm,
  unitsPerKmForRadius,
} from './timescapeMath.ts';

export interface ScenePoint { x: number; y: number; z: number }

export interface SceneSite {
  lat: number;
  lon: number;
  radius_km: number;
}

/**
 * Cartographic display widths in scene units.
 *
 * These are NOT measured channel or carriageway widths - a 30 m river at a 5 km analysis radius
 * would be a fraction of a pixel. They exist so linear features stay visible, and the UI states
 * that widths are cartographic rather than to scale.
 */
export const SCENE_LINE_WIDTHS: Record<string, number> = {
  waterway_major: 0.5,
  waterway_minor: 0.3,
  road_major: 0.55,
  road_minor: 0.32,
  default: 0.3,
};

/** Lifts draped geometry just above the surface so it is not z-fought by the terrain mesh. */
export const DRAPE_OFFSET = 0.18;

/**
 * Projects a GeoJSON coordinate ring ([lon, lat] pairs) into scene space, following the terrain.
 * Returns null when fewer than two usable points survive.
 */
export function projectRingToScene(
  coords: [number, number][],
  site: SceneSite,
  elevation?: ElevationGridPayload | null,
  yOffset: number = DRAPE_OFFSET
): ScenePoint[] | null {
  if (!Array.isArray(coords) || coords.length < 2) return null;
  const points: ScenePoint[] = [];
  for (const coord of coords) {
    if (!Array.isArray(coord) || coord.length < 2) continue;
    const lon = Number(coord[0]);
    const lat = Number(coord[1]);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    const p = projectLatLonToScene(lat, lon, site, elevation);
    if (!p.inBounds) continue;
    points.push({ x: p.x, y: p.y + yOffset, z: p.z });
  }
  return points.length >= 2 ? points : null;
}

/** Triangle fan over a draped ring: flat [x, y, z, ...] for a BufferGeometry position attribute. */
export function triangulateFan(points: ScenePoint[]): number[] {
  if (points.length < 3) return [];
  const out: number[] = [];
  const anchor = points[0];
  for (let i = 1; i < points.length - 1; i++) {
    const b = points[i];
    const c = points[i + 1];
    out.push(anchor.x, anchor.y, anchor.z, b.x, b.y, b.z, c.x, c.y, c.z);
  }
  return out;
}

/**
 * Ribbon strip along a draped polyline: flat [x, y, z, ...] with two triangles per segment.
 * The offset is perpendicular in the XZ plane, so the ribbon keeps a constant on-screen width.
 */
export function ribbon(points: ScenePoint[], halfWidth: number): number[] {
  if (points.length < 2 || !(halfWidth > 0)) return [];
  const out: number[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i];
    const b = points[i + 1];
    const dx = b.x - a.x;
    const dz = b.z - a.z;
    const len = Math.hypot(dx, dz);
    if (len < 1e-9) continue;
    const nx = (-dz / len) * halfWidth;
    const nz = (dx / len) * halfWidth;
    out.push(
      a.x + nx, a.y, a.z + nz,
      a.x - nx, a.y, a.z - nz,
      b.x + nx, b.y, b.z + nz,
      a.x - nx, a.y, a.z - nz,
      b.x - nx, b.y, b.z - nz,
      b.x + nx, b.y, b.z + nz
    );
  }
  return out;
}

/**
 * Reference graticule at whole-kilometre spacing, used ONLY on the explicit flat fallback so the
 * scene is not a bare plane. It is a scale reference, not terrain, and is labelled as such.
 */
export function graticulePoints(radiusKm: number, spacingKm = 1): ScenePoint[][] {
  const r = sanitizeRadiusKm(radiusKm);
  const unitsPerKm = unitsPerKmForRadius(r);
  const reach = Math.min(SCENE_MAX_COORD, r * unitsPerKm);
  const lines: ScenePoint[][] = [];
  const steps = Math.floor(reach / (spacingKm * unitsPerKm));
  for (let i = -steps; i <= steps; i++) {
    const offset = i * spacingKm * unitsPerKm;
    lines.push([
      { x: -reach, y: 0.02, z: offset },
      { x: reach, y: 0.02, z: offset },
    ]);
    lines.push([
      { x: offset, y: 0.02, z: -reach },
      { x: offset, y: 0.02, z: reach },
    ]);
  }
  return lines;
}

/**
 * Splits a cartography collection into draped areas and draped lines, ready for the 3D scene.
 * Areas keep their class colour; lines get a display width from SCENE_LINE_WIDTHS.
 */
export function sceneGeographyFromCartography(params: {
  layers: Record<string, { collection: { features: any[] } }>;
  site: SceneSite;
  elevation?: ElevationGridPayload | null;
}): {
  areas: { class: string; id: string; name: string | null; triangles: number[] }[];
  lines: { class: string; id: string; name: string | null; halfWidth: number; vertices: number[]; intermittent: boolean }[];
  skipped: number;
} {
  const { layers, site, elevation } = params;
  const areas: { class: string; id: string; name: string | null; triangles: number[] }[] = [];
  const lines: { class: string; id: string; name: string | null; halfWidth: number; vertices: number[]; intermittent: boolean }[] = [];
  let skipped = 0;

  const AREA_CLASSES = ['farmland', 'vegetation', 'settlement', 'protected', 'water_area', 'wetland'];
  for (const cls of AREA_CLASSES) {
    const collection = layers[cls]?.collection;
    if (!collection) continue;
    for (const feature of collection.features) {
      const coords = feature.geometry?.coordinates;
      if (feature.geometry?.type !== 'Polygon' || !Array.isArray(coords?.[0])) { skipped++; continue; }
      const ring = projectRingToScene(coords[0], site, elevation);
      if (!ring) { skipped++; continue; }
      const triangles = triangulateFan(ring);
      if (triangles.length === 0) { skipped++; continue; }
      areas.push({
        class: cls,
        id: String(feature.id ?? `${cls}:${areas.length}`),
        name: (feature.properties?.name as string) ?? null,
        triangles,
      });
    }
  }

  // Rivers and canals read wider than drains; major roads wider than lanes. `widthFor` returns a
  // full display width, so the ribbon is given half of it.
  pushLinesWithWidth(layers, 'waterway', site, elevation, (feature) => {
    const tags = (feature.properties?.osm_tags ?? {}) as Record<string, string>;
    const waterway = String(tags.waterway ?? '').toLowerCase();
    return waterway === 'river' || waterway === 'canal'
      ? SCENE_LINE_WIDTHS.waterway_major
      : SCENE_LINE_WIDTHS.waterway_minor;
  }, lines, () => skipped++);
  pushLinesWithWidth(layers, 'road', site, elevation, (feature) =>
    Number(feature.properties?.road_rank ?? 1) >= 4
      ? SCENE_LINE_WIDTHS.road_major
      : SCENE_LINE_WIDTHS.road_minor,
  lines, () => skipped++);

  return { areas, lines, skipped };
}

function pushLinesWithWidth(
  layers: Record<string, { collection: { features: any[] } }>,
  cls: string,
  site: SceneSite,
  elevation: ElevationGridPayload | null | undefined,
  widthFor: (feature: any) => number,
  lines: { class: string; id: string; name: string | null; halfWidth: number; vertices: number[]; intermittent: boolean }[],
  onSkip: () => void
): void {
  const collection = layers[cls]?.collection;
  if (!collection) return;
  for (const feature of collection.features) {
    const geometry = feature.geometry;
    const coordLists: [number, number][][] =
      geometry?.type === 'LineString' ? [geometry.coordinates] :
      geometry?.type === 'MultiLineString' ? geometry.coordinates :
      [];
    let drew = false;
    for (const coords of coordLists) {
      const projected = projectRingToScene(coords, site, elevation);
      if (!projected) continue;
      const vertices = ribbon(projected, widthFor(feature) / 2);
      if (vertices.length === 0) continue;
      drew = true;
      lines.push({
        class: cls,
        id: String(feature.id ?? `${cls}:${lines.length}`),
        name: (feature.properties?.name as string) ?? null,
        halfWidth: widthFor(feature) / 2,
        vertices,
        intermittent: Boolean(feature.properties?.intermittent),
      });
    }
    if (!drew) onSkip();
  }
}
