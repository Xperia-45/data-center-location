import type { Site } from './api.ts';
import {
  type ElevationGridPayload,
  isValidElevationGrid,
  sanitizeRadiusKm,
} from './timescapeMath.ts';

// Browser DEM cache namespace. Bumped `v1` -> `v2` together with the backend `dem:v2`
// namespace: entries saved before the provenance correction carry the withdrawn
// "Measured DEM terrain" label and no `data_class`/`surface_model`, and must never be
// displayed again. Old blobs are simply never read (and are overwritten on next save).
export const CLIENT_DEM_STORAGE_KEY = 'terralex:elevation_cache:v2';
export const LEGACY_CLIENT_DEM_STORAGE_KEY = 'terralex:elevation_cache:v1';
export const MAX_CLIENT_DEM_ENTRIES = 24;

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

interface StoredDemEntry {
  key: string;
  savedAt: number;
  payload: ElevationGridPayload;
}

function getDefaultStorage(): StorageLike | null {
  try {
    if (typeof globalThis !== 'undefined' && 'localStorage' in globalThis && globalThis.localStorage) {
      return globalThis.localStorage;
    }
  } catch {
    return null;
  }
  return null;
}

export function makeClientElevationCacheKey(
  site: Pick<Site, 'lat' | 'lon' | 'radius_km'>,
  gridSize: number = 20
): string {
  const rKm = sanitizeRadiusKm(site.radius_km);
  // `dem:v2` mirrors the backend cache namespace for the same reason (see above).
  return `dem:v2:${Number(site.lat).toFixed(4)}:${Number(site.lon).toFixed(4)}:${rKm.toFixed(2)}:${gridSize}`;
}

/**
 * Drops the pre-correction browser cache blob. It is never read, and removing it keeps a
 * payload labelled "Measured DEM terrain" from surviving in the user's storage.
 */
export function dropLegacyClientElevationCache(storage: StorageLike | null = getDefaultStorage()): void {
  if (!storage?.removeItem) return;
  try {
    storage.removeItem(LEGACY_CLIENT_DEM_STORAGE_KEY);
  } catch {
    // Ignore private-mode / quota failures cleanly
  }
}

/** True when a stored payload carries the current provenance fields. */
function hasCurrentProvenance(payload: ElevationGridPayload): boolean {
  return Boolean(payload.data_class) && payload.data_class !== 'measured' && Boolean(payload.surface_model);
}

export function saveClientCachedElevation(
  site: Pick<Site, 'lat' | 'lon' | 'radius_km'>,
  elevation: ElevationGridPayload,
  storage: StorageLike | null = getDefaultStorage()
): void {
  if (!storage || !isValidElevationGrid(elevation)) return;
  dropLegacyClientElevationCache(storage);
  const gridSize = elevation.values_m.length;
  const key = makeClientElevationCacheKey(site, gridSize);
  try {
    const raw = storage.getItem(CLIENT_DEM_STORAGE_KEY);
    const parsed: StoredDemEntry[] = raw ? JSON.parse(raw) : [];
    const filtered = Array.isArray(parsed) ? parsed.filter((e) => e && e.key !== key) : [];
    filtered.push({ key, savedAt: Date.now(), payload: elevation });
    while (filtered.length > MAX_CLIENT_DEM_ENTRIES) {
      filtered.shift();
    }
    storage.setItem(CLIENT_DEM_STORAGE_KEY, JSON.stringify(filtered));
  } catch {
    // Ignore storage quota / private-mode failures cleanly
  }
}

export function loadClientCachedElevation(
  site: Pick<Site, 'lat' | 'lon' | 'radius_km'>,
  gridSize: number = 20,
  storage: StorageLike | null = getDefaultStorage()
): ElevationGridPayload | null {
  if (!storage) return null;
  dropLegacyClientElevationCache(storage);
  const key = makeClientElevationCacheKey(site, gridSize);
  try {
    const raw = storage.getItem(CLIENT_DEM_STORAGE_KEY);
    if (!raw) return null;
    const parsed: StoredDemEntry[] = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const found = parsed.find((e) => e && e.key === key);
    if (!found || !isValidElevationGrid(found.payload)) return null;
    // A payload saved before the provenance correction must not be restored: it has no
    // `data_class`/`surface_model` and its label claims the DEM is "measured".
    if (!hasCurrentProvenance(found.payload)) return null;
    const p = found.payload;
    if (p.lat != null && Math.abs(Number(p.lat) - Number(site.lat)) > 0.002) return null;
    if (p.lon != null && Math.abs(Number(p.lon) - Number(site.lon)) > 0.002) return null;
    const rKm = sanitizeRadiusKm(site.radius_km);
    if (p.radius_km != null && Math.abs(Number(p.radius_km) - rKm) > 0.05) return null;
    return {
      ...found.payload,
      cache_hit: 'browser_local_storage',
      data_label: `Observed-derived DEM terrain (Copernicus GLO-90 ~90 m digital surface model, ${gridSize}x${gridSize} ENU grid — restored from browser cache)`,
    };
  } catch {
    return null;
  }
}

/**
 * Synchronizes incoming backend elevation payload with bounded browser localStorage cache:
 * - Stores verified status === 'ok' DEM grids for reuse across stateless container restarts.
 * - Restores a previously verified DEM grid ONLY for the exact (lat, lon, radius_km, grid_size) key
 *   if a subsequent call hits transient upstream failure; otherwise preserves the explicit 'unavailable' fallback.
 */
export function syncElevationWithClientCache(
  site: Pick<Site, 'lat' | 'lon' | 'radius_km'>,
  elevation: ElevationGridPayload | null | undefined,
  storage: StorageLike | null = getDefaultStorage()
): ElevationGridPayload | null {
  if (isValidElevationGrid(elevation)) {
    saveClientCachedElevation(site, elevation, storage);
    return elevation;
  }
  const desiredGrid = Number(elevation?.grid_size) || 20;
  const cached = loadClientCachedElevation(site, desiredGrid, storage);
  if (cached) return cached;
  return elevation ?? null;
}
