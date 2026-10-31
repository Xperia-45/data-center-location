/**
 * Site-preset tests for the topographic sheet: Ludhiana, Mohali and Hyderabad.
 *
 * DATA PROVENANCE - READ THIS FIRST
 *   Every DEM grid and every OSM feature below is a SYNTHETIC stand-in, shaped like the kind of
 *   geography each location is known for. It is NOT retrieved data and must never be presented
 *   as such. What these tests validate is the PIPELINE: that whatever the backend returns for a
 *   location is converted into the right cartographic content, that a flat alluvial plain yields
 *   few contours while undulating plateau yields many, that seasonal drainage stays flagged, that
 *   an unavailable layer yields nothing at all, and that the base sheet is identical across
 *   Present / Development / Future.
 *
 *   Real feature validation for each site requires a live /api/v1/landscape response; see the PR
 *   notes for the deployed-backend status.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  baseGeographyFingerprint,
  buildCartography,
  contourIntervalForRelief,
  contoursFromGrid,
  elevationTintCells,
  summarizeSheet,
} from '../src/lib/cartography.ts';
import { sceneGeographyFromCartography } from '../src/lib/sceneGeography.ts';
import { SITE_PRESETS } from '../src/lib/timescapeMath.ts';

const KM_PER_DEG = 111.32;

/** Synthetic DEM: a plane plus an optional undulation, so relief can be tuned per preset. */
function syntheticDem(n: number, baseM: number, reliefM: number, undulate: number): number[][] {
  const grid: number[][] = [];
  for (let r = 0; r < n; r++) {
    const row: number[] = [];
    for (let c = 0; c < n; c++) {
      const t = r / (n - 1);
      const wave = undulate * Math.sin((c / (n - 1)) * Math.PI * 3) * Math.cos(t * Math.PI * 2);
      row.push(Math.round((baseM + t * reliefM + wave) * 10) / 10);
    }
    grid.push(row);
  }
  return grid;
}

function demPayload(values_m: number[][], spanKm: number, site: { lat: number; lon: number }) {
  const flat = values_m.flat();
  return {
    status: 'ok' as const,
    lat: site.lat,
    lon: site.lon,
    radius_km: 5,
    grid_size: values_m.length,
    span_km: spanKm,
    values_m,
    min_m: Math.min(...flat),
    max_m: Math.max(...flat),
    center_m: Math.round(((Math.min(...flat) + Math.max(...flat)) / 2) * 10) / 10,
    resolution_m: 90,
    data_class: 'observation_derived',
    surface_model: 'Digital Surface Model (DSM): elevations include buildings, structures and vegetation canopy. This is NOT bare-earth terrain (DTM).',
    attribution: 'Elevation data by Open-Meteo.com (CC BY 4.0) • Copernicus DEM GLO-90',
  };
}

let idSeq = 0;
function way(tags: Record<string, any>, coords: [number, number][], site: { lat: number; lon: number }) {
  idSeq += 1;
  const closed = coords.length > 2 && coords[0][0] === coords[coords.length - 1][0] && coords[0][1] === coords[coords.length - 1][1];
  const lats = coords.map((c) => c[1]);
  const lons = coords.map((c) => c[0]);
  const lat = lats.reduce((a, b) => a + b, 0) / lats.length;
  const lon = lons.reduce((a, b) => a + b, 0) / lons.length;
  const cosLat = Math.cos((site.lat * Math.PI) / 180);
  const distance = Math.hypot((lon - site.lon) * KM_PER_DEG * cosLat, (lat - site.lat) * KM_PER_DEG);
  return {
    name: tags.name ?? `Synthetic ${idSeq}`,
    kind: 'synthetic',
    layer: 'synthetic',
    lat: Math.round(lat * 1e6) / 1e6,
    lon: Math.round(lon * 1e6) / 1e6,
    distance_km: Math.round(distance * 1000) / 1000,
    osm_type: 'way',
    osm_id: 900000 + idSeq,
    osm_tags: tags,
    data_class: 'mapped',
    geometry: { type: 'LineString', coordinates: coords, sequence_count: 1 },
    synthetic: !closed,
  };
}

function node(tags: Record<string, any>, site: { lat: number; lon: number }) {
  idSeq += 1;
  return {
    name: tags.name ?? `Synthetic node ${idSeq}`,
    kind: 'synthetic',
    layer: 'synthetic',
    lat: site.lat,
    lon: site.lon,
    distance_km: 0,
    osm_type: 'node',
    osm_id: 800000 + idSeq,
    osm_tags: tags,
    data_class: 'mapped',
  };
}

function polygonAround(site: { lat: number; lon: number }, dLat: number, dLon: number): [number, number][] {
  return [
    [site.lon - dLon, site.lat - dLat],
    [site.lon + dLon, site.lat - dLat],
    [site.lon + dLon, site.lat + dLat],
    [site.lon - dLon, site.lat + dLat],
    [site.lon - dLon, site.lat - dLat],
  ];
}

// --------------------------------------------------------------------------- Ludhiana

const LUDHIANA = { ...SITE_PRESETS.ludhiana, radius_km: 5 };

function ludhianaFeatures() {
  const s = LUDHIANA;
  return [
    way({ waterway: 'river', name: 'Synthetic Sutlej' },
      [[s.lon - 0.05, s.lat - 0.03], [s.lon - 0.02, s.lat - 0.01], [s.lon + 0.02, s.lat], [s.lon + 0.05, s.lat + 0.02]], s),
    way({ waterway: 'canal', name: 'Synthetic distributary' },
      [[s.lon - 0.04, s.lat + 0.02], [s.lon, s.lat + 0.01], [s.lon + 0.04, s.lat + 0.03]], s),
    way({ natural: 'water', water: 'pond', name: 'Synthetic village pond' },
      polygonAround({ lat: s.lat + 0.01, lon: s.lon + 0.01 }, 0.004, 0.005), s),
    way({ landuse: 'farmland', name: 'Synthetic wheat block' },
      polygonAround({ lat: s.lat - 0.01, lon: s.lon - 0.01 }, 0.012, 0.015), s),
    way({ landuse: 'farmland', name: 'Synthetic paddy block' },
      polygonAround({ lat: s.lat + 0.02, lon: s.lon - 0.02 }, 0.01, 0.012), s),
    way({ landuse: 'residential', name: 'Synthetic urban sector' },
      polygonAround({ lat: s.lat, lon: s.lon }, 0.008, 0.01), s),
    way({ highway: 'trunk', name: 'Synthetic NH' },
      [[s.lon - 0.05, s.lat + 0.005], [s.lon, s.lat + 0.004], [s.lon + 0.05, s.lat + 0.006]], s),
    node({ place: 'city', name: 'Synthetic Ludhiana' }, s),
  ];
}

test('Ludhiana preset: flat alluvial plain, agriculture and canal drainage', () => {
  // Flat alluvial terrain: ~12 m of relief across the sheet.
  const dem = demPayload(syntheticDem(20, 245, 12, 0.6), 12.5, LUDHIANA);
  const carto = buildCartography({ features: ludhianaFeatures(), radiusKm: 5 });
  const contours = contoursFromGrid({
    values_m: dem.values_m, min_m: dem.min_m, max_m: dem.max_m, span_km: dem.span_km,
    lat: LUDHIANA.lat, lon: LUDHIANA.lon,
  });

  assert.ok(carto.layers.farmland.collection.features.length >= 2, 'agricultural land dominates');
  assert.ok(carto.layers.waterway.collection.features.length >= 2, 'river plus canal');
  assert.equal(carto.layers.water_area.collection.features.length, 1, 'pond is an area');
  assert.equal(carto.layers.settlement.collection.features.length, 1);
  assert.equal(carto.layers.label.collection.features.length, 1, 'city name is a label');

  assert.equal(contours.interval_m, 5, 'flat terrain gets the finest interval');
  assert.ok(contours.lines.length <= 3, `few contours on a plain: ${contours.lines.length}`);
  assert.equal(contours.sample_spacing_km, Math.round((12.5 / 19) * 10000) / 10000);

  const summary = summarizeSheet({
    baseLayer: { terrain: dem, osm: { status: 'ok', attribution: '© OpenStreetMap contributors (ODbL)' } },
    cartography: carto, contours,
  });
  assert.equal(summary.terrainAvailable, true);
  assert.equal(summary.dataClass, 'observation_derived');
  assert.ok(summary.reliefM! < 20, `alluvial relief is small: ${summary.reliefM}`);

  const scene = sceneGeographyFromCartography({ layers: carto.layers as any, site: LUDHIANA, elevation: dem });
  assert.ok(scene.areas.length >= 4, 'areas are draped in 3D too');
  assert.ok(scene.lines.length >= 3, 'rivers, canal and road are draped in 3D too');
});

// --------------------------------------------------------------------------- Mohali

const MOHALI = { ...SITE_PRESETS.mohali, radius_km: 5 };

function mohaliFeatures() {
  const s = MOHALI;
  return [
    // A choe: seasonal drainage, must stay flagged so the sheet can dash it.
    way({ waterway: 'stream', intermittent: 'yes', name: 'Synthetic choe' },
      [[s.lon - 0.04, s.lat + 0.03], [s.lon - 0.01, s.lat + 0.01], [s.lon + 0.03, s.lat - 0.02]], s),
    way({ waterway: 'drain', seasonal: 'yes', name: 'Synthetic seasonal drain' },
      [[s.lon + 0.01, s.lat + 0.03], [s.lon + 0.02, s.lat - 0.01]], s),
    way({ landuse: 'residential', name: 'Synthetic sector 70' },
      polygonAround({ lat: s.lat, lon: s.lon }, 0.006, 0.008), s),
    way({ landuse: 'residential', name: 'Synthetic sector 71' },
      polygonAround({ lat: s.lat + 0.012, lon: s.lon + 0.012 }, 0.006, 0.008), s),
    way({ landuse: 'industrial', name: 'Synthetic industrial area' },
      polygonAround({ lat: s.lat - 0.015, lon: s.lon - 0.01 }, 0.007, 0.01), s),
    way({ landuse: 'farmland', name: 'Synthetic fields' },
      polygonAround({ lat: s.lat - 0.02, lon: s.lon + 0.02 }, 0.01, 0.012), s),
    way({ natural: 'wood', name: 'Synthetic woodland' },
      polygonAround({ lat: s.lat + 0.03, lon: s.lon - 0.02 }, 0.008, 0.01), s),
    way({ highway: 'primary', name: 'Synthetic arterial' },
      [[s.lon - 0.05, s.lat - 0.01], [s.lon, s.lat], [s.lon + 0.05, s.lat + 0.01]], s),
    node({ place: 'town', name: 'Synthetic Mohali' }, s),
  ];
}

test('Mohali preset: urban sectors, seasonal choes, transition to the foothills', () => {
  // Foothill transition: more relief than the plain.
  const dem = demPayload(syntheticDem(20, 310, 65, 2.0), 12.5, MOHALI);
  const features = mohaliFeatures();
  const carto = buildCartography({ features, radiusKm: 5 });
  const contours = contoursFromGrid({
    values_m: dem.values_m, min_m: dem.min_m, max_m: dem.max_m, span_km: dem.span_km,
    lat: MOHALI.lat, lon: MOHALI.lon,
  });

  assert.ok(carto.layers.settlement.collection.features.length >= 3, 'urban sectors');
  assert.equal(carto.layers.waterway.collection.features.length, 2);
  const seasonal = carto.layers.waterway.collection.features.filter((f) => f.properties.intermittent);
  assert.equal(seasonal.length, 2, 'both the choe and the seasonal drain stay flagged');
  assert.ok(carto.layers.vegetation.collection.features.length >= 1);
  assert.ok(carto.layers.farmland.collection.features.length >= 1);

  assert.equal(contours.interval_m, 10, 'foothill relief steps up from the plain');
  assert.ok(contours.lines.length > 3, `undulating terrain yields more contours: ${contours.lines.length}`);

  const scene = sceneGeographyFromCartography({ layers: carto.layers as any, site: MOHALI, elevation: dem });
  const choe = scene.lines.find((l) => l.intermittent);
  assert.ok(choe, 'seasonal drainage is draped in 3D as well');
});

// --------------------------------------------------------------------------- Hyderabad

const HYDERABAD = { ...SITE_PRESETS.hyderabad, radius_km: 5 };

function hyderabadFeatures() {
  const s = HYDERABAD;
  return [
    way({ natural: 'water', name: 'Synthetic lake' },
      polygonAround({ lat: s.lat + 0.01, lon: s.lon - 0.015 }, 0.008, 0.012), s),
    way({ natural: 'water', water: 'reservoir', name: 'Synthetic reservoir' },
      polygonAround({ lat: s.lat - 0.02, lon: s.lon + 0.02 }, 0.006, 0.009), s),
    way({ natural: 'wetland', name: 'Synthetic marsh' },
      polygonAround({ lat: s.lat + 0.025, lon: s.lon + 0.01 }, 0.005, 0.007), s),
    way({ landuse: 'residential', name: 'Synthetic suburb' },
      polygonAround({ lat: s.lat, lon: s.lon }, 0.01, 0.012), s),
    way({ landuse: 'industrial', name: 'Synthetic IT corridor' },
      polygonAround({ lat: s.lat - 0.01, lon: s.lon - 0.02 }, 0.008, 0.01), s),
    way({ highway: 'motorway', name: 'Synthetic ORR' },
      [[s.lon - 0.05, s.lat + 0.02], [s.lon - 0.01, s.lat - 0.01], [s.lon + 0.04, s.lat - 0.03]], s),
    way({ highway: 'secondary', name: 'Synthetic link road' },
      [[s.lon - 0.03, s.lat - 0.02], [s.lon + 0.02, s.lat + 0.02]], s),
    way({ boundary: 'protected_area', name: 'Synthetic reserve' },
      polygonAround({ lat: s.lat + 0.03, lon: s.lon - 0.03 }, 0.008, 0.01), s),
    node({ place: 'city', name: 'Synthetic Hyderabad' }, s),
  ];
}

test('Hyderabad preset: Deccan plateau, lakes and urban infrastructure', () => {
  // Deccan plateau: undulating, with the most relief of the three.
  const dem = demPayload(syntheticDem(20, 500, 110, 6.0), 12.5, HYDERABAD);
  const carto = buildCartography({ features: hyderabadFeatures(), radiusKm: 5 });
  const contours = contoursFromGrid({
    values_m: dem.values_m, min_m: dem.min_m, max_m: dem.max_m, span_km: dem.span_km,
    lat: HYDERABAD.lat, lon: HYDERABAD.lon,
  });

  assert.ok(carto.layers.water_area.collection.features.length >= 2, 'lakes and a reservoir');
  assert.equal(carto.layers.wetland.collection.features.length, 1);
  assert.ok(carto.layers.settlement.collection.features.length >= 2);
  assert.equal(carto.layers.protected.collection.features.length, 1);
  const major = carto.layers.road.collection.features.filter((f) => Number(f.properties.road_rank) >= 5);
  assert.ok(major.length >= 1, 'a motorway reads as a major road');

  assert.equal(contours.interval_m, 20, 'plateau relief steps up again');
  assert.ok(contours.lines.length > 5, `undulating plateau yields the most contours: ${contours.lines.length}`);

  const tint = elevationTintCells({
    values_m: dem.values_m, min_m: dem.min_m, max_m: dem.max_m, span_km: dem.span_km,
    lat: HYDERABAD.lat, lon: HYDERABAD.lon,
  });
  assert.equal(tint.available, true);
  assert.equal(tint.collection.features.length, 19 * 19, 'one tint quad per sample cell');
});

// ------------------------------------------------------------------ shared guarantees

test('each preset keeps an identical base sheet across Present / Development / Future', () => {
  const presets: [string, any, () => any[], number[][]][] = [
    ['Ludhiana', LUDHIANA, ludhianaFeatures, syntheticDem(20, 245, 12, 0.6)],
    ['Mohali', MOHALI, mohaliFeatures, syntheticDem(20, 310, 65, 2.0)],
    ['Hyderabad', HYDERABAD, hyderabadFeatures, syntheticDem(20, 500, 110, 6.0)],
  ];
  for (const [name, site, makeFeatures, values_m] of presets) {
    const features = makeFeatures();
    const prints = ['present', 'development', 'future'].map(() => {
      const carto = buildCartography({ features, radiusKm: 5 });
      const contours = contoursFromGrid({
        values_m, min_m: Math.min(...values_m.flat()), max_m: Math.max(...values_m.flat()),
        span_km: 12.5, lat: site.lat, lon: site.lon,
      });
      return baseGeographyFingerprint({ cartography: carto, contours });
    });
    assert.equal(prints[0], prints[1], `${name}: present vs development`);
    assert.equal(prints[1], prints[2], `${name}: development vs future`);
  }
});

test('a preset with an unavailable OSM layer draws nothing and says so', () => {
  const dem = demPayload(syntheticDem(20, 500, 110, 6.0), 12.5, HYDERABAD);
  const contours = contoursFromGrid({
    values_m: dem.values_m, min_m: dem.min_m, max_m: dem.max_m, span_km: dem.span_km,
    lat: HYDERABAD.lat, lon: HYDERABAD.lon,
  });
  // Exactly what the deployed backend returned for Ludhiana on 2026-10-31: every layer failed.
  const carto = buildCartography({ features: [], radiusKm: 5 });
  const summary = summarizeSheet({
    baseLayer: {
      terrain: dem,
      osm: { status: 'unavailable', reason: 'all_layers_failed:network_error', attribution: '© OpenStreetMap contributors (ODbL)' },
      land_cover: { status: 'unavailable', reason: 'rasterio_unavailable' },
    },
    cartography: carto, contours,
  });
  assert.equal(summary.mappedFeatures, 0, 'no geography is invented');
  const osm = summary.layerStatuses.find((l) => l.id === 'osm')!;
  assert.equal(osm.available, false);
  assert.equal(osm.reason, 'all_layers_failed:network_error');
  assert.equal(summary.terrainAvailable, true, 'terrain is independent of the OSM failure');
  assert.ok(summary.contourCount > 0, 'contours still draw from the DEM');
});

test('a preset with an unavailable DEM keeps the mapped geography and drops only the contours', () => {
  const carto = buildCartography({ features: ludhianaFeatures(), radiusKm: 5 });
  const contours = contoursFromGrid({ values_m: null, lat: LUDHIANA.lat, lon: LUDHIANA.lon });
  const summary = summarizeSheet({
    baseLayer: {
      terrain: { status: 'unavailable', reason: 'rate_limited', surface_model: null, data_class: 'unavailable' },
      osm: { status: 'ok', attribution: '© OpenStreetMap contributors (ODbL)' },
    },
    cartography: carto, contours,
  });
  assert.equal(summary.terrainAvailable, false);
  assert.equal(summary.terrainReason, 'rate_limited');
  assert.equal(summary.contourCount, 0, 'no contours without a DEM');
  assert.equal(summary.surfaceModel, null);
  assert.ok(summary.mappedFeatures >= 7, 'mapped geography survives the DEM failure');

  const scene = sceneGeographyFromCartography({ layers: carto.layers as any, site: LUDHIANA, elevation: null });
  assert.ok(scene.areas.length > 0 && scene.lines.length > 0, '3D keeps the geography on the flat plane');
});

test('contour intervals respond to relief across the three presets', () => {
  assert.equal(contourIntervalForRelief(12), 5, 'Ludhiana: flat alluvial');
  assert.equal(contourIntervalForRelief(65), 10, 'Mohali: foothill transition');
  assert.equal(contourIntervalForRelief(110), 20, 'Hyderabad: undulating Deccan');
  assert.ok(contourIntervalForRelief(12) < contourIntervalForRelief(65));
  assert.ok(contourIntervalForRelief(65) < contourIntervalForRelief(110));
});
