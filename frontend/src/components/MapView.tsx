import {useEffect,useRef} from 'react'; import maplibregl from 'maplibre-gl'; import 'maplibre-gl/dist/maplibre-gl.css';
export default function MapView({lat,lon,radius,features,onPick}:{lat:number;lon:number;radius:number;features:any[];onPick:(lat:number,lon:number)=>void}){
 const ref=useRef<HTMLDivElement>(null); const mapRef=useRef<any>(null);
 useEffect(()=>{ if(!ref.current) return; const map=new maplibregl.Map({container:ref.current, style:{version:8,sources:{osm:{type:'raster',tiles:['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],tileSize:256,attribution:'© OpenStreetMap contributors'}},layers:[{id:'osm',type:'raster',source:'osm'}]}, center:[lon,lat], zoom:10});
 map.addControl(new maplibregl.NavigationControl()); map.on('click',e=>onPick(+e.lngLat.lat.toFixed(5),+e.lngLat.lng.toFixed(5))); mapRef.current=map; return ()=>map.remove(); },[]);
 useEffect(()=>{ const map=mapRef.current; if(!map) return; map.flyTo({center:[lon,lat],duration:600});
  document.querySelectorAll('.site-marker').forEach(e=>e.remove());
  new maplibregl.Marker({color:'#059669'}).setLngLat([lon,lat]).addTo(map).getElement().classList.add('site-marker');
  const pts:any[]=[]; for(let i=0;i<=64;i++){const a=i/64*Math.PI*2; pts.push([lon+(radius/111)*Math.cos(a)/Math.cos(lat*Math.PI/180), lat+(radius/111)*Math.sin(a)]);}
  const data:any={type:'Feature',geometry:{type:'Polygon',coordinates:[pts]}};
  const draw=()=>{ if(map.getSource('radius')) map.getSource('radius').setData(data); else {map.addSource('radius',{type:'geojson',data}); map.addLayer({id:'radius-fill',type:'fill',source:'radius',paint:{'fill-color':'#059669','fill-opacity':0.12}}); map.addLayer({id:'radius-line',type:'line',source:'radius',paint:{'line-color':'#059669','line-width':2}});} };
  if(map.loaded()) draw(); else map.on('load',draw);
  features.forEach(f=>{ const colors:any={water:'#0284c7',wetland:'#7c3aed',forest:'#16a34a',protected:'#dc2626',settlement:'#d97706'}; new maplibregl.Marker({color:colors[f.kind]||'#334155'}).setLngLat([f.lon,f.lat]).setPopup(new maplibregl.Popup().setHTML(`<b>${f.name}</b><br/>${f.kind} • ${f.distance_km} km<br/><small>${f.source}</small>`)).addTo(map).getElement().classList.add('site-marker'); });
 },[lat,lon,radius,features]);
 return <div ref={ref} style={{height:420,borderRadius:16,overflow:'hidden'}} aria-label="Interactive OpenStreetMap - click to select site"/>;
}
