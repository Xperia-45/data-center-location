/**
 * Scene-geography tests: the 3D terrain must drape the SAME projected geography the 2D sheet
 * draws, and must not invent anything when the DEM is missing.
 *
 * DATA PROVENANCE: synthetic coordinates and a synthetic DEM. No provider is contacted.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DRAPE_OFFSET,
  SCENE_LINE_WIDTHS,
  graticulePoints,
  projectRingToScene,
  ribbon,
  sceneGeographyFromCartography,
  triangulateFan,
} from '../src/lib/sceneGeography.ts';
import { ANALYSIS_RADIUS_UNITS, projectLatLonToScene, unitsPerKmForRadius } from '../src/lib/timescapeMath.ts';

const SITE = { lat: 30.901, lon: 75.8573, radius_km: 5 };

/** A gentle synthetic DEM: 100 m in the north row, 120 m in the south row. */
const DEM = {
  status: 'ok' as const,
  lat: SITE.lat,
  lon: SITE.lon,
  radius_km: 5,
  grid_size: 3,
  span_km: 12.5,
  values_m: [
    [100, 100, 100],
    [110, 110, 110],
    [120, 120, 120],
  ],
  min_m: 100,
  max_m: 120,
  center_m: 110,
  data_class: 'observation_derived',
};

const kmPerDegLat = 111.32;

test('a projected ring follows the terrain and sits just above it', () => {
  // A short east-west line 1 km north of the site.
  const northLat = SITE.lat + 1 / kmPerDegLat;
  const coords: [number, number][] = [
    [SITE.lon - 0.005, northLat],
    [SITE.lon + 0.005, northLat],
  ];
  const ring = projectRingToScene(coords, SITE, DEM)!;
  assert.equal(ring.length, 2);
  const expected = projectLatLonToScene(northLat, SITE.lon, SITE, DEM);
  for (const p of ring) {
    assert.ok(p.y > expected.y, 'draped above the surface');
    assert.ok(Math.abs(p.y - (expected.y + DRAPE_OFFSET)) < 1e-9, 'by exactly the drape offset');
  }
  assert.ok(ring[0].x < 0 && ring[1].x > 0, 'west point is -X, east point is +X');
  assert.ok(ring[0].z < 0, 'north is -Z');
});

test('the same projection is used as the rest of the scene, so 2D and 3D cannot disagree', () => {
  const lat = SITE.lat + 0.01;
  const lon = SITE.lon + 0.01;
  const ring = projectRingToScene([[lon, lat], [lon + 0.001, lat]], SITE, DEM)!;
  const direct = projectLatLonToScene(lat, lon, SITE, DEM);
  assert.ok(Math.abs(ring[0].x - direct.x) < 1e-9);
  assert.ok(Math.abs(ring[0].z - direct.z) < 1e-9);
});

test('a ring with no DEM is draped on the flat reference plane at y = 0', () => {
  const coords: [number, number][] = [[SITE.lon - 0.01, SITE.lat], [SITE.lon + 0.01, SITE.lat]];
  const flat = projectRingToScene(coords, SITE, null)!;
  for (const p of flat) assert.equal(p.y, DRAPE_OFFSET, 'flat plane plus drape offset, never a fake hill');
  const unavailable = projectRingToScene(coords, SITE, { status: 'unavailable', reason: 'rate_limited' } as any)!;
  for (const p of unavailable) assert.equal(p.y, DRAPE_OFFSET);
});

test('features outside the scene bounds are dropped rather than drawn off the sheet', () => {
  const far = SITE.lat + 0.9; // ~100 km north
  const ring = projectRingToScene([[SITE.lon, far], [SITE.lon + 0.01, far]], SITE, DEM);
  assert.equal(ring, null);
  assert.equal(projectRingToScene([[SITE.lon, SITE.lat]], SITE, DEM), null, 'a single point is not a line');
  assert.equal(projectRingToScene([], SITE, DEM), null);
});

test('a triangle fan covers an n-gon with n-2 triangles', () => {
  const square = [
    { x: -1, y: 0, z: -1 },
    { x: 1, y: 0, z: -1 },
    { x: 1, y: 0, z: 1 },
    { x: -1, y: 0, z: 1 },
  ];
  const tris = triangulateFan(square);
  assert.equal(tris.length, 2 * 3 * 3, 'two triangles, three vertices, three components');
  assert.deepEqual(triangulateFan(square.slice(0, 2)), []);
});

test('a ribbon gives every segment a constant perpendicular width', () => {
  const line = [
    { x: 0, y: 0, z: 0 },
    { x: 10, y: 0, z: 0 },
  ];
  const verts = ribbon(line, 0.5);
  assert.equal(verts.length, 6 * 3, 'two triangles = six vertices = eighteen numbers');
  const zs = verts.filter((_, i) => i % 3 === 2);
  assert.ok(zs.every((z) => Math.abs(Math.abs(z) - 0.5) < 1e-9), 'offset is perpendicular to an east-west line');
  assert.deepEqual(ribbon([line[0]], 0.5), [], 'a single point cannot be ribboned');
  assert.deepEqual(ribbon(line, 0), [], 'zero width draws nothing');
});

test('the graticule is whole-kilometre spacing inside the analysis circle', () => {
  const lines = graticulePoints(5, 1);
  const unitsPerKm = unitsPerKmForRadius(5);
  assert.ok(lines.length > 0);
  for (const line of lines) {
    assert.equal(line.length, 2);
    for (const p of line) {
      assert.ok(Math.abs(p.x) <= ANALYSIS_RADIUS_UNITS + 1e-9);
      assert.ok(Math.abs(p.z) <= ANALYSIS_RADIUS_UNITS + 1e-9);
    }
  }
  // Every graticule offset must be a whole number of kilometres in scene units.
  const offsets = new Set(lines.map((l) => Math.abs(l[0].x) || Math.abs(l[0].z)));
  for (const offset of offsets) {
    const km = offset / unitsPerKm;
    assert.ok(Math.abs(km - Math.round(km)) < 1e-9, `${offset} units = ${km} km`);
  }
});

test('cartography is draped into areas and lines with the right display widths', () => {
  const layers = {
    water_area: {
      collection: {
        features: [{
          id: 'way/1', properties: { name: 'Pond' },
          geometry: { type: 'Polygon', coordinates: [[
            [SITE.lon - 0.01, SITE.lat - 0.01],
            [SITE.lon + 0.01, SITE.lat - 0.01],
            [SITE.lon + 0.01, SITE.lat + 0.01],
            [SITE.lon - 0.01, SITE.lat - 0.01],
          ]] },
        }],
      },
    },
    waterway: {
      collection: {
        features: [
          {
            id: 'way/2', properties: { name: 'Sutlej', osm_tags: { waterway: 'river' }, intermittent: false },
            geometry: { type: 'LineString', coordinates: [[SITE.lon - 0.02, SITE.lat], [SITE.lon + 0.02, SITE.lat]] },
          },
          {
            id: 'way/3', properties: { name: 'Choe', osm_tags: { waterway: 'stream' }, intermittent: true },
            geometry: { type: 'LineString', coordinates: [[SITE.lon, SITE.lat - 0.02], [SITE.lon, SITE.lat + 0.02]] },
          },
        ],
      },
    },
    road: {
      collection: {
        features: [{
          id: 'way/4', properties: { name: 'NH 44', road_rank: 5 },
          geometry: { type: 'LineString', coordinates: [[SITE.lon - 0.02, SITE.lat + 0.01], [SITE.lon + 0.02, SITE.lat + 0.01]] },
        }],
      },
    },
    label: { collection: { features: [] } },
  };

  const result = sceneGeographyFromCartography({ layers, site: SITE, elevation: DEM });
  assert.equal(result.areas.length, 1);
  assert.equal(result.areas[0].class, 'water_area');
  assert.equal(result.areas[0].name, 'Pond');
  assert.ok(result.areas[0].triangles.length > 0);

  const river = result.lines.find((l) => l.id === 'way/2')!;
  const stream = result.lines.find((l) => l.id === 'way/3')!;
  const road = result.lines.find((l) => l.id === 'way/4')!;
  assert.ok(Math.abs(river.halfWidth - SCENE_LINE_WIDTHS.waterway_major / 2) < 1e-9);
  assert.ok(Math.abs(stream.halfWidth - SCENE_LINE_WIDTHS.waterway_minor / 2) < 1e-9);
  assert.ok(Math.abs(road.halfWidth - SCENE_LINE_WIDTHS.road_major / 2) < 1e-9);
  assert.equal(stream.intermittent, true, 'seasonal drainage stays flagged in 3D too');
  assert.equal(result.skipped, 0);
});

test('the 3D scene still draws mapped geography when the DEM is unavailable', () => {
  const layers = {
    waterway: {
      collection: {
        features: [{
          id: 'way/9', properties: { name: 'River', osm_tags: { waterway: 'river' } },
          geometry: { type: 'LineString', coordinates: [[SITE.lon - 0.02, SITE.lat], [SITE.lon + 0.02, SITE.lat]] },
        }],
      },
    },
  };
  const result = sceneGeographyFromCartography({ layers, site: SITE, elevation: null });
  assert.equal(result.lines.length, 1, 'a missing DEM must not blank the geography');
  const ys = result.lines[0].vertices.filter((_, i) => i % 3 === 1);
  assert.ok(ys.every((y) => Math.abs(y - DRAPE_OFFSET) < 1e-9), 'draped on the flat reference plane');
});

test('geometry that cannot be draped is counted as skipped, never faked', () => {
  const layers = {
    water_area: {
      collection: {
        features: [
          { id: 'bad/1', properties: {}, geometry: { type: 'Point', coordinates: [SITE.lon, SITE.lat] } },
          { id: 'bad/2', properties: {}, geometry: null },
        ],
      },
    },
  };
  const result = sceneGeographyFromCartography({ layers, site: SITE, elevation: DEM });
  assert.equal(result.areas.length, 0);
  assert.equal(result.skipped, 2);
});
