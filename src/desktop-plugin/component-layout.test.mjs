import assert from 'node:assert/strict'
import { componentAwarePositions, distributedLabelIds } from './component-layout.mjs'

const finite = positions => [...positions.values()].every(p => Number.isFinite(p.x) && Number.isFinite(p.y))
const bounds = positions => {
  const ps=[...positions.values()], xs=ps.map(p=>p.x), ys=ps.map(p=>p.y)
  return {w:Math.max(...xs)-Math.min(...xs),h:Math.max(...ys)-Math.min(...ys)}
}

// A realistic disconnected graph: one linked core, smaller islands, and many isolated notes.
const nodes=Array.from({length:223},(_,i)=>({id:String(i),degree:0}))
const edges=[]
for(let i=1;i<90;i++) edges.push({source:String(i-1),target:String(i)})
for(let base=90;base<150;base+=10) for(let i=base+1;i<base+10;i++) edges.push({source:String(i-1),target:String(i)})
for(let i=150;i<188;i+=2) edges.push({source:String(i),target:String(i+1)})
for(const e of edges){nodes[+e.source].degree++;nodes[+e.target].degree++}
const a=componentAwarePositions(nodes,edges)
const b=componentAwarePositions(nodes,edges)
assert.equal(a.size,nodes.length,'positions every node')
assert.ok(finite(a),'all positions are finite')
assert.deepEqual([...a],[...b],'layout is deterministic')
const bb=bounds(a), ratio=Math.max(bb.w/bb.h,bb.h/bb.w)
assert.ok(ratio<=2.5,`packed graph aspect ratio ${ratio.toFixed(2)} should be balanced`)
const core=[...Array(90).keys()].map(i=>a.get(String(i)))
const coreCenter=core.reduce((p,q)=>({x:p.x+q.x/core.length,y:p.y+q.y/core.length}),{x:0,y:0})
assert.ok(Math.hypot(coreCenter.x,coreCenter.y)<5,'largest component is centred')
const coreRadius=Math.max(...core.map(p=>Math.hypot(p.x,p.y)))
for(let i=188;i<223;i++) assert.ok(Math.hypot(a.get(String(i)).x,a.get(String(i)).y)>coreRadius,'singletons remain peripheral')
const labels=distributedLabelIds(nodes,edges,34)
assert.equal(labels.size,34,'label budget is exact')
assert.ok([...labels].some(id=>+id<90),'linked core gets a label')
assert.ok([...labels].some(id=>+id>=188),'peripheral isolates get labels too')

const empty=componentAwarePositions([],[])
assert.equal(empty.size,0)
const lone=componentAwarePositions([{id:'x',degree:0}],[])
assert.deepEqual(lone.get('x'),{x:0,y:0})
console.log(`PASS component-aware layout: ${nodes.length} finite deterministic positions, ${Math.round(bb.w)}×${Math.round(bb.h)} (${ratio.toFixed(2)}:1)`)
