import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ANALYSIS_RADIUS_UNITS,
  DEFAULT_VERTICAL_EXAGGERATION,
  type ElevationGridPayload,
  KM_PER_DEG_LAT,
  MAX_PAD_WIDTH,
  MAX_SCENE_RELIEF_UNITS,
  SITE_PRESETS,
  computeCampusLayout,
  computeTerrainVerticalParams,
  isValidElevationGrid,
  latLonToEnuKm,
  projectLatLonToScene,
  resolveTimescapeElevation,
  resolveTimescapeFeatures,
  sampleElevationMeters,
  selectSceneFeatures,
  terrainHeightAt,
  unitsPerKmForRadius,
} from '../src/lib/timescapeMath.ts';

import {
  type StorageLike,
  syncElevationWithClientCache,
} from '../src/lib/elevationCache.ts';

import {
  attachCreatedSite,
  beginCreateAndAnalyze,
  beginSiteLoad,
  completeSiteLoad,
  createInitialWorkspaceState,
  failSiteLoad,
  getValidatedFeaturesForSite,
  isTimescapeReady,
  onFormCoordinatesChanged,
} from '../src/lib/siteState.ts';

test('Ludhiana and Mohali presets have distinct, realistic coordinates', () => {
  assert.notEqual(SITE_PRESETS.ludhiana.lat, SITE_PRESETS.mohali.lat);
  assert.notEqual(SITE_PRESETS.ludhiana.lon, SITE_PRESETS.mohali.lon);

  const delta = latLonToEnuKm(
    SITE_PRESETS.ludhiana.lat,
    SITE_PRESETS.ludhiana.lon,
    SITE_PRESETS.mohali.lat,
    SITE_PRESETS.mohali.lon
  );
  assert.ok(delta.northKm > 15, 'Ludhiana should be north of Mohali');
  assert.ok(delta.eastKm < -70, 'Ludhiana should be west of Mohali');
  assert.ok(delta.distanceKm > 75 && delta.distanceKm < 100);
});

test('latLonToEnuKm applies latitude cosine adjustment to longitude distances', () => {
  const eq = latLonToEnuKm(0, 1, 0, 0);
  assert.ok(Math.abs(eq.eastKm - KM_PER_DEG_LAT) < 1e-6);

  const lat60 = latLonToEnuKm(60, 1, 60, 0);
  assert.ok(Math.abs(lat60.eastKm - KM_PER_DEG_LAT * 0.5) < 1e-4);

  const northOnly = latLonToEnuKm(31.901, 75.8573, 30.901, 75.8573);
  assert.ok(Math.abs(northOnly.eastKm) < 1e-9);
  assert.ok(Math.abs(northOnly.northKm - KM_PER_DEG_LAT) < 1e-6);
});

test('projectLatLonToScene maps ENU correctly (+X East, -Z North, Y on terrain) and scales by radius_km', () => {
  const site5 = { lat: 30.901, lon: 75.8573, radius_km: 5 };
  const site10 = { lat: 30.901, lon: 75.8573, radius_km: 10 };
  const site50 = { lat: 30.901, lon: 75.8573, radius_km: 50 };

  assert.equal(unitsPerKmForRadius(5), ANALYSIS_RADIUS_UNITS / 5);
  assert.equal(unitsPerKmForRadius(10), ANALYSIS_RADIUS_UNITS / 10);

  const northLat = site5.lat + 2.5 / KM_PER_DEG_LAT;
  const p5 = projectLatLonToScene(northLat, site5.lon, site5);
  const p10 = projectLatLonToScene(northLat, site10.lon, site10);

  assert.ok(Math.abs(p5.x) < 1e-6);
  assert.ok(Math.abs(p5.z - -16) < 1e-4, '2.5 km North on 5 km radius maps to z = -16');
  assert.ok(Math.abs(p10.z - -8) < 1e-4, 'Doubling radius_km halves scene distance');
  assert.ok(Math.abs(p5.y - terrainHeightAt(p5.x, p5.z)) < 1e-9);

  const farLat = site50.lat + 45 / KM_PER_DEG_LAT;
  const p50 = projectLatLonToScene(farLat, site50.lon, site50);
  assert.equal(p50.inBounds, true);
  assert.equal(p50.withinRadius, true);
  assert.ok(Math.abs(p50.z) < ANALYSIS_RADIUS_UNITS);
});

test('computeCampusLayout keeps campus compact and proportional so nearby features are not buried', () => {
  const present = computeCampusLayout({ footprint_ha: 10, radius_km: 5 }, 'baseline', 'present');
  assert.equal(present.visible, false);
  assert.equal(present.halls.length, 0);

  const dev = computeCampusLayout({ footprint_ha: 10, radius_km: 5 }, 'baseline', 'development');
  assert.equal(dev.visible, true);
  assert.ok(dev.padWidth > 1.8 && dev.padWidth < 2.5);

  const highGrowth = computeCampusLayout({ footprint_ha: 25, radius_km: 5 }, 'high-growth', 'future');
  assert.ok(highGrowth.padWidth <= MAX_PAD_WIDTH);
  for (const h of highGrowth.halls) {
    assert.ok(Math.abs(h.x) + h.width / 2 <= highGrowth.padWidth / 2 + 1e-6);
    assert.ok(Math.abs(h.z) + h.depth / 2 <= highGrowth.padDepth / 2 + 1e-6);
  }

  const site = { lat: 30.901, lon: 75.8573, radius_km: 5 };
  const cosLat = Math.cos((site.lat * Math.PI) / 180);
  const nearFeat = projectLatLonToScene(site.lat, site.lon + 0.6 / (KM_PER_DEG_LAT * cosLat), site);
  assert.ok(nearFeat.x > dev.padWidth / 2 + 2.0);
});

test('selectSceneFeatures sorts features ascending by distance BEFORE slicing to maxCount', () => {
  const site = { lat: 30.901, lon: 75.8573, radius_km: 5 };
  const unsorted: any[] = [];
  for (let i = 0; i < 38; i++) {
    const dKm = 4.5 - (i % 5) * 0.2;
    unsorted.push({
      name: `Far-${i}`,
      kind: 'settlement',
      lat: site.lat + dKm / KM_PER_DEG_LAT,
      lon: site.lon,
      distance_km: Number(dKm.toFixed(2)),
    });
  }
  unsorted.push({
    name: 'Nearest wetland',
    kind: 'wetland',
    lat: site.lat + 0.22 / KM_PER_DEG_LAT,
    lon: site.lon,
    distance_km: 0.22,
  });
  unsorted.push({
    name: 'Nearest canal',
    kind: 'water',
    lat: site.lat + 0.45 / KM_PER_DEG_LAT,
    lon: site.lon,
    distance_km: 0.45,
  });

  const selected = selectSceneFeatures(unsorted, site, 30);
  assert.equal(selected.length, 30);
  assert.equal(selected[0].feature.name, 'Nearest wetland');
  assert.equal(selected[1].feature.name, 'Nearest canal');
  for (let i = 1; i < selected.length; i++) {
    assert.ok(selected[i].sortDistanceKm >= selected[i - 1].sortDistanceKm);
  }
});

test('site switching, coordinate edits, and loading guards prevent stale previous-site features in Timescape', () => {
  const siteA = {
    id: 'site-a',
    name: 'Ludhiana node',
    lat: SITE_PRESETS.ludhiana.lat,
    lon: SITE_PRESETS.ludhiana.lon,
    radius_km: 5,
    footprint_ha: 10,
    water_mld: 1,
    power_mw: 20,
    cooling: 'air',
    built_up_m2: 30000,
  };
  const siteB = {
    ...siteA,
    id: 'site-b',
    name: 'Hyderabad candidate',
    lat: SITE_PRESETS.hyderabad.lat,
    lon: SITE_PRESETS.hyderabad.lon,
  };
  const analysisA = {
    site_id: 'site-a',
    findings: [],
    features: [{ name: 'Sutlej canal', kind: 'water', lat: 30.91, lon: 75.86, distance_km: 1.0 }],
    data_label: 'Verified real data (OpenStreetMap)',
  };
  const analysisB = {
    site_id: 'site-b',
    findings: [],
    features: [{ name: 'Hussain Sagar', kind: 'water', lat: 17.42, lon: 78.47, distance_km: 4.2 }],
    data_label: 'Verified real data (OpenStreetMap)',
  };

  let state = createInitialWorkspaceState();
  state = beginSiteLoad(state, siteA);
  const reqA = state.requestId;
  assert.equal(state.loadingAnalysis, true);
  assert.deepEqual(getValidatedFeaturesForSite(state), []);
  state = completeSiteLoad(state, reqA, siteA, analysisA, [], { preset: 'baseline' });
  assert.equal(isTimescapeReady(state), true);
  assert.equal(getValidatedFeaturesForSite(state)[0].name, 'Sutlej canal');

  state = beginSiteLoad(state, siteB);
  const reqB = state.requestId;
  assert.equal(state.loadingAnalysis, true);
  assert.equal(isTimescapeReady(state), false);
  assert.deepEqual(getValidatedFeaturesForSite(state), []);
  assert.deepEqual(
    resolveTimescapeFeatures({
      site: siteB,
      features: analysisA.features,
      analysisSiteId: analysisA.site_id,
      loading: false,
    }),
    []
  );
  assert.deepEqual(
    resolveTimescapeFeatures({
      site: siteB,
      features: analysisB.features,
      analysisSiteId: analysisB.site_id,
      loading: true,
    }),
    []
  );

  const afterStale = completeSiteLoad(state, reqA, siteA, analysisA, [], null);
  assert.equal(afterStale, state);

  state = completeSiteLoad(state, reqB, siteB, analysisB, [], { preset: 'baseline' });
  assert.equal(isTimescapeReady(state), true);
  assert.equal(getValidatedFeaturesForSite(state)[0].name, 'Hussain Sagar');

  const movedForm = { lat: SITE_PRESETS.mohali.lat, lon: SITE_PRESETS.mohali.lon, radius_km: 5 };
  assert.deepEqual(getValidatedFeaturesForSite(state, movedForm), []);
  state = onFormCoordinatesChanged(state, movedForm);
  assert.equal(state.site, null);
  assert.equal(state.analysis, null);
  assert.equal(isTimescapeReady(state), false);

  state = beginCreateAndAnalyze(state);
  const reqCreate = state.requestId;
  state = attachCreatedSite(state, reqCreate, { ...siteA, id: 'site-c' });
  assert.equal(state.loadingAnalysis, true);
  assert.deepEqual(getValidatedFeaturesForSite(state), []);
  state = failSiteLoad(state, reqCreate);
  assert.equal(state.loadingAnalysis, false);
  assert.deepEqual(getValidatedFeaturesForSite(state), []);
});

test('Stage 2: bilinear interpolation supports configurable grid sizes and aligns ENU axes', () => {
  // 2x2 grid: NW=400, NE=600, SW=200, SE=300
  const dem2: ElevationGridPayload = {
    status: 'ok',
    grid_size: 2,
    values_m: [
      [400, 600],
      [200, 300],
    ],
  };
  assert.equal(isValidElevationGrid(dem2), true);
  assert.equal(sampleElevationMeters(-40, -40, dem2), 400); // NW
  assert.equal(sampleElevationMeters(40, -40, dem2), 600);  // NE
  assert.equal(sampleElevationMeters(-40, 40, dem2), 200);  // SW
  assert.equal(sampleElevationMeters(40, 40, dem2), 300);   // SE
  assert.equal(sampleElevationMeters(0, 0, dem2), 375);     // Exact bilinear midpoint
  assert.ok(Math.abs(terrainHeightAt(0, 0, dem2, 5)) < 1e-9, 'Site origin (0,0) anchored at y = 0');

  // 20x20 grid (Stage 2 default)
  const values20 = Array.from({ length: 20 }, (_, r) =>
    Array.from({ length: 20 }, (_, c) => 240 + r * 2 + c * 3)
  );
  const dem20: ElevationGridPayload = { status: 'ok', grid_size: 20, values_m: values20 };
  assert.equal(isValidElevationGrid(dem20), true);
  assert.ok(Math.abs(sampleElevationMeters(-40, -40, dem20)! - 240) < 1e-6);
  assert.ok(Math.abs(sampleElevationMeters(40, 40, dem20)! - (240 + 19 * 2 + 19 * 3)) < 1e-6);
  assert.ok(Math.abs(terrainHeightAt(0, 0, dem20, 5)) < 1e-9);
});

test('Stage 2: missing, unavailable, or malformed elevation falls back cleanly to flat y=0 plane', () => {
  const unavailable: ElevationGridPayload = { status: 'unavailable', reason: 'timeout', values_m: null };
  const malformedDim: ElevationGridPayload = { status: 'ok', grid_size: 4, values_m: [[1, 2], [3, 4]] };
  const withNaN: ElevationGridPayload = { status: 'ok', grid_size: 2, values_m: [[250, NaN], [250, 250]] };

  for (const bad of [null, undefined, unavailable, malformedDim, withNaN]) {
    assert.equal(isValidElevationGrid(bad), false);
    const params = computeTerrainVerticalParams(bad, 5);
    assert.equal(params.isMeasured, false);
    assert.equal(params.minM, null);
    assert.equal(terrainHeightAt(-25, 18, bad, 5), 0);
    assert.equal(terrainHeightAt(0, 0, bad, 5), 0);
  }
});

test('Stage 2: distinct DEM profiles produce distinct 3D terrain heights and cap extreme mountain relief', () => {
  // Flat Ludhiana-like alluvial plain (~6 m relief across 20x20)
  const ludGrid = Array.from({ length: 20 }, (_, r) =>
    Array.from({ length: 20 }, (_, c) => 246 + (r / 19) * 3 + (c / 19) * 3)
  );
  const ludDem: ElevationGridPayload = { status: 'ok', grid_size: 20, values_m: ludGrid };

  // Sloped Mohali/Shivalik foothill profile (~420 m rise toward North-East: row -> 0, col -> 19)
  const mohGrid = Array.from({ length: 20 }, (_, r) =>
    Array.from({ length: 20 }, (_, c) => 305 + (1 - r / 19) * 240 + (c / 19) * 180)
  );
  const mohDem: ElevationGridPayload = { status: 'ok', grid_size: 20, values_m: mohGrid };

  const ludParams = computeTerrainVerticalParams(ludDem, 5);
  const mohParams = computeTerrainVerticalParams(mohDem, 5);
  assert.equal(ludParams.verticalExaggeration, DEFAULT_VERTICAL_EXAGGERATION);
  assert.ok(mohParams.reliefM! > ludParams.reliefM! * 50);

  const ludNE = Math.abs(terrainHeightAt(32, -32, ludDem, 5));
  const mohNE = terrainHeightAt(32, -32, mohDem, 5);
  const mohSW = terrainHeightAt(-32, 32, mohDem, 5);

  assert.ok(ludNE < 0.15, 'Flat plain stays visibly flat in scene units');
  assert.ok(mohNE > 2.5, 'NE Shivalik foothills rise substantially above site origin');
  assert.ok(mohSW < -2.5, 'SW plain slopes below foothill midpoint');

  // Extreme mountain relief (e.g. 2200 m at r=5 km) tapers verticalExaggeration cleanly inside MAX_SCENE_RELIEF_UNITS
  const steepDem: ElevationGridPayload = {
    status: 'ok',
    grid_size: 2,
    values_m: [
      [3200, 3200],
      [1000, 1000],
    ],
  };
  const steepParams = computeTerrainVerticalParams(steepDem, 5);
  assert.ok(steepParams.verticalExaggeration < DEFAULT_VERTICAL_EXAGGERATION);
  const peakRange =
    terrainHeightAt(0, -40, steepDem, 5) - terrainHeightAt(0, 40, steepDem, 5);
  assert.ok(peakRange <= MAX_SCENE_RELIEF_UNITS + 0.1);
});

test('Stage 2: client localStorage cache restores verified DEM only for matching site & radius', () => {
  const store = new Map<string, string>();
  const mockStorage: StorageLike = {
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => {
      store.set(k, v);
    },
  };

  const ludSite = { lat: SITE_PRESETS.ludhiana.lat, lon: SITE_PRESETS.ludhiana.lon, radius_km: 5 };
  const mohSite = { lat: SITE_PRESETS.mohali.lat, lon: SITE_PRESETS.mohali.lon, radius_km: 5 };
  const validDem: ElevationGridPayload = {
    status: 'ok',
    grid_size: 2,
    values_m: [
      [248, 250],
      [246, 247],
    ],
    // Current provenance fields: a stored payload without them is treated as pre-correction
    // and must never be restored (see tests/elevationCache.test.ts).
    data_class: 'observation_derived',
    surface_model: 'Digital Surface Model (DSM): includes canopy and structures; NOT bare-earth DTM.',
    data_label: 'Observed-derived DEM terrain',
  };
  const failedDem: ElevationGridPayload = { status: 'unavailable', reason: 'rate_limited', grid_size: 2, values_m: null };

  // Save on healthy response
  const saved = syncElevationWithClientCache(ludSite, validDem, mockStorage);
  assert.equal(saved?.status, 'ok');

  // Restore on subsequent transient failure for SAME site & radius
  const restored = syncElevationWithClientCache(ludSite, failedDem, mockStorage);
  assert.equal(restored?.status, 'ok');
  assert.equal(restored?.cache_hit, 'browser_local_storage');

  // Do NOT restore Ludhiana DEM for Mohali or for a different radius_km
  const otherSite = syncElevationWithClientCache(mohSite, failedDem, mockStorage);
  assert.equal(otherSite?.status, 'unavailable');
  const otherRadius = syncElevationWithClientCache({ ...ludSite, radius_km: 10 }, failedDem, mockStorage);
  assert.equal(otherRadius?.status, 'unavailable');
});

test('Integration: resolveTimescapeElevation rejects cross-site or cross-radius elevation payloads', () => {
  const ludSite = { id: 'lud-1', lat: 30.901, lon: 75.8573, radius_km: 5 };
  const mohSite = { id: 'moh-1', lat: 30.7046, lon: 76.7179, radius_km: 5 };
  const ludElev: ElevationGridPayload = {
    status: 'ok',
    lat: 30.901,
    lon: 75.8573,
    radius_km: 5,
    grid_size: 2,
    values_m: [
      [246, 248],
      [245, 247],
    ],
  };

  assert.equal(
    resolveTimescapeElevation({ site: ludSite, elevation: ludElev, analysisSiteId: 'lud-1', loading: false }),
    ludElev
  );
  // Reject if site coordinates belong to Mohali even if caller forgot to clear ludElev
  assert.equal(
    resolveTimescapeElevation({ site: mohSite, elevation: ludElev, analysisSiteId: 'moh-1', loading: false }),
    null
  );
  // Reject if radius changed (e.g. 5 km -> 12 km)
  assert.equal(
    resolveTimescapeElevation({
      site: { ...ludSite, radius_km: 12 },
      elevation: ludElev,
      analysisSiteId: 'lud-1',
      loading: false,
    }),
    null
  );
  // Reject while loading
  assert.equal(
    resolveTimescapeElevation({ site: ludSite, elevation: ludElev, analysisSiteId: 'lud-1', loading: true }),
    null
  );
});

test('Integration: Timescape component renders AMSL metadata, attribution, fallback warning, and survives 30 rapid site switches', async () => {
  const esbuild = await import('esbuild');
  const React = await import('react');
  const ReactDOMServer = await import('react-dom/server');
  const path = await import('node:path');
  const url = await import('node:url');

  const dir = path.dirname(url.fileURLToPath(import.meta.url));
  const entry = path.resolve(dir, '../src/components/Timescape.tsx');
  const { createRequire } = await import('node:module');
  const bundled = await esbuild.build({
    entryPoints: [entry],
    bundle: true,
    write: false,
    format: 'cjs',
    platform: 'node',
    external: ['react', 'react-dom', 'three', 'three/*'],
  });
  const req = createRequire(import.meta.url);
  const cjsMod = { exports: {} as any };
  new Function('require', 'module', 'exports', bundled.outputFiles[0].text)(req, cjsMod, cjsMod.exports);
  const Timescape = cjsMod.exports.default;

  const sites = [
    {
      site: {
        id: 'site-lud',
        name: 'Ludhiana node',
        lat: SITE_PRESETS.ludhiana.lat,
        lon: SITE_PRESETS.ludhiana.lon,
        radius_km: 5,
        footprint_ha: 10,
        water_mld: 1,
        power_mw: 20,
        cooling: 'air',
        built_up_m2: 30000,
      },
      elev: {
        status: 'ok',
        lat: SITE_PRESETS.ludhiana.lat,
        lon: SITE_PRESETS.ludhiana.lon,
        radius_km: 5,
        grid_size: 20,
        values_m: Array.from({ length: 20 }, (_, r) =>
          Array.from({ length: 20 }, (_, c) => 244 + (r / 19) * 5 + (c / 19) * 4)
        ),
        source_url: 'https://open-meteo.com/en/docs/elevation-api',
        copernicus_doi_url: 'https://doi.org/10.5270/ESA-c5d3d65',
      } as ElevationGridPayload,
    },
    {
      site: {
        id: 'site-moh',
        name: 'Mohali candidate',
        lat: SITE_PRESETS.mohali.lat,
        lon: SITE_PRESETS.mohali.lon,
        radius_km: 8,
        footprint_ha: 15,
        water_mld: 1.2,
        power_mw: 30,
        cooling: 'air',
        built_up_m2: 35000,
      },
      elev: {
        status: 'ok',
        lat: SITE_PRESETS.mohali.lat,
        lon: SITE_PRESETS.mohali.lon,
        radius_km: 8,
        grid_size: 20,
        values_m: Array.from({ length: 20 }, (_, r) =>
          Array.from({ length: 20 }, (_, c) => 302 + (1 - r / 19) * 210 + (c / 19) * 140)
        ),
        source_url: 'https://open-meteo.com/en/docs/elevation-api',
        copernicus_doi_url: 'https://doi.org/10.5270/ESA-c5d3d65',
      } as ElevationGridPayload,
    },
    {
      site: {
        id: 'site-hyd',
        name: 'Hyderabad candidate',
        lat: SITE_PRESETS.hyderabad.lat,
        lon: SITE_PRESETS.hyderabad.lon,
        radius_km: 5,
        footprint_ha: 12,
        water_mld: 1.5,
        power_mw: 40,
        cooling: 'hybrid-dry',
        built_up_m2: 40000,
      },
      elev: {
        status: 'ok',
        lat: SITE_PRESETS.hyderabad.lat,
        lon: SITE_PRESETS.hyderabad.lon,
        radius_km: 5,
        grid_size: 20,
        values_m: Array.from({ length: 20 }, (_, r) =>
          Array.from({ length: 20 }, (_, c) => 495 + Math.sin(r * 0.5) * 28 + (c / 19) * 42)
        ),
        source_url: 'https://open-meteo.com/en/docs/elevation-api',
        copernicus_doi_url: 'https://doi.org/10.5270/ESA-c5d3d65',
      } as ElevationGridPayload,
    },
  ];

  // Verify measured UI output for all 3 cities + 30 rapid site-switch cycles
  let ws = createInitialWorkspaceState();
  for (let i = 0; i < 30; i++) {
    const entry = sites[i % sites.length];
    ws = beginSiteLoad(ws, entry.site);
    const reqId = ws.requestId;
    assert.deepEqual(getValidatedFeaturesForSite(ws), []);
    ws = completeSiteLoad(
      ws,
      reqId,
      entry.site,
      {
        site_id: entry.site.id,
        findings: [],
        features: [{ name: `${entry.site.name} canal`, kind: 'water', lat: entry.site.lat + 0.01, lon: entry.site.lon, distance_km: 1.11 }],
        elevation: entry.elev,
        data_label: 'Verified real data (OpenStreetMap)',
      },
      [],
      { preset: 'baseline' }
    );
    const html = ReactDOMServer.renderToStaticMarkup(
      React.createElement(Timescape, {
        site: ws.site!,
        features: getValidatedFeaturesForSite(ws),
        elevation: ws.analysis!.elevation,
        analysisSiteId: ws.analysis!.site_id,
        dataLabel: ws.analysis!.data_label,
        loading: ws.loadingAnalysis,
        temporal: 'development',
        scenario: 'baseline',
        showLegal: true,
      })
    );
    assert.ok(html.includes('Observed-derived DEM Terrain (DSM)'));
    assert.ok(!html.includes('Measured DEM Terrain'), 'the withdrawn label must not be rendered');
    assert.ok(html.includes('observation_derived'), 'the provenance class is disclosed in the UI');
    assert.ok(html.includes('digital surface model'));
    assert.ok(html.includes('m AMSL'));
    assert.ok(html.includes('20×20'));
    assert.ok(html.includes('2.5×'));
    assert.ok(html.includes('Open-Meteo.com'));
    assert.ok(html.includes('10.5270/ESA-c5d3d65'));
    assert.ok(html.includes(entry.site.name));
  }

  // Verify unavailable fallback UI output when Open-Meteo fails
  const fallbackHtml = ReactDOMServer.renderToStaticMarkup(
    React.createElement(Timescape, {
      site: sites[0].site,
      features: [],
      elevation: { status: 'unavailable', reason: 'timeout', values_m: null },
      analysisSiteId: sites[0].site.id,
      dataLabel: 'Verified real data (OpenStreetMap)',
      loading: false,
      temporal: 'present',
      scenario: 'baseline',
      showLegal: true,
    })
  );
  assert.ok(fallbackHtml.includes('FALLBACK REFERENCE PLANE (Elevation UNAVAILABLE)'));
  assert.ok(fallbackHtml.includes('timeout'));
  assert.ok(fallbackHtml.includes('not measured terrain'));
});
