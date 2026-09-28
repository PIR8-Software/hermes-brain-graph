const idOf=n=>String(n.id)
const byId=(a,b)=>String(a).localeCompare(String(b),undefined,{numeric:true,sensitivity:'base'})

export function connectedComponents(nodes,edges){
  const ids=nodes.map(idOf).sort(byId), known=new Set(ids), adjacency=new Map(ids.map(id=>[id,new Set()]))
  for(const edge of edges){const a=String(edge.source),b=String(edge.target);if(a===b||!known.has(a)||!known.has(b))continue;adjacency.get(a).add(b);adjacency.get(b).add(a)}
  const seen=new Set(),out=[]
  for(const start of ids){if(seen.has(start))continue;const stack=[start],component=[];seen.add(start);while(stack.length){const id=stack.pop();component.push(id);for(const next of [...adjacency.get(id)].sort(byId).reverse())if(!seen.has(next)){seen.add(next);stack.push(next)}}component.sort(byId);out.push(component)}
  return out.sort((a,b)=>b.length-a.length||byId(a[0],b[0]))
}

export function distributedLabelIds(nodes,edges,budget){
  if(budget>=nodes.length)return new Set(nodes.map(idOf))
  const nodeById=new Map(nodes.map(n=>[idOf(n),n])),components=connectedComponents(nodes,edges),picked=[],seen=new Set()
  const take=id=>{if(picked.length<budget&&!seen.has(id)){seen.add(id);picked.push(id)}}
  for(const component of components)take([...component].sort((a,b)=>(nodeById.get(b)?.degree||0)-(nodeById.get(a)?.degree||0)||byId(a,b))[0])
  for(const node of [...nodes].sort((a,b)=>(b.degree||0)-(a.degree||0)||byId(a.id,b.id)))take(idOf(node))
  return new Set(picked)
}

function localConcentric(ids,nodeById){
  if(ids.length===1)return new Map([[ids[0],{x:0,y:0}]])
  const ordered=[...ids].sort((a,b)=>(nodeById.get(b)?.degree||0)-(nodeById.get(a)?.degree||0)||byId(a,b))
  const positions=new Map([[ordered[0],{x:0,y:0}]])
  let at=1,ring=1
  while(at<ordered.length){const count=Math.min(ordered.length-at,Math.max(8,ring*10)),radius=ring*66;for(let i=0;i<count;i++){const angle=-Math.PI/2+2*Math.PI*i/count;positions.set(ordered[at+i],{x:radius*Math.cos(angle),y:radius*Math.sin(angle)})}at+=count;ring++}
  return positions
}

function bounds(positions){const p=[...positions.values()],xs=p.map(v=>v.x),ys=p.map(v=>v.y);return {minX:Math.min(...xs),maxX:Math.max(...xs),minY:Math.min(...ys),maxY:Math.max(...ys),w:Math.max(...xs)-Math.min(...xs),h:Math.max(...ys)-Math.min(...ys)}}
function spiralCell(index){if(index===0)return [0,0];const ring=Math.ceil((Math.sqrt(index+1)-1)/2),side=ring*2,offset=index-(2*ring-1)**2;if(offset<side)return [ring,-ring+1+offset];if(offset<side*2)return [ring-1-(offset-side),ring];if(offset<side*3)return [-ring,ring-1-(offset-side*2)];return [-ring+1+(offset-side*3),-ring]}

export function componentAwarePositions(nodes,edges){
  if(!nodes.length)return new Map()
  if(nodes.length===1)return new Map([[idOf(nodes[0]),{x:0,y:0}]])
  const nodeById=new Map(nodes.map(n=>[idOf(n),n])),components=connectedComponents(nodes,edges),connected=components.filter(c=>c.length>1),singletons=components.filter(c=>c.length===1),result=new Map()
  const layouts=connected.map(ids=>{const positions=localConcentric(ids,nodeById),bb=bounds(positions);return {ids,positions,bb,radius:Math.max(34,Math.hypot(bb.w,bb.h)/2+24)}})
  const placePart=(part,cx,cy)=>{for(const [id,p] of part.positions)result.set(id,{x:p.x-part.bb.minX-part.bb.w/2+cx,y:p.y-part.bb.minY-part.bb.h/2+cy})}
  let coreRadius=72
  if(layouts.length){const core=layouts[0];placePart(core,0,0);coreRadius=core.radius}
  const golden=Math.PI*(3-Math.sqrt(5)),placed=[]
  for(let i=1;i<layouts.length;i++){
    const part=layouts[i];let attempt=0,cx=0,cy=0
    while(attempt<240){const angle=(i+attempt*.31)*golden,r=coreRadius+part.radius+56+18*Math.sqrt(i+attempt);cx=Math.cos(angle)*r;cy=Math.sin(angle)*r;if(placed.every(p=>Math.hypot(cx-p.x,cy-p.y)>=part.radius+p.radius+30))break;attempt++}
    placePart(part,cx,cy);placed.push({x:cx,y:cy,radius:part.radius})
  }
  const islandRadius=Math.max(coreRadius,...placed.map(p=>Math.hypot(p.x,p.y)+p.radius),72)
  const singletonPlaced=[]
  singletons.forEach((component,i)=>{let attempt=0,x=0,y=0;while(attempt<300){const n=i+attempt*.37+1,angle=n*golden,r=islandRadius+54+22*Math.sqrt(n);x=Math.cos(angle)*r;y=Math.sin(angle)*r;const clearParts=placed.every(p=>Math.hypot(x-p.x,y-p.y)>=p.radius+22),clearSingles=singletonPlaced.every(p=>Math.hypot(x-p.x,y-p.y)>=28);if(clearParts&&clearSingles)break;attempt++}result.set(component[0],{x,y});singletonPlaced.push({x,y})})
  return result
}
