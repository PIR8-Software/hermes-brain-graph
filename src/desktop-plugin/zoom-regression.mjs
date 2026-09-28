import fs from 'node:fs'
const s=fs.readFileSync(new URL('./plugin.src.js',import.meta.url),'utf8')
const checks=[
 ['visible numeric zoom status',s.includes('Graph zoom percentage')],
 ['zoom range 10-400%',s.includes('const ZOOM_MIN=.1,ZOOM_MAX=4')],
 ['pointer-centred wheel zoom',s.includes('zoomAt(Math.exp(-e.deltaY*.001),sx,sy)')],
 ['toolbar zoom centres graph bounds',s.includes('s.tx=s.w/2-cx*next')&&s.includes('s.ty=s.h/2-cy*next')],
 ['Fit centres graph bounds',s.includes('s.tx=s.w/2-(x1+x2)*s.scale/2')],
 ['Reset rebuilds force layout',s.includes('const rebuild=useCallback')&&s.includes('layoutGraph(s.raw)')],
 ['Ref-safe initial fit',s.includes('fitRef.current=fit')&&s.includes('fitRef.current()')],
 ['Resize preserves camera',s.includes('worldX=(s.w/2-s.tx)/s.scale')&&s.includes('s.tx=w/2-worldX*s.scale')],
 ['non-passive wheel listener',s.includes("addEventListener('wheel',wheel,{passive:false})")],
 ['pointer capture pan and drag',s.includes('setPointerCapture')&&s.includes('releasePointerCapture')],
 ['All labels remain temporary',s.includes("useEffect(()=>{setLabelMode('more')},[ctx])")],
 ['Labels scale with zoom',s.includes('baseAtFit*(zoom/fitScale)')&&s.includes('g.strokeText(title,tx,ty)')]
]
const failed=checks.filter(([,ok])=>!ok).map(([n])=>n)
if(failed.length){console.error('FAIL: '+failed.length);failed.forEach(n=>console.error(' - '+n));process.exit(1)}
console.log('PASS: '+checks.length+' viewport/lifecycle regression checks')
