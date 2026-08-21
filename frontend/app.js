/* DroneFleet frontend — a GBA-style overworld that happens to be a ground
   control station.

   Strictly an observer: it receives world snapshots, plan updates and raw
   protocol messages over one WebSocket, and sends command strings back. It
   holds no mission state of its own, which is what would let this same page be
   pointed at real hardware.                                                  */

'use strict';

// ============================================================ config
const TILES   = 60;              // the world is a 60x60 tile overworld
const SRC     = 8;               // each tile is drawn from an 8x8 pixel cell
const SEED    = 1337;

const PAL = {
  grassA:'#78c850', grassB:'#68b840', grassC:'#88d860',
  tree1:'#388030',  tree2:'#205020', trunk:'#785038',
  waterA:'#6890f0', waterB:'#5878d8', waterC:'#88b0f8',
  sand:'#e8d8a0',   sand2:'#d8c078',
  rockA:'#a8a898',  rockB:'#888878', rockC:'#c0c0b0',
  path:'#d8c8a0',   path2:'#c0b088',
  roof:'#d84848',   roof2:'#a83030', wall:'#f8f8f0', door:'#785038',
};

// Two independent axes for a contact:
//   KIND  -> hue + shape  (what the thing is)
//   STAGE -> fill weight  (how far through the pipeline it is)
// Keeping them separate means you can read either without decoding the other.
const KIND = {
  unknown:  {hue:'#c8c8c8', dark:'#6a6a6a', label:'UNKNOWN'},
  hostile:  {hue:'#d84848', dark:'#7a1c1c', label:'HOSTILE'},
  friendly: {hue:'#48b048', dark:'#1f6a2a', label:'FRIENDLY'},
  survivor: {hue:'#f8a030', dark:'#8a4e08', label:'SURVIVOR'},
  defect:   {hue:'#f8d030', dark:'#8a6a08', label:'DEFECT'},
};

const ROLE_COLOR = {
  area_search:'#48c8c0', classify_survivor:'#9868d8', classify_iff:'#9868d8',
  classify_defect:'#9868d8', deliver_payload:'#d84848', relay_comms:'#f8d030',
  intercept:'#f87038', measure_defect:'#68a8f8', file_report:'#68a8f8',
  guide_ground_team:'#48b048', track:'#48b048', assess:'#c8c8c8', loiter:'#c8c8c8',
};

// ============================================================ tiny PRNG + noise
function mulberry(seed){
  return function(){
    seed|=0; seed=seed+0x6D2B79F5|0;
    let t=Math.imul(seed^seed>>>15,1|seed);
    t=t+Math.imul(t^t>>>7,61|t)^t;
    return ((t^t>>>14)>>>0)/4294967296;
  };
}
function valueNoise(seed,size){
  const rnd=mulberry(seed), g=[];
  for(let i=0;i<size*size;i++) g.push(rnd());
  return (x,y)=>{
    const x0=Math.floor(x), y0=Math.floor(y), fx=x-x0, fy=y-y0;
    const at=(i,j)=>g[((j%size)+size)%size*size+((i%size)+size)%size];
    const s=t=>t*t*(3-2*t);
    const u=s(fx), v=s(fy);
    return (at(x0,y0)*(1-u)+at(x0+1,y0)*u)*(1-v)
         + (at(x0,y0+1)*(1-u)+at(x0+1,y0+1)*u)*v;
  };
}

// ============================================================ terrain
const T={GRASS:0,GRASS2:1,TALL:2,TREE:3,WATER:4,SAND:5,ROCK:6,PATH:7};

function buildTerrain(){
  const elevN=valueNoise(SEED,16), moisN=valueNoise(SEED+91,16), fineN=valueNoise(SEED+7,32);
  const grid=new Uint8Array(TILES*TILES);

  // a river meandering down the map — the natural landmark for "north of the river"
  const river=new Set();
  let rx=TILES*0.62;
  for(let y=0;y<TILES;y++){
    rx += (valueNoise(SEED+55,8)(y*0.28,0)-0.5)*2.4;
    rx = Math.max(5,Math.min(TILES-6,rx));
    const w = 1 + Math.floor(valueNoise(SEED+12,8)(y*0.2,0)*2);
    for(let d=-w;d<=w;d++) river.add((Math.round(rx)+d)+','+y);
  }

  for(let y=0;y<TILES;y++){
    for(let x=0;x<TILES;x++){
      const e=elevN(x/9,y/9)*0.7+fineN(x/3.5,y/3.5)*0.3;
      const m=moisN(x/7,y/7);
      let t;
      if(river.has(x+','+y))        t=T.WATER;
      else if(e<0.30)               t=T.WATER;
      else if(e<0.36)               t=T.SAND;
      else if(e>0.74)               t=T.ROCK;
      else if(m>0.62 && e<0.66)     t=T.TREE;
      else if(m>0.50)               t=T.TALL;
      else                          t=(fineN(x/2,y/2)>0.5)?T.GRASS2:T.GRASS;
      // shoreline sand
      if(t!==T.WATER && (river.has((x+1)+','+y)||river.has((x-1)+','+y)||
                         river.has(x+','+(y+1))||river.has(x+','+(y-1)))) t=T.SAND;
      grid[y*TILES+x]=t;
    }
  }
  return grid;
}

// ============================================================ tile art
function makeTile(draw){
  const c=document.createElement('canvas'); c.width=c.height=SRC;
  const g=c.getContext('2d'); g.imageSmoothingEnabled=false;
  draw(g,(x,y,col)=>{g.fillStyle=col;g.fillRect(x,y,1,1);});
  return c;
}
function buildTileset(){
  const px=(g,base)=>{g.fillStyle=base;g.fillRect(0,0,SRC,SRC);};
  return {
    [T.GRASS]:makeTile((g,p)=>{px(g,PAL.grassA);
      [[1,2],[5,1],[3,5],[6,6],[2,6]].forEach(([x,y])=>p(x,y,PAL.grassC));
      [[4,3],[0,5],[7,3]].forEach(([x,y])=>p(x,y,PAL.grassB));}),
    [T.GRASS2]:makeTile((g,p)=>{px(g,PAL.grassB);
      [[2,1],[6,2],[4,6],[0,4]].forEach(([x,y])=>p(x,y,PAL.grassA));
      [[5,5],[1,6]].forEach(([x,y])=>p(x,y,PAL.grassC));}),
    [T.TALL]:makeTile((g,p)=>{px(g,PAL.grassA);
      for(let x=0;x<SRC;x+=2){p(x,4,PAL.tree1);p(x,5,PAL.tree1);p(x+1,6,PAL.tree1);p(x,7,PAL.grassB);}
      [[1,2],[5,1]].forEach(([x,y])=>p(x,y,PAL.grassC));}),
    [T.TREE]:makeTile((g,p)=>{px(g,PAL.grassB);
      g.fillStyle=PAL.tree1; g.fillRect(1,0,6,5);
      g.fillStyle=PAL.tree2; g.fillRect(1,4,6,2); g.fillRect(0,1,1,3); g.fillRect(7,1,1,3);
      [[2,1],[4,0],[5,2]].forEach(([x,y])=>p(x,y,'#48a040'));
      g.fillStyle=PAL.trunk; g.fillRect(3,6,2,2);}),
    [T.WATER]:makeTile((g,p)=>{px(g,PAL.waterA);
      g.fillStyle=PAL.waterB; g.fillRect(0,3,8,1); g.fillRect(0,7,8,1);
      [[1,1],[5,5],[6,1]].forEach(([x,y])=>p(x,y,PAL.waterC));}),
    [T.SAND]:makeTile((g,p)=>{px(g,PAL.sand);
      [[2,2],[6,5],[4,7],[0,3]].forEach(([x,y])=>p(x,y,PAL.sand2));}),
    [T.ROCK]:makeTile((g,p)=>{px(g,PAL.rockA);
      g.fillStyle=PAL.rockB; g.fillRect(1,1,3,2); g.fillRect(4,4,3,2);
      [[6,1],[0,6],[3,6]].forEach(([x,y])=>p(x,y,PAL.rockC));}),
    [T.PATH]:makeTile((g,p)=>{px(g,PAL.path);
      [[1,3],[5,6],[3,1]].forEach(([x,y])=>p(x,y,PAL.path2));}),
  };
}

// ============================================================ sprites
function drawDrone(g,cx,cy,size,color,headingDeg,spin,dead,landed){
  const s=size/16;
  g.save(); g.translate(cx,cy); g.rotate(headingDeg*Math.PI/180); g.scale(s,s);
  g.imageSmoothingEnabled=false;

  const body=dead?'#585858':'#303840', arm=dead?'#484848':'#485868';
  // arms
  g.fillStyle=arm;
  g.fillRect(-7,-1,14,2); g.fillRect(-1,-7,2,14);
  // rotors — two blade phases so they read as spinning
  const r=3.2, ph=spin;
  [[-6,-6],[6,-6],[-6,6],[6,6]].forEach(([ox,oy])=>{
    g.fillStyle=dead?'#6a6a6a':'#20282f';
    g.beginPath(); g.arc(ox,oy,r,0,Math.PI*2); g.fill();
    if(!dead&&!landed){          // rotors only spin when it is actually flying
      g.strokeStyle=color; g.lineWidth=1.1; g.globalAlpha=.85;
      g.beginPath(); g.arc(ox,oy,r-0.6,ph,ph+2.1); g.stroke();
      g.beginPath(); g.arc(ox,oy,r-0.6,ph+Math.PI,ph+Math.PI+2.1); g.stroke();
      g.globalAlpha=1;
    }
  });
  // hull
  g.fillStyle=body; g.fillRect(-3.5,-3.5,7,7);
  g.fillStyle=color; g.fillRect(-2.5,-2.5,5,5);
  g.fillStyle=dead?'#888':'#f8f8f0'; g.fillRect(-1,-1,2,2);
  // nose
  g.fillStyle=dead?'#888':'#f8f8f0';
  g.beginPath(); g.moveTo(8,0); g.lineTo(4.5,-2.2); g.lineTo(4.5,2.2); g.closePath(); g.fill();
  g.restore();
}

// stage -> how solidly the marker is painted.
//   found       outline only, nothing known yet
//   classified  tinted, identity revealed
//   served      solid, dealt with
function stageStyle(g, kind, stage){
  const k = KIND[kind] || KIND.unknown;
  g.lineWidth = 1.4;
  g.strokeStyle = stage === 'found' ? KIND.unknown.dark : k.dark;
  if (stage === 'served')      { g.fillStyle = k.hue;      g.globalAlpha = 1;   }
  else if (stage === 'classified'){ g.fillStyle = k.hue;   g.globalAlpha = .55; }
  else                          { g.fillStyle = '#0d1420'; g.globalAlpha = .35; }
  return k;
}

function servedTick(g,ox,oy){
  g.strokeStyle='#f8f8f0'; g.lineWidth=1.5; g.lineCap='square';
  g.beginPath(); g.moveTo(ox-1.8,oy-0.2); g.lineTo(ox-0.5,oy+1.1); g.lineTo(ox+2,oy-1.8);
  g.stroke(); g.lineCap='butt';
}

// A person. Used for survivors and friendlies — someone you are helping or
// must not shoot at.
function drawPerson(g,size,kind,stage){
  const k = KIND[kind] || KIND.unknown;
  const s=size/12; g.scale(s,s);
  g.fillStyle='#00000040'; g.beginPath(); g.ellipse(0,6,4,1.6,0,0,7); g.fill();
  const skin = stage==='found' ? '#8a8a8a' : '#f8d0a8';
  g.fillStyle=skin;      g.fillRect(-2,-6,4,4);            // head
  g.fillStyle= stage==='found' ? '#5a5a5a' : '#584038';
  g.fillRect(-2,-7,4,2);                                   // hair
  stageStyle(g,kind,stage); g.fillRect(-3,-2,6,5);          // torso carries the hue
  g.globalAlpha=1; g.strokeRect(-3,-2,6,5);
  g.fillStyle=skin; g.fillRect(-4,-1,1,3); g.fillRect(3,-1,1,3);
  g.fillStyle='#384058'; g.fillRect(-2,3,1.6,3); g.fillRect(0.6,3,1.6,3);
  if(stage==='served') servedTick(g,0,0);
}

// Angular, dark, spiked — reads as a threat at a glance and shares no
// silhouette with the civilian figure.
function drawHostile(g,size,stage){
  const s=size/12; g.scale(s,s);
  g.fillStyle='#00000040'; g.beginPath(); g.ellipse(0,6,4.5,1.6,0,0,7); g.fill();
  stageStyle(g,'hostile',stage);
  g.beginPath();                       // four-pointed spike
  g.moveTo(0,-7); g.lineTo(2.2,-2.2); g.lineTo(6.5,0); g.lineTo(2.2,2.2);
  g.lineTo(0,6.5); g.lineTo(-2.2,2.2); g.lineTo(-6.5,0); g.lineTo(-2.2,-2.2);
  g.closePath(); g.fill(); g.globalAlpha=1; g.stroke();
  g.fillStyle = stage==='found' ? '#8a8a8a' : '#2a0d0d';
  g.fillRect(-1.1,-1.1,2.2,2.2);       // dark core
  if(stage==='served') servedTick(g,0.2,0.2);
}

// A hazard diamond planted on the asset. No person: a crack in a pipe is not
// a someone.
function drawDefect(g,size,stage){
  const s=size/12; g.scale(s,s);
  g.fillStyle='#00000040'; g.beginPath(); g.ellipse(0,6,4,1.5,0,0,7); g.fill();
  stageStyle(g,'defect',stage);
  g.beginPath(); g.moveTo(0,-6); g.lineTo(6,0); g.lineTo(0,6); g.lineTo(-6,0);
  g.closePath(); g.fill(); g.globalAlpha=1; g.stroke();
  g.fillStyle = stage==='found' ? '#9a9a9a' : '#3a2c04';
  g.fillRect(-0.7,-3.4,1.4,4.2); g.fillRect(-0.7,2.2,1.4,1.4);   // bang
  if(stage==='served') servedTick(g,0.2,0.4);
}

// Not yet classified: a hollow marker with a question mark. Deliberately
// shapeless — you genuinely do not know what this is yet.
function drawUnknown(g,size){
  const s=size/12; g.scale(s,s);
  g.fillStyle='#00000040'; g.beginPath(); g.ellipse(0,6,3.6,1.4,0,0,7); g.fill();
  stageStyle(g,'unknown','found');
  g.beginPath(); g.arc(0,0,5,0,Math.PI*2); g.fill(); g.globalAlpha=1; g.stroke();
  g.fillStyle='#e8e8e0';                       // 5x7 pixel '?'
  g.fillRect(-1.5,-3.5,3,1);                   // top bar
  g.fillRect(-2.5,-2.5,1,1); g.fillRect(1.5,-2.5,1,1);
  g.fillRect(1.5,-1.5,1,1);
  g.fillRect(-0.5,-0.5,2,1);                   // hook into the stem
  g.fillRect(-0.5,0.5,1,1);
  g.fillRect(-0.5,2.2,1,1);                    // dot
}

// ---------------------------------------------------------------- hazards
// The environment the incident is happening in. Purely visual — it changes
// nothing about feasibility — but "the north flood zone" should look flooded.
// Generated from the region's own coordinates so it is stable frame to frame.
const HAZARD_META = {
  flood:      {label:'FLOOD ZONE',    tint:'rgba(64,110,220,.40)', edge:'#88b0f8'},
  fire:       {label:'ACTIVE FIRE',   tint:'rgba(200,64,16,.26)',  edge:'#f87038'},
  earthquake: {label:'QUAKE DAMAGE',  tint:'rgba(90,74,58,.30)',   edge:'#a89078'},
  storm:      {label:'SEVERE STORM',  tint:'rgba(40,48,72,.42)',   edge:'#8fa8d0'},
  chemical:   {label:'CONTAMINATED',  tint:'rgba(150,90,190,.34)', edge:'#c8f83a'},
};

function hazardCells(region, cell){
  // deterministic scatter of feature points across the region
  const out=[];
  const i0=Math.floor(region.x/cell), j0=Math.floor(region.y/cell);
  const i1=Math.ceil((region.x+region.w)/cell), j1=Math.ceil((region.y+region.h)/cell);
  for(let j=j0;j<j1;j++) for(let i=i0;i<i1;i++){
    const r=mulberry(((i*73856093)^(j*19349663))>>>0)();
    out.push({i,j,r});
  }
  return out;
}

function drawHazard(g, hz, frame){
  if(!hz || !hz.kind || !hz.region || !hz.region.w) return;
  const meta = HAZARD_META[hz.kind]; if(!meta) return;
  const R = hz.region;
  const [x0,y0] = w2s(R.x, R.y), [x1,y1] = w2s(R.x+R.w, R.y+R.h);
  const cell = Math.max(120, (worldSize()/60));
  const px = (tilePx()/mPerTile()) * cell;         // one hazard cell in screen px

  g.save();
  g.beginPath(); g.rect(x0,y0,x1-x0,y1-y0); g.clip();
  g.fillStyle = meta.tint; g.fillRect(x0,y0,x1-x0,y1-y0);

  const cells = hazardCells(R, cell);

  if(hz.kind==='flood'){
    // standing water: cells below a threshold are submerged, with a crawling
    // wave line so the surface reads as liquid rather than a blue rectangle
    cells.forEach(c=>{
      if(c.r>0.62) return;
      const [sx,sy]=w2s(c.i*cell, c.j*cell);
      g.fillStyle = c.r<0.3 ? 'rgba(48,88,208,.82)' : 'rgba(104,164,248,.74)';
      g.fillRect(sx,sy,px+1,px+1);
      g.fillStyle='rgba(200,224,255,.5)';
      const yy = sy + px*0.5 + Math.sin(frame*0.06 + c.i*0.8 + c.j*0.5)*px*0.16;
      g.fillRect(sx+px*0.15, yy, px*0.7, Math.max(1,px*0.09));
    });
  }

  else if(hz.kind==='fire'){
    cells.forEach(c=>{
      const [sx,sy]=w2s(c.i*cell, c.j*cell);
      if(c.r<0.34){                                  // burnt ground
        g.fillStyle='rgba(30,20,16,.62)'; g.fillRect(sx,sy,px+1,px+1);
      }
      if(c.r>0.72){                                  // flame + smoke
        const f=(frame*0.14+c.r*11)%1;
        const h=px*(0.5+0.3*Math.sin(frame*0.2+c.i));
        g.fillStyle='#f8a030'; g.fillRect(sx+px*0.3, sy+px*0.6-h*0.5, px*0.4, h*0.5);
        g.fillStyle='#f8d030'; g.fillRect(sx+px*0.4, sy+px*0.6-h*0.28, px*0.2, h*0.28);
        g.fillStyle='#d84848'; g.fillRect(sx+px*0.34, sy+px*0.55, px*0.32, px*0.14);
        g.globalAlpha=0.30*(1-f);                    // smoke rising
        g.fillStyle='#585858';
        g.fillRect(sx+px*0.25, sy+px*0.4-f*px*2.2, px*0.5, px*0.5);
        g.globalAlpha=1;
      }
    });
  }

  else if(hz.kind==='earthquake'){
    cells.forEach(c=>{
      const [sx,sy]=w2s(c.i*cell, c.j*cell);
      if(c.r>0.5){                                   // collapsed rubble heaps
        g.fillStyle='#7a6a58'; g.fillRect(sx+px*0.08,sy+px*0.38,px*0.55,px*0.46);
        g.fillStyle='#9a8a76'; g.fillRect(sx+px*0.44,sy+px*0.5,px*0.46,px*0.36);
        g.fillStyle='#b8a894'; g.fillRect(sx+px*0.22,sy+px*0.18,px*0.34,px*0.3);
        g.fillStyle='#5a4a3a'; g.fillRect(sx+px*0.3,sy+px*0.6,px*0.18,px*0.2);
      }
      if(c.r<0.34){                                  // ground fissures
        g.strokeStyle='rgba(24,18,14,.8)'; g.lineWidth=Math.max(1,px*0.1);
        g.beginPath(); g.moveTo(sx,sy+px*0.3);
        g.lineTo(sx+px*0.45,sy+px*0.6); g.lineTo(sx+px,sy+px*0.35); g.stroke();
      }
    });
  }

  else if(hz.kind==='storm'){
    g.strokeStyle='rgba(180,204,240,.55)'; g.lineWidth=Math.max(1,px*0.06);
    const off=(frame*7)%40;
    for(let k=-40;k<(x1-x0)+40;k+=Math.max(6,px*0.55)){
      g.beginPath();
      g.moveTo(x0+k+off, y0-20); g.lineTo(x0+k+off-14, y1+20); g.stroke();
    }
    if(Math.floor(frame/9)%23===0){                  // lightning flash
      g.fillStyle='rgba(248,248,240,.30)'; g.fillRect(x0,y0,x1-x0,y1-y0);
    }
  }

  else if(hz.kind==='chemical'){
    cells.forEach(c=>{
      if(c.r<0.55) return;
      const [sx,sy]=w2s(c.i*cell, c.j*cell);
      const d=Math.sin(frame*0.05+c.i*0.7+c.j*0.4)*px*0.2;
      // acid green on a violet ground: nothing on the terrain palette is
      // anywhere near this, so contamination can never read as scenery
      g.globalAlpha=0.5; g.fillStyle='#c8f83a';
      g.beginPath(); g.arc(sx+px*0.5+d, sy+px*0.5, px*0.5, 0, Math.PI*2); g.fill();
      g.globalAlpha=0.35; g.fillStyle='#7a3a9a';
      g.beginPath(); g.arc(sx+px*0.5-d*0.6, sy+px*0.55, px*0.62, 0, Math.PI*2); g.fill();
      g.globalAlpha=1;
    });
  }

  g.restore();

  // banner on the region edge
  g.save();
  g.strokeStyle=meta.edge; g.lineWidth=2; g.setLineDash([2,4]);
  g.strokeRect(x0,y0,x1-x0,y1-y0); g.setLineDash([]);
  g.font='8px "Press Start 2P",monospace'; g.textAlign='left';
  g.lineWidth=3; g.strokeStyle='#000'; g.strokeText(meta.label, x0+4, y1+13);
  g.fillStyle=meta.edge; g.fillText(meta.label, x0+4, y1+13);
  g.restore();
}

function drawContact(g,cx,cy,size,kind,stage,bob){
  g.save(); g.translate(cx,cy+bob);
  if      (stage === 'found')      drawUnknown(g,size);
  else if (kind === 'hostile')     drawHostile(g,size,stage);
  else if (kind === 'defect')      drawDefect(g,size,stage);
  else                             drawPerson(g,size,kind,stage);
  g.globalAlpha=1; g.restore();
}

function drawBase(g,cx,cy,size){
  const s=size/16; g.save(); g.translate(cx,cy); g.scale(s,s);
  g.fillStyle='#00000040'; g.fillRect(-8,5,16,3);
  g.fillStyle=PAL.wall;  g.fillRect(-7,-2,14,8);
  g.fillStyle=PAL.roof;  g.fillRect(-8,-7,16,5);
  g.fillStyle=PAL.roof2; g.fillRect(-8,-3,16,1);
  g.fillStyle=PAL.door;  g.fillRect(-2,1,4,5);
  g.fillStyle='#88c8f8'; g.fillRect(-6,0,3,3); g.fillRect(3,0,3,3);
  g.fillStyle='#f8f8f0'; g.fillRect(-1,-6,2,3); g.fillRect(-2,-5,4,1);   // cross
  g.restore();
}

// ============================================================ state
const state={
  world:null, plan:null, selected:null, domain:'—', showHb:false,
  follow:false, fog:true, cam:{x:0,y:0,zoom:1.6}, wire:[], hello:null,
};
const terrain=buildTerrain();
const tileset=buildTileset();

// ============================================================ canvas
const canvas=document.getElementById('map');
const ctx=canvas.getContext('2d');
let cssW=0,cssH=0;

function resize(){
  const r=canvas.getBoundingClientRect();
  const dpr=Math.min(window.devicePixelRatio||1,2);
  cssW=r.width; cssH=r.height;
  canvas.width=Math.max(1,Math.round(r.width*dpr));
  canvas.height=Math.max(1,Math.round(r.height*dpr));
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.imageSmoothingEnabled=false;
  if(!resize._once){ resize._once=true; state.cam.zoom=coverZoom(); centerOn(6000,6000); }
  state.cam.zoom=Math.max(state.cam.zoom,coverZoom());
  clampCam();
}
new ResizeObserver(resize).observe(document.getElementById('stage'));

const tilePx=()=>SRC*state.cam.zoom;
const worldSize=()=>state.world?state.world.size_m:12000;
const mPerTile=()=>worldSize()/TILES;

function w2s(mx,my){
  const t=tilePx(), k=t/mPerTile();
  return [mx*k-state.cam.x, my*k-state.cam.y];
}
function s2w(sx,sy){
  const t=tilePx(), k=t/mPerTile();
  return [(sx+state.cam.x)/k,(sy+state.cam.y)/k];
}
function coverZoom(){
  // fill the stage in both axes -- a square overworld in a wide viewport must
  // cover, not letterbox, or the game window sits in black bars
  return Math.max(cssW,cssH)/(TILES*SRC);
}
function clampCam(){
  const full=TILES*tilePx();
  state.cam.x=full<=cssW?(full-cssW)/2:Math.max(0,Math.min(full-cssW,state.cam.x));
  state.cam.y=full<=cssH?(full-cssH)/2:Math.max(0,Math.min(full-cssH,state.cam.y));
}
function centerOn(mx,my){
  const t=tilePx(), k=t/mPerTile();
  state.cam.x=mx*k-cssW/2; state.cam.y=my*k-cssH/2; clampCam();
}

// ============================================================ render
let frame=0;
function render(){
  frame++;
  const t=tilePx();
  ctx.fillStyle='#000'; ctx.fillRect(0,0,cssW,cssH);

  // --- terrain -----------------------------------------------------------
  const x0=Math.max(0,Math.floor(state.cam.x/t)), y0=Math.max(0,Math.floor(state.cam.y/t));
  const x1=Math.min(TILES,Math.ceil((state.cam.x+cssW)/t)), y1=Math.min(TILES,Math.ceil((state.cam.y+cssH)/t));
  for(let y=y0;y<y1;y++){
    for(let x=x0;x<x1;x++){
      ctx.drawImage(tileset[terrain[y*TILES+x]], Math.round(x*t-state.cam.x), Math.round(y*t-state.cam.y),
                    Math.ceil(t), Math.ceil(t));
    }
  }

  const w=state.world;
  if(!w){ requestAnimationFrame(render); return; }

  const region=regionOfPlan();

  // --- environmental hazard (under the fog, over the terrain) -------------
  drawHazard(ctx, w.hazard, frame);

  // --- unswept fog inside the search box ---------------------------------
  if(state.fog && region){
    const cell=w.coverage_cell_m||200;
    const covered=new Set((w.coverage||[]).map(c=>c[0]+','+c[1]));
    const [rx0,ry0]=w2s(region.x,region.y), [rx1,ry1]=w2s(region.x+region.w,region.y+region.h);
    ctx.save();
    ctx.beginPath(); ctx.rect(rx0,ry0,rx1-rx0,ry1-ry0); ctx.clip();
    // a hazard already darkens this area; two heavy overlays stacked turn the
    // whole region to mud, so the unswept shading gets out of the way
    ctx.fillStyle = (w.hazard && w.hazard.kind) ? 'rgba(10,16,36,.30)' : 'rgba(10,16,36,.52)';
    const i0=Math.floor(region.x/cell), j0=Math.floor(region.y/cell);
    const i1=Math.ceil((region.x+region.w)/cell), j1=Math.ceil((region.y+region.h)/cell);
    for(let j=j0;j<=j1;j++) for(let i=i0;i<=i1;i++){
      if(covered.has(i+','+j)) continue;
      const [sx,sy]=w2s(i*cell,j*cell), [ex,ey]=w2s((i+1)*cell,(j+1)*cell);
      ctx.fillRect(sx,sy,ex-sx+1,ey-sy+1);
    }
    ctx.restore();
  }

  // --- mission region marquee --------------------------------------------
  if(region){
    const [rx0,ry0]=w2s(region.x,region.y), [rx1,ry1]=w2s(region.x+region.w,region.y+region.h);
    ctx.save();
    ctx.setLineDash([6,4]); ctx.lineDashOffset=-frame*0.25;
    ctx.strokeStyle='#f8d030'; ctx.lineWidth=3;
    ctx.strokeRect(rx0,ry0,rx1-rx0,ry1-ry0);
    ctx.setLineDash([]);
    ctx.fillStyle='#f8d030'; ctx.font='8px "Press Start 2P",monospace';
    ctx.fillText('SEARCH AREA', rx0+4, ry0-7);
    ctx.restore();
  }

  // --- routes -------------------------------------------------------------
  (w.drones||[]).forEach(d=>{
    if(!d.waypoints||!d.waypoints.length||!d.alive) return;
    const sel = state.selected===d.id;
    ctx.save();
    ctx.strokeStyle = sel?'#f8f8f0':'#ffffff55';
    ctx.lineWidth = sel?2:1.2;
    ctx.setLineDash([3,4]);
    ctx.beginPath();
    let [px,py]=w2s(d.x,d.y); ctx.moveTo(px,py);
    d.waypoints.forEach(p=>{const [sx,sy]=w2s(p.x,p.y); ctx.lineTo(sx,sy);});
    ctx.stroke(); ctx.restore();
  });

  // --- comms relay links --------------------------------------------------
  (w.drones||[]).forEach(d=>{
    if(!d.alive) return;
    let ax,ay;
    if(d.link_via==='direct'){ [ax,ay]=w2s(w.base.x,w.base.y); }
    else if(d.link_via&&d.link_via!=='none'){
      const r=(w.drones||[]).find(o=>o.id===d.link_via); if(!r) return;
      [ax,ay]=w2s(r.x,r.y);
    } else return;
    const [bx,by]=w2s(d.x,d.y);
    ctx.save();
    ctx.strokeStyle=d.link_via==='direct'?'#48c8c033':'#f8d03066';
    ctx.lineWidth=1; ctx.setLineDash([2,5]); ctx.lineDashOffset=frame*0.3;
    ctx.beginPath(); ctx.moveTo(ax,ay); ctx.lineTo(bx,by); ctx.stroke();
    ctx.restore();
  });

  // --- base ---------------------------------------------------------------
  {
    const [bx,by]=w2s(w.base.x,w.base.y);
    drawBase(ctx,bx,by,Math.max(22,t*2.2));
    label(bx,by+t*1.6,'BASE','#f8f8f0');
  }

  // --- contacts -----------------------------------------------------------
  (w.contacts||[]).forEach(c=>{
    if(!c.found) return;
    const [sx,sy]=w2s(c.x,c.y);
    const bob=Math.sin(frame*0.09+sx)*1.2;
    const stage = c.served ? 'served' : (c.classified ? 'classified' : 'found');
    drawContact(ctx,sx,sy,Math.max(16,t*1.6),c.kind||'unknown',stage,bob);
    if(stage==='found'){
      ctx.save(); ctx.fillStyle='#f8d030'; ctx.font='16px "Press Start 2P",monospace';
      ctx.textAlign='center'; ctx.fillText('!',sx,sy-t*1.15+Math.sin(frame*0.15)*2); ctx.restore();
    } else {
      label(sx,sy+t*1.05,(KIND[c.kind]||KIND.unknown).label,(KIND[c.kind]||KIND.unknown).hue);
    }
  });

  // --- drones -------------------------------------------------------------
  (w.drones||[]).forEach(d=>{
    const [sx,sy]=w2s(d.x,d.y);
    const color=ROLE_COLOR[d.current_verb]||roleColorOf(d.id);
    const size=Math.max(20,t*2.0);
    // sensor footprint while searching
    if(d.alive&&d.current_verb==='area_search'&&d.swath_m){
      const k=tilePx()/mPerTile();
      ctx.save(); ctx.globalAlpha=.16; ctx.fillStyle='#48c8c0';
      ctx.beginPath(); ctx.arc(sx,sy,(d.swath_m/2)*k,0,Math.PI*2); ctx.fill(); ctx.restore();
    }
    if(!d.link_ok&&d.alive){
      ctx.save(); ctx.strokeStyle='#d84848'; ctx.lineWidth=2; ctx.globalAlpha=.5+0.5*Math.sin(frame*0.2);
      ctx.beginPath(); ctx.arc(sx,sy,size*0.72,0,Math.PI*2); ctx.stroke(); ctx.restore();
    }
    const landed = d.airborne===false && d.alive;
    if(landed){                                   // a pad ring, so parked reads
      ctx.save(); ctx.strokeStyle='#00000055'; ctx.lineWidth=2;
      ctx.beginPath(); ctx.arc(sx,sy,size*0.52,0,Math.PI*2); ctx.stroke(); ctx.restore();
    }
    ctx.save(); if(landed) ctx.globalAlpha=0.82;
    drawDrone(ctx,sx,sy,size,color,d.heading_deg,frame*0.55,!d.alive,landed);
    ctx.restore();

    if(state.selected===d.id){
      ctx.save();
      ctx.fillStyle='#f8f8f0';
      const ay=sy-size*0.9+Math.sin(frame*0.14)*2.5;
      ctx.beginPath(); ctx.moveTo(sx,ay+7); ctx.lineTo(sx-6,ay-2); ctx.lineTo(sx+6,ay-2); ctx.closePath(); ctx.fill();
      ctx.strokeStyle='#f8d030'; ctx.lineWidth=2; ctx.setLineDash([4,3]); ctx.lineDashOffset=-frame*0.4;
      ctx.beginPath(); ctx.arc(sx,sy,size*0.85,0,Math.PI*2); ctx.stroke();
      ctx.restore();
    }
    label(sx,sy+size*0.78,d.name.toUpperCase(),d.alive?'#f8f8f0':'#d84848');
  });

  // --- legend: only the kinds actually present, so it never clutters -------
  {
    const seen=[];
    (w.contacts||[]).forEach(c=>{
      if(!c.found) return;
      const k=c.classified?(c.kind||'unknown'):'unknown';
      if(!seen.includes(k)) seen.push(k);
    });
    if(seen.length){
      ctx.save();
      const lh=13, pad=6, bw=104, bh=pad*2+seen.length*lh;
      const bx=10, by=cssH-bh-26;
      ctx.fillStyle='rgba(16,28,52,.86)'; ctx.fillRect(bx,by,bw,bh);
      ctx.strokeStyle='#000'; ctx.lineWidth=2; ctx.strokeRect(bx,by,bw,bh);
      ctx.font='8px "Press Start 2P",monospace'; ctx.textAlign='left';
      seen.forEach((k,i)=>{
        const meta=KIND[k]||KIND.unknown, yy=by+pad+i*lh;
        ctx.fillStyle=meta.hue; ctx.fillRect(bx+pad,yy+2,7,7);
        ctx.strokeStyle=meta.dark; ctx.lineWidth=1; ctx.strokeRect(bx+pad,yy+2,7,7);
        ctx.fillStyle='#e8eef8'; ctx.fillText(meta.label,bx+pad+13,yy+9);
      });
      ctx.restore();
    }
  }

  if(state.follow&&state.selected){
    const d=(w.drones||[]).find(x=>x.id===state.selected);
    if(d) centerOn(d.x,d.y);
  }

  requestAnimationFrame(render);
}

function label(x,y,text,color){
  ctx.save();
  ctx.font='8px "Press Start 2P",monospace'; ctx.textAlign='center';
  ctx.lineWidth=3; ctx.strokeStyle='#000'; ctx.strokeText(text,x,y);
  ctx.fillStyle=color; ctx.fillText(text,x,y);
  ctx.restore();
}
function roleColorOf(id){
  const d=state.plan&&state.plan.fleet&&state.plan.fleet.find(f=>f.id===id);
  if(d&&d.capabilities&&d.capabilities.length){
    for(const c of d.capabilities){ if(ROLE_COLOR[c.verb]&&c.verb!=='loiter') return ROLE_COLOR[c.verb]; }
  }
  return '#c8c8c8';
}
function regionOfPlan(){
  if(!state.plan||!state.plan.tasks) return null;
  for(const t of state.plan.tasks){ if(t.params&&t.params.region&&t.params.region.w) return t.params.region; }
  return null;
}

// ============================================================ interaction
canvas.addEventListener('click',e=>{
  const r=canvas.getBoundingClientRect();
  const sx=e.clientX-r.left, sy=e.clientY-r.top;
  const w=state.world; if(!w) return;
  let best=null,bestD=1e9;
  (w.drones||[]).forEach(d=>{
    const [dx,dy]=w2s(d.x,d.y);
    const dist=Math.hypot(dx-sx,dy-sy);
    if(dist<bestD){bestD=dist;best=d;}
  });
  if(best&&bestD<Math.max(26,tilePx()*1.4)){ select(best.id); }
  else { select(null); }
});

let drag=null;
canvas.addEventListener('mousedown',e=>{drag={x:e.clientX,y:e.clientY,cx:state.cam.x,cy:state.cam.y,moved:false};});
window.addEventListener('mousemove',e=>{
  if(!drag) return;
  const dx=e.clientX-drag.x, dy=e.clientY-drag.y;
  if(Math.abs(dx)+Math.abs(dy)>3){drag.moved=true;state.follow=false;syncFollowBtn();}
  state.cam.x=drag.cx-dx; state.cam.y=drag.cy-dy; clampCam();
});
window.addEventListener('mouseup',()=>{drag=null;});
canvas.addEventListener('wheel',e=>{e.preventDefault();zoom(e.deltaY<0?1.15:1/1.15);},{passive:false});

function zoom(f){
  const before=tilePx();
  state.cam.zoom=Math.max(coverZoom(),Math.min(8,state.cam.zoom*f));
  const after=tilePx(), k=after/before;
  state.cam.x=(state.cam.x+cssW/2)*k-cssW/2;
  state.cam.y=(state.cam.y+cssH/2)*k-cssH/2;
  clampCam();
}
document.getElementById('btn-zoomin').onclick=()=>zoom(1.25);
document.getElementById('btn-zoomout').onclick=()=>zoom(1/1.25);
document.getElementById('btn-fog').onclick=e=>{
  state.fog=!state.fog; e.target.textContent='FOG: '+(state.fog?'ON':'OFF');
};
document.getElementById('btn-follow').onclick=()=>{
  state.follow=!state.follow; syncFollowBtn();
};
function syncFollowBtn(){
  document.getElementById('btn-follow').textContent='FOLLOW: '+(state.follow?'ON':'OFF');
}

document.querySelectorAll('.tab').forEach(tab=>{
  tab.onclick=()=>showTab(tab.dataset.tab);
});
document.getElementById('btn-hb').onclick=e=>{
  state.showHb=!state.showHb;
  e.target.textContent='HEARTBEATS: '+(state.showHb?'ON':'OFF');
};
document.getElementById('btn-clearwire').onclick=()=>{
  document.getElementById('wire-log').innerHTML=''; state.wire.length=0;
};

function showTab(name){
  const tab=document.querySelector('.tab[data-tab="'+name+'"]');
  if(!tab) return;
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  tab.classList.add('active');
  document.getElementById('panel-'+name).classList.add('active');
}
window.addEventListener('hashchange',()=>showTab(location.hash.slice(1)));

function select(id){
  state.selected=id;
  renderParty();
  if(id&&state.follow){
    const d=(state.world.drones||[]).find(x=>x.id===id);
    if(d) centerOn(d.x,d.y);
  }
}

// ============================================================ party + summary
function droneRecord(id){
  return (state.plan&&state.plan.fleet||[]).find(f=>f.id===id)||null;
}
function barClass(p){ return p>50?'':(p>20?'warn':'crit'); }

function renderParty(){
  const list=document.getElementById('party-list');
  const empty=document.getElementById('party-empty');
  const drones=(state.world&&state.world.drones)||[];
  empty.hidden = drones.length>0;
  list.innerHTML='';

  drones.forEach(d=>{
    const rec=droneRecord(d.id);
    const card=document.createElement('div');
    card.className='party-card'+(state.selected===d.id?' sel':'')+(d.alive?'':' dead');
    card.onclick=()=>select(d.id);

    const cv=document.createElement('canvas'); cv.width=cv.height=32;
    const g=cv.getContext('2d'); g.imageSmoothingEnabled=false;
    drawDrone(g,16,16,28,ROLE_COLOR[d.current_verb]||roleColorOf(d.id),-90,frame*0.3,
              !d.alive, d.airborne===false && d.alive);

    const info=document.createElement('div');
    const verbs=rec?rec.capabilities.map(c=>c.verb).filter(v=>v!=='loiter'):[];
    info.innerHTML=
      `<div class="pc-name"><span>${esc(d.name.toUpperCase())}</span><em>${esc(d.status)}</em></div>`+
      `<div class="pc-role">${esc(verbs.join(' · ')||'—')}</div>`+
      `<div class="bar ${barClass(d.battery_pct)}"><span style="width:${d.battery_pct}%"></span></div>`+
      `<div class="bar-label"><span>BAT ${d.battery_pct.toFixed(0)}%</span>`+
      `<span>${d.link_ok?'LINK OK':'NO LINK'}</span></div>`;

    card.appendChild(cv); card.appendChild(info);
    list.appendChild(card);
  });

  renderSummary();
}

function renderSummary(){
  const box=document.getElementById('summary');
  const d=(state.world&&state.world.drones||[]).find(x=>x.id===state.selected);
  if(!d){ box.hidden=true; return; }
  box.hidden=false;

  const rec=droneRecord(d.id)||{capabilities:[],sensors:[],constraints:{}};
  const c=rec.constraints||{};
  const task=taskOf(d.current_task), next=taskOf(d.next_task);

  box.innerHTML=`
    <div class="sum-head">
      <canvas width="44" height="44" id="sum-sprite"></canvas>
      <div>
        <div class="sum-title">${esc(d.name.toUpperCase())}</div>
        <div class="sum-id">${esc(d.id)} · ${esc(d.status)}</div>
      </div>
    </div>

    <div class="sum-section">OBJECTIVE</div>
    <div class="objective">
      <span class="lbl">NOW</span>
      ${task?`<b>${esc(task.verb)}</b> (${esc(task.id)})
        <div class="prog"><span style="width:${(d.task_progress*100).toFixed(0)}%"></span></div>
        <div style="font-size:8px;color:#777;margin-top:4px">${(d.task_progress*100).toFixed(0)}% · ${esc(task.state)}</div>`
       :`<span style="color:#888">standing by</span>`}
    </div>
    <div class="objective next">
      <span class="lbl">NEXT</span>
      ${next?`<b>${esc(next.verb)}</b> (${esc(next.id)})
              <div style="font-size:8px;color:#777;margin-top:4px">${esc(next.state)}${
                next.depends_on&&next.depends_on.length?' · waits on '+esc(next.depends_on.join(',')):''}</div>`
            :`<span style="color:#888">nothing queued</span>`}
    </div>

    <div class="sum-section">POSITION</div>
    <dl class="kv">
      <dt>EASTING</dt><dd>${(d.x/1000).toFixed(2)} km</dd>
      <dt>NORTHING</dt><dd>${(d.y/1000).toFixed(2)} km</dd>
      <dt>ALTITUDE</dt><dd>${d.z.toFixed(0)} m</dd>
      <dt>HEADING</dt><dd>${d.heading_deg.toFixed(0)}°</dd>
      <dt>SPEED</dt><dd>${d.speed_ms.toFixed(1)} m/s</dd>
      <dt>FLOWN</dt><dd>${d.distance_km.toFixed(2)} km</dd>
    </dl>

    <div class="sum-section">SYSTEMS</div>
    <div class="bar ${barClass(d.battery_pct)}"><span style="width:${d.battery_pct}%"></span></div>
    <div class="bar-label"><span>BATTERY</span><span>${d.battery_pct.toFixed(0)}%</span></div>
    <dl class="kv" style="margin-top:8px">
      <dt>LINK</dt><dd>${d.link_ok?esc(d.link_via==='direct'?'DIRECT':'VIA '+d.link_via):'LOST'}</dd>
      <dt>SIGNAL</dt><dd>${d.link_dbm.toFixed(0)} dBm</dd>
      <dt>RADIO</dt><dd>${((c.comms_range_m||0)/1000).toFixed(1)} km</dd>
      <dt>ENDURANCE</dt><dd>${(c.endurance_min||0).toFixed(0)} min</dd>
      <dt>SWATH</dt><dd>${(d.swath_m||0).toFixed(0)} m</dd>
      <dt>PAYLOAD</dt><dd>${(c.payload_kg||0).toFixed(1)} kg</dd>
    </dl>

    <div class="sum-section">CAPABILITIES</div>
    <div class="chips">
      ${(rec.capabilities||[]).map(x=>`<span class="chip">${esc(x.verb)}</span>`).join('')||'<span class="chip off">none</span>'}
    </div>
    <div class="sum-section">SENSORS</div>
    <div class="chips">
      ${(rec.sensors||[]).map(s=>`<span class="chip sensor">${esc(s)}</span>`).join('')||'<span class="chip off">none</span>'}
    </div>`;

  const g=document.getElementById('sum-sprite').getContext('2d');
  g.imageSmoothingEnabled=false;
  drawDrone(g,22,22,40,ROLE_COLOR[d.current_verb]||roleColorOf(d.id),-90,frame*0.3,
            !d.alive, d.airborne===false && d.alive);
}

function taskOf(id){
  if(!id||!state.plan) return null;
  return (state.plan.tasks||[]).find(t=>t.id===id)||null;
}

// ============================================================ mission tab
function renderMission(){
  const body=document.getElementById('mission-body');
  const p=state.plan;
  if(!p||!p.verdict){ body.innerHTML='<div class="empty">NO MISSION EVALUATED YET.</div>'; return; }

  let html=`<div class="m-goal">VERDICT <b>${esc(p.verdict)}</b><br>DOMAIN ${esc(p.domain||'—')}<br>GOAL ${esc(p.goal||'—')}</div>`;

  (p.gaps||[]).forEach(g=>{
    html+=`<div class="gapbox ${g.severity==='degraded'?'degraded':''}">
      <b>${esc(g.reason)} · ${esc(g.needed)}</b>${esc(g.why)}
      ${g.suggestion?`<i>→ ${esc(g.suggestion)}</i>`:''}</div>`;
  });

  (p.tasks||[]).forEach(t=>{
    html+=`<div class="task ${esc(t.state)}">
      <div class="t-top"><span>${esc(t.id)} ${esc(t.verb)}</span><span class="t-state">${esc(t.state)}</span></div>
      <div class="t-meta">→ ${esc(t.assignee_name||'UNASSIGNED')}${
        t.depends_on&&t.depends_on.length?' · after '+esc(t.depends_on.join(',')):''} · ~${Math.round(t.est_duration_s)}s</div>
      ${t.state==='RUNNING'?`<div class="prog"><span style="width:${(t.progress*100).toFixed(0)}%"></span></div>`:''}
      ${t.note?`<div class="t-note">${esc(t.note)}</div>`:''}
    </div>`;
  });

  (p.notes||[]).forEach(n=>{ html+=`<div class="gapbox degraded">${esc(n)}</div>`; });
  body.innerHTML=html;
}

// ============================================================ protocol tab
function pushWire(topic,env){
  // heartbeats are ~80% of the traffic and bury the interesting exchanges
  if(env.type==='HEARTBEAT'&&!state.showHb) return;
  const short=topic.replace('fleet/','').replace('/inbox','');
  state.wire.push({t:new Date(),topic:short,env});
  if(state.wire.length>400) state.wire.shift();
  const log=document.getElementById('wire-log');
  const row=document.createElement('div');
  row.className='wire-row';
  const time=new Date().toLocaleTimeString('en-GB',{hour12:false}).slice(3);
  const body = env.type==='HEARTBEAT'
    ? `<span class="src">${esc(env.src)}</span> ♥ ${env.payload&&env.payload.battery_pct!=null?env.payload.battery_pct+'%':''}`
    : `<span class="src">${esc(env.src)}</span> → ${esc(env.dst||'?')} ${
        env.corr_id?'['+esc(env.corr_id)+']':''} ${esc(summarisePayload(env))}`;
  row.innerHTML=`<div class="wire-t">${time}</div>
    <div class="wire-body"><span class="wire-type">${esc(env.type||'?')}</span> ${body}</div>`;
  log.appendChild(row);
  while(log.children.length>400) log.removeChild(log.firstChild);
  log.scrollTop=log.scrollHeight;
}
function summarisePayload(env){
  const p=env.payload||{};
  if(env.type==='TASK_ASSIGN') return p.verb||'';
  if(env.type==='TASK_COMPLETE') return (p.verb||'')+' '+JSON.stringify(p.result||{}).slice(0,60);
  if(env.type==='TASK_PROGRESS') return Math.round((p.progress||0)*100)+'%';
  if(env.type==='TASK_REJECT') return p.reason||'';
  if(env.type==='CAPABILITY_ANNOUNCE') return '['+(p.capabilities||[]).map(c=>c.verb).join(',')+']';
  return '';
}

// ============================================================ dialogue box
const logEl=document.getElementById('log');
let typing=false; const queue=[];

function say(kind,text,extra){
  queue.push({kind,text,extra:extra||{}});
  if(!typing) drain();
}
function drain(){
  if(!queue.length){typing=false;return;}
  typing=true;
  const {kind,text,extra}=queue.shift();
  const p=document.createElement('p');
  p.className='line '+kind+(extra.verdict?' '+extra.verdict:'');
  logEl.appendChild(p);
  while(logEl.children.length>300) logEl.removeChild(logEl.firstChild);

  // the signature effect — but only while the queue is short, so a burst of
  // protocol chatter never makes the box lag behind the fleet
  if(extra.replay||queue.length>2||text.length>150||prefersReduced()){
    p.textContent=text; logEl.scrollTop=logEl.scrollHeight; drain(); return;
  }
  let i=0;
  const tick=()=>{
    p.textContent=text.slice(0,++i);
    logEl.scrollTop=logEl.scrollHeight;
    if(i<text.length) setTimeout(tick,9); else drain();
  };
  tick();
}
function prefersReduced(){
  return window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function flashVerdict(v){
  const badge=document.getElementById('verdict-badge');
  const txt=document.getElementById('verdict-text');
  txt.textContent=v;
  txt.parentElement.className='verdict-inner v-'+v;
  badge.hidden=false;
  clearTimeout(flashVerdict._t);
  flashVerdict._t=setTimeout(()=>{badge.hidden=true;},1900);
}

// ============================================================ input
const input=document.getElementById('input');
const history=[]; let hIdx=-1;

input.addEventListener('keydown',e=>{
  if(e.key==='Enter'){
    const text=input.value.trim();
    if(!text) return;
    history.push(text); hIdx=history.length;
    send({cmd:'input',text});
    input.value='';
  } else if(e.key==='ArrowUp'){
    if(hIdx>0){hIdx--;input.value=history[hIdx];e.preventDefault();}
  } else if(e.key==='ArrowDown'){
    if(hIdx<history.length-1){hIdx++;input.value=history[hIdx];}
    else {hIdx=history.length;input.value='';}
    e.preventDefault();
  }
});
window.addEventListener('keydown',e=>{
  if(e.target!==input && e.key.length===1 && !e.ctrlKey && !e.metaKey) input.focus();
});

// ============================================================ websocket
let ws=null;
function connect(){
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
  ws.onopen=()=>say('system','Link established.');
  ws.onclose=()=>{ say('error','Connection lost. Retrying…'); setTimeout(connect,1500); };
  ws.onmessage=ev=>{
    let m; try{ m=JSON.parse(ev.data);}catch{ return; }
    if(m.ev==='hello'){
      state.hello=m;
      document.getElementById('hud-domain').innerHTML='DOMAIN <b>'+esc(m.domain||'—')+'</b>';
    } else if(m.ev==='world'){
      state.world=m.data;
      if(!state.selected&&m.data.drones&&m.data.drones.length) state.selected=m.data.drones[0].id;
      if(state.selected&&!m.data.drones.some(d=>d.id===state.selected))
        state.selected=m.data.drones.length?m.data.drones[0].id:null;
      document.getElementById('hud-wind').innerHTML=
        'WIND <b>'+m.data.wind.speed_ms.toFixed(1)+' m/s '+m.data.wind.dir_deg.toFixed(0)+'°</b>';
      document.getElementById('hud-clock').innerHTML='T+<b>'+Math.floor(m.data.t)+'s</b>';
      renderParty();
    } else if(m.ev==='plan'){
      state.plan=m.data;
      if(m.data.domain) document.getElementById('hud-domain').innerHTML='DOMAIN <b>'+esc(m.data.domain)+'</b>';
      renderMission(); renderParty();
    } else if(m.ev==='console'){
      say(m.data.kind||'system',m.data.text||'',m.data);
      if(m.data.kind==='verdict'&&m.data.verdict&&!m.data.replay) flashVerdict(m.data.verdict);
    } else if(m.ev==='wire'){
      pushWire(m.topic,m.data);
    }
  };
}
function send(obj){ if(ws&&ws.readyState===1) ws.send(JSON.stringify(obj)); }

function esc(s){
  return String(s==null?'':s).replace(/[&<>"']/g,c=>(
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ============================================================ boot
resize();
syncFollowBtn();
if(location.hash) showTab(location.hash.slice(1));
connect();
requestAnimationFrame(render);
