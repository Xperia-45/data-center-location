export interface Site {id?:string; name:string; lat:number; lon:number; radius_km:number; footprint_ha:number; water_mld:number; power_mw:number; cooling:string; built_up_m2:number; created_at?:string}
export interface Finding {category:string; finding:string; risk:string; evidence:any[]; source:string; data_quality:string; last_updated:string; confidence:string; explanation:string; data_label:string}
export interface LegalResult {law:string; section:string; original_text:string; plain_explanation:string; why_applies:string; required_action:string; authority:string; compliance_status:string; consequence_type:string; potential_consequences:string; source_url:string; effective_date:string; amendment_status:string; last_verified:string; trigger:string}
export const API = 'https://data-center-location.onrender.com/api/v1';
async function req<T>(url:string, opts?:RequestInit):Promise<T>{ const r=await fetch(API+url,{headers:{'Content-Type':'application/json'},...opts}); if(!r.ok) throw new Error(await r.text()); return r.json(); }
export const api={
 createSite:(s:Site)=>req<Site>('/sites',{method:'POST',body:JSON.stringify(s)}),
 listSites:()=>req<Site[]>('/sites'),
 analyze:(id:string)=>req<any>(`/sites/${id}/analyze`,{method:'POST'}),
 environment:(id:string)=>req<any>(`/sites/${id}/environment`),
 elevation:(lat:number,lon:number,radius_km:number=5,grid_size:number=20)=>req<any>(`/elevation?lat=${lat}&lon=${lon}&radius_km=${radius_km}&grid_size=${grid_size}`),
 /**
  * Stage 3A geographic base layer + development impact layer.
  * `grid_size` deliberately defaults to the same value as /elevation so the terrain layer hits the
  * identical `dem:v2` cache key instead of triggering a second upstream DEM fetch.
  */
 landscape:(p:{lat:number;lon:number;radius_km:number;grid_size?:number;layers?:string;osm_layers?:string;include_geometry?:boolean;preset?:string;footprint_ha?:number;water_mld?:number;power_mw?:number;refresh?:boolean})=>{
  const q=new URLSearchParams({lat:String(p.lat),lon:String(p.lon),radius_km:String(p.radius_km),grid_size:String(p.grid_size??20),layers:p.layers??'dem,osm,landcover',include_geometry:String(p.include_geometry??true),preset:p.preset??'baseline'});
  if(p.osm_layers) q.set('osm_layers',p.osm_layers);
  if(p.footprint_ha!=null) q.set('footprint_ha',String(p.footprint_ha));
  if(p.water_mld!=null) q.set('water_mld',String(p.water_mld));
  if(p.power_mw!=null) q.set('power_mw',String(p.power_mw));
  if(p.refresh) q.set('refresh','true');
  return req<any>(`/landscape?${q.toString()}`);
 },
 legal:(id:string)=>req<{results:LegalResult[]}>(`/sites/${id}/legal`),
 scenario:(id:string,body:any)=>req<any>(`/sites/${id}/scenarios`,{method:'POST',body:JSON.stringify(body)}),
 legislation:()=>req<any[]>('/legal/legislation'),
 legislationOne:(id:string)=>req<any>(`/legal/legislation/${id}`),
 searchLegal:(q:string)=>req<any[]>('/legal/search',{method:'POST',body:JSON.stringify({query:q})}),
 sources:()=>req<any[]>('/data-sources'),
};
