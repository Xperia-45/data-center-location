import type { Site, LegalResult } from './api.ts';
import type { ElevationGridPayload } from './timescapeMath.ts';

export interface SiteAnalysisPayload {
  site_id: string;
  findings: any[];
  features: any[];
  elevation?: ElevationGridPayload | null;
  data_label: string;
  disclaimer?: string;
}

export interface ActiveSiteWorkspaceState {
  site: Site | null;
  analysis: SiteAnalysisPayload | null;
  legal: LegalResult[];
  scenario: any | null;
  loadingAnalysis: boolean;
  requestId: number;
}

export function createInitialWorkspaceState(): ActiveSiteWorkspaceState {
  return {
    site: null,
    analysis: null,
    legal: [],
    scenario: null,
    loadingAnalysis: false,
    requestId: 0,
  };
}

export function coordinatesMatchSite(
  form: Pick<Site, 'lat' | 'lon' | 'radius_km'>,
  site: Pick<Site, 'lat' | 'lon' | 'radius_km'> | null | undefined
): boolean {
  if (!site) return false;
  return (
    Math.abs(Number(form.lat) - Number(site.lat)) < 1e-6 &&
    Math.abs(Number(form.lon) - Number(site.lon)) < 1e-6 &&
    Math.abs(Number(form.radius_km) - Number(site.radius_km)) < 1e-6
  );
}

/**
 * Invalidates active site/analysis state immediately when candidate coordinates or radius change
 * so stale features/findings from a previous site cannot carry over.
 */
export function onFormCoordinatesChanged(
  state: ActiveSiteWorkspaceState,
  nextForm: Pick<Site, 'lat' | 'lon' | 'radius_km'>
): ActiveSiteWorkspaceState {
  if (coordinatesMatchSite(nextForm, state.site)) {
    return state;
  }
  if (!state.site && !state.analysis && !state.loadingAnalysis && state.legal.length === 0 && !state.scenario) {
    return state;
  }
  return {
    site: null,
    analysis: null,
    legal: [],
    scenario: null,
    loadingAnalysis: false,
    requestId: state.requestId + 1,
  };
}

/**
 * Starts loading a saved site, clearing any previous site's analysis/features synchronously.
 */
export function beginSiteLoad(
  state: ActiveSiteWorkspaceState,
  targetSite: Site
): ActiveSiteWorkspaceState {
  return {
    site: targetSite,
    analysis: null,
    legal: [],
    scenario: null,
    loadingAnalysis: true,
    requestId: state.requestId + 1,
  };
}

/**
 * Starts creating + analysing a new candidate site, clearing any previous site's state synchronously.
 */
export function beginCreateAndAnalyze(
  state: ActiveSiteWorkspaceState
): ActiveSiteWorkspaceState {
  return {
    site: null,
    analysis: null,
    legal: [],
    scenario: null,
    loadingAnalysis: true,
    requestId: state.requestId + 1,
  };
}

/**
 * Records the newly created site record while keeping analysis null and loadingAnalysis true
 * until the site's environmental analysis resolves.
 */
export function attachCreatedSite(
  state: ActiveSiteWorkspaceState,
  requestId: number,
  createdSite: Site
): ActiveSiteWorkspaceState {
  if (requestId !== state.requestId) return state;
  return {
    ...state,
    site: createdSite,
    analysis: null,
    legal: [],
    scenario: null,
    loadingAnalysis: true,
  };
}

/**
 * Commits analysis/legal/scenario results only if requestId matches the latest request AND
 * analysis.site_id matches site.id.
 */
export function completeSiteLoad(
  state: ActiveSiteWorkspaceState,
  requestId: number,
  site: Site,
  analysis: SiteAnalysisPayload,
  legal: LegalResult[],
  scenario: any
): ActiveSiteWorkspaceState {
  if (requestId !== state.requestId) return state;
  if (!site.id || !analysis || analysis.site_id !== site.id) {
    return {
      ...state,
      site,
      analysis: null,
      legal: [],
      scenario: null,
      loadingAnalysis: false,
    };
  }
  return {
    site,
    analysis,
    legal,
    scenario,
    loadingAnalysis: false,
    requestId: state.requestId,
  };
}

export function failSiteLoad(
  state: ActiveSiteWorkspaceState,
  requestId: number
): ActiveSiteWorkspaceState {
  if (requestId !== state.requestId) return state;
  return {
    ...state,
    analysis: null,
    legal: [],
    scenario: null,
    loadingAnalysis: false,
  };
}

export function isTimescapeReady(state: ActiveSiteWorkspaceState): boolean {
  return (
    !state.loadingAnalysis &&
    Boolean(state.site?.id && state.analysis && state.analysis.site_id === state.site.id)
  );
}

/**
 * Returns environmental features ONLY when analysis is finished, matches state.site.id,
 * and (when form is provided, e.g. MapView) matches the form's coordinates & radius.
 */
export function getValidatedFeaturesForSite(
  state: ActiveSiteWorkspaceState,
  form?: Pick<Site, 'lat' | 'lon' | 'radius_km'>
): any[] {
  if (!isTimescapeReady(state)) return [];
  if (form && !coordinatesMatchSite(form, state.site)) return [];
  return Array.isArray(state.analysis?.features) ? state.analysis!.features : [];
}
