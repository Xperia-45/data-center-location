/**
 * Cartography tests for the Environmental Timescape topographic sheet.
 *
 * DATA PROVENANCE: every input below is a synthetic stand-in shaped like a real
 * /api/v1/landscape response. No test contacts a provider, and nothing here is presented as
 * live geography. The point of these tests is the CONVERSION: that mapped tags become the right
 * cartographic class, that nothing is invented when a layer is unavailable, and that the base
 * sheet is identical across temporal scenarios.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CARTO_CLASSES,
  TOPO_PALETTE,
  baseGeographyFingerprint,
  buildCartography,
  classifyFeature,
  computeScaleBar,
  MAX_CONTOURS_PER_SHEET,
  contourIntervalForRelief,
  contourLevels,
  contoursFromGrid,
  elevationAtLatLon,
  elevationTintCells,
  featureIdentity,
  featureToGeometry,
  footprintEnvelope,
  hypsometricStops,
  layerAvailability,
  metersPerPixel,
  niceScaleBarMeters,
  notModelledEffects,
  summarizeSheet,
} from '../src/lib/cartography.ts';

const SITE = { lat: 30.901, lon: 75.8573 };

function ring(lon: number, lat: number, size = 0.01): [number, number][] {
  return [
    [lon, lat], [lon, lat + size], [lon + size, lat + size], [lon + size, lat], [lon, lat],
  ];
}

function osmFeature(over: Record<string, any> = {}): any {
  return {
    name: 'Test feature',
    kind: 'water',
    layer: 'water',
    lat: 30.9,
    lon: 75.85,
    distance_km: 0.5,
    osm_type: 'way',
    osm_id: 1,
    osm_tags: { natural: 'water', name: 'Test feature' },
    data_class: 'mapped',
    geometry: { type: 'LineString', coordinates: ring(75.85, 30.9), sequence_count: 1 },
    ...over,
  };
}

// ----------------------------------------------------------------------- classification

test('classification reads OSM tags, not the backend layer/kind label', () => {
  const cases: [Record<string, any>, string][] = [
    [{ waterway: 'river' }, 'waterway'],
    [{ waterway: 'canal' }, 'waterway'],
    [{ waterway: 'drain' }, 'waterway'],
    [{ waterway: 'riverbank' }, 'water_area'],
    [{ natural: 'water' }, 'water_area'],
    [{ water: 'pond' }, 'water_area'],
    [{ landuse: 'reservoir' }, 'water_area'],
    [{ natural: 'wetland' }, 'wetland'],
    [{ natural: 'wood' }, 'vegetation'],
    [{ landuse: 'forest' }, 'vegetation'],
    [{ landuse: 'farmland' }, 'farmland'],
    [{ landuse: 'residential' }, 'settlement'],
    [{ highway: 'trunk' }, 'road'],
    [{ place: 'city' }, 'label'],
    [{ boundary: 'protected_area' }, 'protected'],
  ];
  for (const [tags, expected] of cases) {
    const cls = classifyFeature(tags);
    assert.equal(cls?.class, expected, `${JSON.stringify(tags)} -> ${cls?.class}`);
  }
  assert.equal(classifyFeature({ amenity: 'cafe' }), null, 'unmapped-for-cartography tags are dropped');
  assert.equal(classifyFeature(null), null);
});

test('the same element from two layer queries is drawn once, hydrology winning', () => {
  // A river polygon tagged both natural=water and landuse=grass arrives from two queries.
  const features = [
    osmFeature({ layer: 'landuse', kind: 'landuse', osm_tags: { natural: 'water', landuse: 'grass', name: 'Pond' } }),
    osmFeature({ layer: 'water', kind: 'water', osm_tags: { natural: 'water', landuse: 'grass', name: 'Pond' } }),
  ];
  const carto = buildCartography({ features });
  assert.equal(carto.deduped, 1);
  assert.equal(carto.layers.water_area.collection.features.length, 1);
  assert.equal(carto.layers.vegetation.collection.features.length, 0);
});

test('seasonal and intermittent drainage is flagged so it can be dashed', () => {
  const cls = classifyFeature({ waterway: 'stream', intermittent: 'yes' });
  assert.equal(cls?.class, 'waterway');
  assert.equal(cls?.intermittent, true);
  const seasonal = classifyFeature({ waterway: 'stream', seasonal: 'yes' });
  assert.equal(seasonal?.intermittent, true);
  assert.equal(classifyFeature({ waterway: 'river' })?.intermittent, false);
});

test('roads carry a rank and places a label rank for paint order', () => {
  assert.equal(classifyFeature({ highway: 'motorway' })?.roadRank, 6);
  assert.equal(classifyFeature({ highway: 'residential' })?.roadRank, 1);
  assert.equal(classifyFeature({ highway: 'footway' })?.roadRank, 1, 'unranked highways still draw');
  assert.equal(classifyFeature({ place: 'city' })?.placeRank, 6);
  assert.equal(classifyFeature({ place: 'hamlet' })?.placeRank, 2);
});

// --------------------------------------------------------------------------- geometry

test('a closed LineString becomes a Polygon, an open one stays a line, a node becomes a point', () => {
  const closed = featureToGeometry(osmFeature());
  assert.equal(closed?.type, 'Polygon');

  const open = featureToGeometry(osmFeature({
    geometry: { type: 'LineString', coordinates: [[75.8, 30.9], [75.85, 30.92], [75.9, 30.94]], sequence_count: 1 },
  }));
  assert.equal(open?.type, 'LineString');

  const node = featureToGeometry(osmFeature({ osm_type: 'node', geometry: undefined }));
  assert.equal(node?.type, 'Point');
  assert.deepEqual((node as any).coordinates, [75.85, 30.9], 'GeoJSON is [lon, lat]');

  assert.equal(featureToGeometry({ osm_tags: {} }), null, 'no coordinates at all yields nothing');
});

test('non-finite coordinates are filtered rather than drawn', () => {
  const geom = featureToGeometry(osmFeature({
    geometry: { type: 'LineString', coordinates: [[75.8, 30.9], [null, 30.9], [75.9, 30.95]], sequence_count: 1 },
  }));
  assert.equal(geom?.type, 'LineString');
  assert.equal((geom as any).coordinates.length, 2);
});

test('features outside the site radius are counted but not drawn', () => {
  const features = [
    osmFeature({ osm_id: 1, distance_km: 1.0 }),
    osmFeature({ osm_id: 2, distance_km: 9.5 }),
  ];
  const carto = buildCartography({ features, radiusKm: 5 });
  assert.equal(carto.outsideRadius, 1);
  assert.equal(carto.layers.water_area.collection.features.length, 1);
});

test('an unavailable or empty OSM layer yields empty collections, never invented features', () => {
  for (const features of [undefined, null, []]) {
    const carto = buildCartography({ features });
    for (const c of CARTO_CLASSES) {
      assert.equal(carto.layers[c].collection.features.length, 0, `${c} must be empty`);
      assert.equal(carto.layers[c].collection.type, 'FeatureCollection');
    }
    assert.equal(carto.total, 0);
  }
});

test('feature identity is stable per OSM element across layers', () => {
  assert.equal(featureIdentity({ osm_type: 'way', osm_id: 42 }), 'way/42');
  assert.equal(
    featureIdentity(osmFeature({ layer: 'water' })),
    featureIdentity(osmFeature({ layer: 'landuse' }))
  );
});

// --------------------------------------------------------------------------- contours

test('contour interval is chosen from the site relief', () => {
  assert.equal(contourIntervalForRelief(12), 5, 'flat alluvial terrain gets the finest interval');
  assert.equal(contourIntervalForRelief(65), 10, 'rising ground steps up');
  assert.equal(contourIntervalForRelief(110), 20);
  assert.equal(contourIntervalForRelief(300), 50);
  assert.equal(contourIntervalForRelief(900), 200);
  assert.equal(contourIntervalForRelief(2500), 500);
  // Never more than about eight contours, whatever the relief.
  for (const relief of [12, 65, 110, 300, 900, 2500, 9000]) {
    const interval = contourIntervalForRelief(relief);
    assert.ok(relief / interval <= MAX_CONTOURS_PER_SHEET + 1, `relief ${relief} -> ${interval}`);
  }
});

test('contour levels fall strictly between the extremes', () => {
  assert.deepEqual(contourLevels(0, 20, 5), [5, 10, 15]);
  assert.deepEqual(contourLevels(243, 251, 20), []);
  assert.deepEqual(contourLevels(250, 250, 5), []);
  assert.deepEqual(contourLevels(0, 10, 0), []);
});

test('contours are derived from the actual sample grid and positioned in lat/lon', () => {
  // A plane rising 10 m per row from north to south, 3x3 samples over a 1 km span.
  const values_m = [
    [0, 0, 0],
    [10, 10, 10],
    [20, 20, 20],
  ];
  const result = contoursFromGrid({ values_m, min_m: 0, max_m: 20, span_km: 1, ...SITE });
  assert.deepEqual(result.levels, [5, 10, 15]);
  assert.equal(result.interval_m, 5);
  assert.equal(result.index_interval_m, 25);
  assert.equal(result.sample_spacing_km, 0.5);
  assert.equal(result.lines.length, 3);

  // The 10 m contour sits exactly on the middle row, i.e. at the site latitude.
  const ten = result.lines.find((l) => l.level === 10)!;
  const lats = ten.geometry.coordinates.flat().map((c) => c[1]);
  assert.ok(lats.every((la) => Math.abs(la - SITE.lat) < 1e-9), `10 m contour on the middle row: ${lats}`);

  // The 5 m contour lies between the north edge and the middle row.
  const five = result.lines.find((l) => l.level === 5)!;
  const fiveLats = five.geometry.coordinates.flat().map((c) => c[1]);
  assert.ok(fiveLats.every((la) => la > SITE.lat && la < SITE.lat + 0.5 / 111.32));

  assert.match(result.note, /does not add native resolution/);
});

test('an unavailable DEM produces no contours and no synthesised surface', () => {
  for (const values_m of [null, undefined, [], [[1]], [[1, 2], [3, NaN]]]) {
    const result = contoursFromGrid({ values_m, min_m: 0, max_m: 10, span_km: 1, ...SITE });
    assert.deepEqual(result.levels, []);
    assert.deepEqual(result.collection.features, [], 'never draw a fake contour');
    assert.equal(result.interval_m, 0);
  }
  const flat = contoursFromGrid({ values_m: [[5, 5], [5, 5]], min_m: 5, max_m: 5, span_km: 1, ...SITE });
  assert.deepEqual(flat.levels, [], 'zero relief has no contours');
});

test('contours span the requested grid extent, not an assumed one', () => {
  const values_m = [
    [100, 100, 100],
    [110, 110, 110],
    [120, 120, 120],
  ];
  const result = contoursFromGrid({ values_m, min_m: 100, max_m: 120, span_km: 4, ...SITE });
  assert.equal(result.sample_spacing_km, 2);
  const lons = result.lines.flatMap((l) => l.geometry.coordinates.flat().map((c) => c[0]));
  const halfSpanDeg = 2 / (111.32 * Math.cos((SITE.lat * Math.PI) / 180));
  assert.ok(Math.max(...lons) <= SITE.lon + halfSpanDeg + 1e-9);
  assert.ok(Math.min(...lons) >= SITE.lon - halfSpanDeg - 1e-9);
});

test('elevation tinting is one quad per sample cell and empty when the DEM is unavailable', () => {
  const values_m = [
    [245, 250, 255],
    [250, 258, 262],
    [255, 262, 266],
  ];
  const tint = elevationTintCells({ values_m, min_m: 245, max_m: 266, span_km: 12.5, ...SITE });
  assert.equal(tint.available, true);
  assert.equal(tint.collection.features.length, 4, 'a 3x3 grid has 2x2 sample cells');
  assert.equal(tint.band_edges_m.length, 7, 'six bands have seven edges');
  assert.equal(tint.band_edges_m[0], 245);
  assert.equal(tint.band_edges_m[6], 266);
  // Cell means are smoothed toward the middle, so the extreme bands need not appear on a coarse
  // grid - but a higher cell must always tint into a higher band.
  const byId = new Map(tint.collection.features.map((f) => [f.id, f]));
  const bandOf = (id: string) => Number(byId.get(id)!.properties.band);
  assert.ok(bandOf('tint:0:0') < bandOf('tint:1:1'), 'tint rises with elevation');
  for (const f of tint.collection.features) {
    const band = Number(f.properties.band);
    assert.ok(band >= 0 && band <= 5, `band ${band} in range`);
    const mean = Number(f.properties.mean_m);
    assert.ok(mean >= tint.band_edges_m[band] && mean <= tint.band_edges_m[band + 1],
      `mean ${mean} sits inside band ${band}`);
  }
  for (const f of tint.collection.features) {
    assert.equal(f.geometry.type, 'Polygon');
    assert.equal((f.geometry as any).coordinates[0].length, 5, 'closed quad');
  }
  for (const values of [null, undefined, [[1, 2], [3, NaN]]]) {
    const empty = elevationTintCells({ values_m: values as any, min_m: 0, max_m: 10, span_km: 1, ...SITE });
    assert.equal(empty.available, false);
    assert.equal(empty.collection.features.length, 0, 'no tint is invented');
  }
});

// ------------------------------------------------------------------------ sheet furniture

test('hypsometric tints are built from the site relief, not a global ramp', () => {
  const stops = hypsometricStops(240, 260);
  assert.equal(stops.length, 6);
  assert.equal(stops[0].stop, 240);
  assert.equal(stops[stops.length - 1].stop, 260);
  assert.deepEqual(hypsometricStops(0, 0), [{ stop: 0, color: TOPO_PALETTE.paper }]);
});

test('scale bar maths rounds to 1/2/5 and reports a usable length', () => {
  assert.ok(metersPerPixel(12, 30.9) > 0);
  assert.equal(niceScaleBarMeters(137), 100);
  assert.equal(niceScaleBarMeters(260), 200);
  assert.equal(niceScaleBarMeters(740), 500);
  assert.equal(niceScaleBarMeters(0), 0);
  const bar = computeScaleBar(12, 30.9, 120);
  assert.ok(bar.pixels > 40 && bar.pixels < 200, `pixels=${bar.pixels}`);
  assert.match(bar.text, /(\d+(\.\d+)? (m|km))/);
});

test('an unavailable layer is reported with its real reason and marked unavailable', () => {
  const base = {
    terrain: { status: 'ok', data_class: 'observation_derived', source: 'Copernicus DEM GLO-90 via Open-Meteo' },
    osm: { status: 'ok', data_class: 'mapped', attribution: '© OpenStreetMap contributors (ODbL)' },
    land_cover: { status: 'unavailable', reason: 'rasterio_unavailable', detail: 'rasterio not installed' },
  };
  const rows = layerAvailability(base);
  assert.equal(rows.length, 3);
  const lc = rows.find((r) => r.id === 'land_cover')!;
  assert.equal(lc.available, false);
  assert.equal(lc.reason, 'rasterio_unavailable');
  assert.equal(rows.find((r) => r.id === 'terrain')!.available, true);
  assert.equal(layerAvailability({}).length, 0, 'a layer the backend never returned is not fabricated');
});

test('the sheet summary keeps provenance, DSM wording and the contour budget', () => {
  const baseLayer = {
    terrain: {
      status: 'ok', data_class: 'observation_derived', min_m: 245.3, max_m: 266.6,
      relief_m: 21.3, resolution_m: 90, span_km: 12.5, grid_size: 20,
      surface_model: 'Digital Surface Model (DSM): elevations include buildings ... NOT bare-earth terrain (DTM).',
    },
    osm: { status: 'ok', attribution: '© OpenStreetMap contributors (ODbL)' },
    land_cover: { status: 'unavailable', reason: 'raster_read_failed', dataset: { epoch: '2021' } },
  };
  const values_m = [
    [245, 250, 255],
    [250, 258, 262],
    [255, 262, 266],
  ];
  const contours = contoursFromGrid({ values_m, min_m: 245.3, max_m: 266.6, span_km: 12.5, ...SITE });
  const carto = buildCartography({ features: [osmFeature()], radiusKm: 5 });
  const summary = summarizeSheet({ baseLayer, cartography: carto, contours });

  assert.equal(summary.terrainAvailable, true);
  assert.equal(summary.dataClass, 'observation_derived');
  assert.match(summary.surfaceModel!, /NOT bare-earth terrain \(DTM\)/);
  assert.equal(summary.resolutionM, 90);
  assert.equal(summary.contourCount, contours.lines.length);
  assert.ok(summary.contourIntervalM > 0);
  assert.equal(summary.mappedFeatures, 1);
  assert.equal(summary.osmAttribution, '© OpenStreetMap contributors (ODbL)');
  assert.equal(summary.landCoverEpoch, '2021');
  assert.equal(summary.layerStatuses.find((l) => l.id === 'land_cover')!.available, false);
});

test('the sheet says terrain is unavailable when the DEM is missing, without dropping the map', () => {
  const baseLayer = {
    terrain: { status: 'unavailable', reason: 'rate_limited' },
    osm: { status: 'ok', attribution: '© OpenStreetMap contributors (ODbL)' },
  };
  const contours = contoursFromGrid({ values_m: null, lat: SITE.lat, lon: SITE.lon });
  const carto = buildCartography({ features: [osmFeature()], radiusKm: 5 });
  const summary = summarizeSheet({ baseLayer, cartography: carto, contours });
  assert.equal(summary.terrainAvailable, false);
  assert.equal(summary.terrainReason, 'rate_limited');
  assert.equal(summary.contourCount, 0);
  assert.equal(summary.surfaceModel, null);
  assert.equal(summary.mappedFeatures, 1, 'mapped geography still renders');
});

// ------------------------------------------------------------- temporal invariance

test('base geography is identical across Present / Development / Future', () => {
  const baseLayer = {
    terrain: { status: 'ok', min_m: 245, max_m: 266, span_km: 12.5, grid_size: 3 },
    osm: { status: 'ok' },
  };
  const values_m = [
    [245, 250, 255],
    [250, 258, 262],
    [255, 262, 266],
  ];
  const features = [
    osmFeature({ osm_id: 1, osm_tags: { natural: 'water', name: 'Pond' } }),
    osmFeature({ osm_id: 2, osm_tags: { waterway: 'river', name: 'Sutlej' },
      geometry: { type: 'LineString', coordinates: [[75.8, 30.9], [75.85, 30.92]], sequence_count: 1 } }),
    osmFeature({ osm_id: 3, osm_tags: { landuse: 'farmland', name: 'Wheat' } }),
  ];
  const fingerprints = ['baseline', 'development', 'future'].map(() => {
    // The scenario only changes development_impact_layer; the base layer is the same object.
    const carto = buildCartography({ features, radiusKm: 5 });
    const contours = contoursFromGrid({ values_m, min_m: 245, max_m: 266, span_km: 12.5, ...SITE });
    return baseGeographyFingerprint({ cartography: carto, contours });
  });
  assert.equal(fingerprints[0], fingerprints[1]);
  assert.equal(fingerprints[1], fingerprints[2]);
  assert.match(fingerprints[0], /contours:/);
  assert.equal(baseLayer.terrain.grid_size, 3, 'sanity: the fixture was not mutated');
});

test('the fingerprint does change when the geography changes, so the test above means something', () => {
  const values_m = [
    [245, 250, 255],
    [250, 258, 262],
    [255, 262, 266],
  ];
  const contours = contoursFromGrid({ values_m, min_m: 245, max_m: 266, span_km: 12.5, ...SITE });
  const a = buildCartography({ features: [osmFeature({ osm_id: 1 })], radiusKm: 5 });
  const b = buildCartography({
    features: [osmFeature({ osm_id: 1 }), osmFeature({ osm_id: 2, osm_tags: { highway: 'trunk' } })],
    radiusKm: 5,
  });
  assert.notEqual(
    baseGeographyFingerprint({ cartography: a, contours }),
    baseGeographyFingerprint({ cartography: b, contours })
  );
});

test('the elevation readout interpolates the real grid and stays silent without a DEM', () => {
  const values_m = [
    [100, 100, 100],
    [110, 110, 110],
    [120, 120, 120],
  ];
  const at = (lat: number, lon: number) =>
    elevationAtLatLon({ lat, lon, values_m, span_km: 1, siteLat: SITE.lat, siteLon: SITE.lon });
  assert.equal(at(SITE.lat, SITE.lon), 110, 'centre sample');
  const north = at(SITE.lat + 0.5 / 111.32, SITE.lon);
  assert.equal(north, 100, 'north edge is the first row');
  assert.equal(at(SITE.lat + 5 / 111.32, SITE.lon), null, 'outside the sheet returns nothing');
  assert.equal(elevationAtLatLon({ ...SITE, siteLat: SITE.lat, siteLon: SITE.lon, values_m: null, span_km: 1 }), null);
});

test('the footprint envelope is a modelled square of the declared area', () => {
  const fp = footprintEnvelope({ lat: SITE.lat, lon: SITE.lon, footprintHa: 10 });
  assert.equal(fp.sideM, 316, 'sqrt(10 ha) ~ 316 m side');
  const poly = fp.collection.features[0].geometry as any;
  assert.equal(poly.coordinates[0].length, 5);
  assert.equal(fp.collection.features[0].properties.data_class, 'modelled');
  assert.match(String(fp.collection.features[0].properties.note), /Not a surveyed boundary/);
  assert.equal(footprintEnvelope({ lat: SITE.lat, lon: SITE.lon, footprintHa: null }).collection.features.length, 0);
});

/**
 * The backend declares eight environmental effects it does NOT model, each with a reason, and
 * publishes the list both top-level on /datasets and nested under `integrity` on /landscape.
 * The frontend read neither, so the app could present an analysis with no groundwater,
 * flood-risk, noise or air-quality component and never say those effects were out of scope -
 * an absence a reader could mistake for a finding of no impact. These tests pin the
 * normalisation: both shapes parse, and nothing is ever invented.
 *
 * The list below is a synthetic stand-in shaped like the real registry, per the provenance note
 * at the top of this file - it is not a statement about what the backend currently declares.
 */
const NOT_MODELLED_DATASETS_SHAPE = {
  schema_version: '3a.1',
  not_modelled_effects: [
    {
      id: 'groundwater_drawdown',
      effect: 'Groundwater drawdown / aquifer response',
      reason: 'Requires hydrogeological survey data and a calibrated groundwater model.',
      status: 'NOT MODELLED',
    },
    {
      id: 'flood_risk_change',
      effect: 'Change in flood risk / surface runoff',
      reason: 'Needs a rainfall-runoff model plus pre- and post-development surface data.',
      status: 'NOT MODELLED',
    },
  ],
};

const NOT_MODELLED_LANDSCAPE_SHAPE = {
  schema_version: '3a.1',
  integrity: { not_modelled_effects: NOT_MODELLED_DATASETS_SHAPE.not_modelled_effects },
};

test('notModelledEffects parses both published shapes identically', () => {
  const fromDatasets = notModelledEffects(NOT_MODELLED_DATASETS_SHAPE);
  const fromLandscape = notModelledEffects(NOT_MODELLED_LANDSCAPE_SHAPE);

  assert.equal(fromDatasets.length, 2);
  assert.deepEqual(fromLandscape, fromDatasets);

  assert.deepEqual(fromDatasets[0], {
    id: 'groundwater_drawdown',
    effect: 'Groundwater drawdown / aquifer response',
    reason: 'Requires hydrogeological survey data and a calibrated groundwater model.',
    status: 'NOT MODELLED',
  });
});

test('notModelledEffects invents nothing for missing or malformed payloads', () => {
  for (const payload of [
    null,
    undefined,
    {},
    { not_modelled_effects: null },
    { not_modelled_effects: {} },
    { not_modelled_effects: 'groundwater' },
    { not_modelled_effects: '[]' },
    { integrity: {} },
    { integrity: { not_modelled_effects: 7 } },
  ]) {
    assert.deepEqual(notModelledEffects(payload), [], JSON.stringify(payload));
  }
});

test('notModelledEffects drops entries that cannot name the effect', () => {
  const rows = notModelledEffects({
    not_modelled_effects: [
      { id: 'a', effect: 'Noise propagation', reason: 'r', status: 'NOT MODELLED' },
      { id: 'b', effect: '   ', reason: 'r', status: 'NOT MODELLED' },
      { id: 'c', effect: 42, reason: 'r', status: 'NOT MODELLED' },
      { id: 'd', reason: 'r', status: 'NOT MODELLED' },
      null,
      'noise',
      7,
    ],
  });

  assert.equal(rows.length, 1, 'only the row that names an effect survives');
  assert.equal(rows[0].id, 'a');
});

test('notModelledEffects never supplies a reason or status the backend did not give', () => {
  const rows = notModelledEffects({
    not_modelled_effects: [{ effect: 'Heat island magnitude' }],
  });

  assert.equal(rows.length, 1);
  assert.equal(rows[0].reason, '', 'a missing reason stays empty - it is not filled in');
  assert.equal(rows[0].effect, 'Heat island magnitude');
  assert.equal(rows[0].status, 'NOT MODELLED', 'status is a label, not a measurement');
  assert.equal(rows[0].id, 'effect-0', 'a missing id falls back to a stable index key');
});
