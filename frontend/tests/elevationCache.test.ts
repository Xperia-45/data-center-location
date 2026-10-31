/**
 * Browser DEM cache versioning (P1 from the Stage 3A production verification).
 *
 * Entries saved before the provenance correction carry the withdrawn "Measured DEM terrain"
 * label and no `data_class` / `surface_model`. They must never be displayed again, so the
 * localStorage namespace moved `terralex:elevation_cache:v1` -> `:v2`, the per-entry keys
 * moved `dem:v1:` -> `dem:v2:`, the legacy blob is dropped, and any payload without current
 * provenance fields is refused on load.
 *
 * All payloads here are synthetic test fixtures.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import type { ElevationGridPayload } from '../src/lib/timescapeMath.ts';
import {
  CLIENT_DEM_STORAGE_KEY,
  LEGACY_CLIENT_DEM_STORAGE_KEY,
  MAX_CLIENT_DEM_ENTRIES,
  type StorageLike,
  dropLegacyClientElevationCache,
  loadClientCachedElevation,
  makeClientElevationCacheKey,
  saveClientCachedElevation,
  syncElevationWithClientCache,
} from '../src/lib/elevationCache.ts';

const site = { lat: 30.901, lon: 75.8573, radius_km: 5 };

function makeStorage(initial: Record<string, string> = {}): StorageLike & { store: Map<string, string> } {
  const store = new Map<string, string>(Object.entries(initial));
  return {
    store,
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => {
      store.set(k, v);
    },
    removeItem: (k) => {
      store.delete(k);
    },
  };
}

const currentDem: ElevationGridPayload = {
  status: 'ok',
  grid_size: 2,
  values_m: [
    [248, 250],
    [246, 247],
  ],
  data_class: 'observation_derived',
  surface_model: 'Digital Surface Model (DSM): includes canopy and structures; NOT bare-earth DTM.',
  data_label: 'Observed-derived DEM terrain (Copernicus GLO-90 ~90 m digital surface model, 2x2 ENU grid)',
};

/** Exactly what the pre-correction frontend stored: valid grid, withdrawn classification. */
const legacyDem: ElevationGridPayload = {
  status: 'ok',
  grid_size: 2,
  values_m: [
    [248, 250],
    [246, 247],
  ],
  data_label: 'Measured DEM terrain (Copernicus GLO-90 ~90m, 2x2 ENU grid)',
};

test('browser DEM cache namespace is versioned and the legacy key is known', () => {
  assert.equal(CLIENT_DEM_STORAGE_KEY, 'terralex:elevation_cache:v2');
  assert.equal(LEGACY_CLIENT_DEM_STORAGE_KEY, 'terralex:elevation_cache:v1');
  assert.ok(makeClientElevationCacheKey(site, 20).startsWith('dem:v2:'));
  assert.ok(!makeClientElevationCacheKey(site, 20).startsWith('dem:v1:'));
});

test('a legacy v1 blob is never read and is dropped from storage', () => {
  const storage = makeStorage({
    [LEGACY_CLIENT_DEM_STORAGE_KEY]: JSON.stringify([
      { key: 'dem:v1:30.9010:75.8573:5.00:2', savedAt: Date.now(), payload: legacyDem },
    ]),
  });

  // Nothing is restored from the old namespace...
  assert.equal(loadClientCachedElevation(site, 2, storage), null);
  // ...and the stale blob is removed so it cannot be displayed later.
  assert.equal(storage.store.has(LEGACY_CLIENT_DEM_STORAGE_KEY), false);
  dropLegacyClientElevationCache(storage); // idempotent
  assert.equal(storage.store.has(LEGACY_CLIENT_DEM_STORAGE_KEY), false);
});

test('a pre-correction payload under the current key is refused', () => {
  const storage = makeStorage();
  const key = makeClientElevationCacheKey(site, 2);
  storage.setItem(
    CLIENT_DEM_STORAGE_KEY,
    JSON.stringify([{ key, savedAt: Date.now(), payload: legacyDem }])
  );

  assert.equal(loadClientCachedElevation(site, 2, storage), null);

  // The explicit unavailable result is preserved instead of being replaced by stale terrain.
  const failed: ElevationGridPayload = { status: 'unavailable', reason: 'rate_limited', grid_size: 2, values_m: null };
  const resolved = syncElevationWithClientCache(site, failed, storage);
  assert.equal(resolved?.status, 'unavailable');
  assert.equal(resolved?.reason, 'rate_limited');
});

test('a payload classified as measured is refused even with the other fields present', () => {
  const storage = makeStorage();
  saveClientCachedElevation(
    site,
    { ...legacyDem, data_class: 'measured', surface_model: 'DSM' } as ElevationGridPayload,
    storage
  );
  assert.equal(loadClientCachedElevation(site, 2, storage), null);
});

test('a current payload round-trips with an honest label', () => {
  const storage = makeStorage();
  saveClientCachedElevation(site, currentDem, storage);

  const restored = loadClientCachedElevation(site, 2, storage);
  assert.ok(restored);
  assert.equal(restored!.status, 'ok');
  assert.equal(restored!.cache_hit, 'browser_local_storage');
  assert.equal(restored!.data_class, 'observation_derived');
  assert.ok(restored!.surface_model!.includes('Digital Surface Model'));
  assert.ok(restored!.data_label!.includes('Observed-derived DEM terrain'));
  assert.ok(!restored!.data_label!.includes('Measured'), 'the withdrawn wording must not return');

  const failed: ElevationGridPayload = { status: 'unavailable', reason: 'timeout', grid_size: 2, values_m: null };
  const resolved = syncElevationWithClientCache(site, failed, storage);
  assert.equal(resolved?.status, 'ok', 'a verified grid may still backfill a transient failure');
  assert.equal(resolved?.cache_hit, 'browser_local_storage');
});

test('cache entries stay site-scoped and bounded', () => {
  const storage = makeStorage();
  saveClientCachedElevation(site, currentDem, storage);

  const other = { lat: 30.7046, lon: 76.7179, radius_km: 5 };
  assert.equal(loadClientCachedElevation(other, 2, storage), null, 'another site must not reuse it');
  assert.equal(loadClientCachedElevation({ ...site, radius_km: 12 }, 2, storage), null, 'nor another radius');

  for (let i = 0; i < MAX_CLIENT_DEM_ENTRIES + 5; i++) {
    saveClientCachedElevation({ lat: 30 + i * 0.01, lon: 75, radius_km: 5 }, currentDem, storage);
  }
  const entries = JSON.parse(storage.getItem(CLIENT_DEM_STORAGE_KEY) ?? '[]');
  assert.ok(entries.length <= MAX_CLIENT_DEM_ENTRIES, 'the browser cache stays bounded');
});

test('storage without removeItem still refuses legacy data', () => {
  const store = new Map<string, string>();
  const storage: StorageLike = {
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => {
      store.set(k, v);
    },
  };
  store.set(
    LEGACY_CLIENT_DEM_STORAGE_KEY,
    JSON.stringify([{ key: 'dem:v1:30.9010:75.8573:5.00:2', savedAt: Date.now(), payload: legacyDem }])
  );
  assert.equal(loadClientCachedElevation(site, 2, storage), null);
  saveClientCachedElevation(site, currentDem, storage); // must not throw without removeItem
  assert.ok(loadClientCachedElevation(site, 2, storage));
});
