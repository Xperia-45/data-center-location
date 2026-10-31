import {useEffect,useRef} from 'react'; import * as THREE from 'three'; import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
export default function Timescape({site,features,temporal,scenario,showLegal}:{site:any;features:any[];temporal:string;scenario:string;showLegal:boolean}){
 const ref=useRef<HTMLDivElement>(null); const resetRef=useRef<()=>void>(()=>{});
 useEffect(()=>{ if(!ref.current) return; const w=ref.current.clientWidth,h=460;
  const scene=new THREE.Scene(); scene.background=new THREE.Color(0x0b1f17); scene.fog=new THREE.Fog(0x0b1f17,80,180);
  const cam=new THREE.PerspectiveCamera(50,w/h,0.1,500); cam.position.set(28,24,32);
  const ren=new THREE.WebGLRenderer({antialias:true}); ren.setSize(w,h); ref.current.innerHTML=''; ref.current.appendChild(ren.domElement);
  const controls=new OrbitControls(cam,ren.domElement); controls.enableDamping=true; resetRef.current=()=>{cam.position.set(28,24,32); controls.target.set(0,0,0);};
  scene.add(new THREE.AmbientLight(0xffffff,0.7)); const sun=new THREE.DirectionalLight(0xffffff,1.4); sun.position.set(20,30,10); scene.add(sun);
  // synthetic labelled terrain
  const geo=new THREE.PlaneGeometry(80,80,48,48); const pos=geo.attributes.position;
  for(let i=0;i<pos.count;i++){ const x=pos.getX(i),y=pos.getY(i); pos.setZ(i, Math.sin(x*0.15)*Math.cos(y*0.12)*1.6 + Math.max(0,(-x-15)*0.08)); }
  geo.computeVertexNormals(); const terrain=new THREE.Mesh(geo,new THREE.MeshStandardMaterial({color:0x2f6b46,roughness:1})); terrain.rotation.x=-Math.PI/2; scene.add(terrain);
  const scale = temporal==='present'?0 : scenario==='high-growth'?1.8 : scenario==='mitigation'?0.85 : 1;
  if(temporal!=='present'){
    const fp=Math.sqrt(site.footprint_ha)*3*scale;
    const mat=new THREE.MeshStandardMaterial({color: scenario==='mitigation'?0x94a3b8:0xcbd5e1, roughness:0.4, metalness:0.3});
    for(let i=0;i<Math.round(3*scale)+1;i++){ const b=new THREE.Mesh(new THREE.BoxGeometry(fp/2,3+ (temporal==='future'?2:0),fp/3),mat); b.position.set((i-1)*fp/2,1.5,0); scene.add(b); }
    const pad=new THREE.Mesh(new THREE.PlaneGeometry(fp*1.4,fp),new THREE.MeshStandardMaterial({color:0x475569,transparent:true,opacity:0.7})); pad.rotation.x=-Math.PI/2; pad.position.y=0.06; scene.add(pad);
    if(scenario==='mitigation'){ for(let i=0;i<14;i++){ const t=new THREE.Mesh(new THREE.ConeGeometry(0.8,2.6,7),new THREE.MeshStandardMaterial({color:0x22c55e})); t.position.set(-fp+ i*1.8,1.3,fp*0.8); scene.add(t);} const solar=new THREE.Mesh(new THREE.PlaneGeometry(fp,fp/2),new THREE.MeshStandardMaterial({color:0x1d4ed8})); solar.rotation.x=-Math.PI/3; solar.position.set(0,5,-fp/2); scene.add(solar); }
  }
  // features from same analysis used by map/risk
  features.slice(0,30).forEach(f=>{ const x=(f.lon-site.lon)*300, z=(site.lat-f.lat)*300; if(Math.abs(x)>38||Math.abs(z)>38) return;
    if(f.kind==='water'||f.kind==='wetland'){ const m=new THREE.Mesh(new THREE.CircleGeometry(f.kind==='wetland'?3:2.2,24),new THREE.MeshStandardMaterial({color:f.kind==='wetland'?0x8b5cf6:0x38bdf8,transparent:true,opacity:0.85})); m.rotation.x=-Math.PI/2; m.position.set(x,0.15,z); scene.add(m); }
    else if(f.kind==='forest'||f.kind==='protected'){ const m=new THREE.Mesh(new THREE.ConeGeometry(1,3,7),new THREE.MeshStandardMaterial({color:f.kind==='protected'?0xef4444:0x16a34a})); m.position.set(x,1.5,z); scene.add(m); }
    else { const m=new THREE.Mesh(new THREE.BoxGeometry(1.2,1.2,1.2),new THREE.MeshStandardMaterial({color:0xf59e0b})); m.position.set(x,0.6,z); scene.add(m); }
    if(showLegal && (f.kind==='wetland'||f.kind==='protected'||f.kind==='forest')){ const ring=new THREE.Mesh(new THREE.RingGeometry(3.4,3.8,32),new THREE.MeshBasicMaterial({color:0xef4444,side:THREE.DoubleSide})); ring.rotation.x=-Math.PI/2; ring.position.set(x,0.2,z); scene.add(ring); }
  });
  let id:number; const loop=()=>{id=requestAnimationFrame(loop); controls.update(); ren.render(scene,cam);}; loop();
  return ()=>{cancelAnimationFrame(id); ren.dispose();};
 },[site,features,temporal,scenario,showLegal]);
 return <div><div ref={ref} style={{height:460,borderRadius:16,overflow:'hidden'}}/><button className="btn mt-2" onClick={()=>resetRef.current()}>Reset camera</button><p className="text-xs opacity-70 mt-1">Terrain: synthetic illustrative mesh (labelled). Features/buildings: same site analysis features + selected scenario/temporal state. Drag rotate • scroll zoom • right-drag pan.</p></div>;
}
