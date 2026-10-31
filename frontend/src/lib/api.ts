export interface Site {id?:string; name:string; lat:number; lon:number; radius_km:number; footprint_ha:number; water_mld:number; power_mw:number; cooling:string; built_up_m2:number; created_at?:string}
export interface Finding {category:string; finding:string; risk:string; evidence:any[]; source:string; data_quality:string; last_updated:string; confidence:string; explanation:string; data_label:string}
export interface LegalResult {law:string; section:string; original_text:string; plain_explanation:string; why_applies:string; required_action:string; authority:string; compliance_status:string; consequence_type:string; potential_consequences:string; source_url:string; effective_date:string; amendment_status:string; last_verified:string; trigger:string}
export const API='/api/v1';
async function req<T>(url:string, opts?:RequestInit):Promise<T>{ const r=await fetch(API+url,{headers:{'Content-Type':'application/json'},...opts}); if(!r.ok) throw new Error(await r.text()); return r.json(); }
export const api={
 createSite:(s:Site)=>req<Site>('/sites',{method:'POST',body:JSON.stringify(s)}),
 listSites:()=>req<Site[]>('/sites'),
 analyze:(id:string)=>req<any>(`/sites/${id}/analyze`,{method:'POST'}),
 legal:(id:string)=>req<{results:LegalResult[]}>(`/sites/${id}/legal`),
 scenario:(id:string,body:any)=>req<any>(`/sites/${id}/scenarios`,{method:'POST',body:JSON.stringify(body)}),
 legislation:()=>req<any[]>('/legal/legislation'),
 legislationOne:(id:string)=>req<any>(`/legal/legislation/${id}`),
 searchLegal:(q:string)=>req<any[]>('/legal/search',{method:'POST',body:JSON.stringify({query:q})}),
 sources:()=>req<any[]>('/data-sources'),
};
