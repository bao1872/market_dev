/** Scatter glyphs only; candles, time scale, autoscale and coordinates belong to lightweight-charts. */
import type {ISeriesPrimitive, ISeriesPrimitivePaneRenderer, SeriesAttachedParameter, Time} from 'lightweight-charts'
import {scoreValue,type ScorePoint} from './scorePoints'
export function scorePrimitive(points:ScorePoint[],raw:boolean):ISeriesPrimitive<Time>{
  let attached:SeriesAttachedParameter<Time>|undefined
  const coords=()=>!attached?[]:points.flatMap(p=>{
    const x=attached!.chart.timeScale().timeToCoordinate(p.date as Time),y=attached!.series.priceToCoordinate(scoreValue(p,raw))
    return x==null||y==null?[]:[{p,x,y}]
  })
  const renderer:ISeriesPrimitivePaneRenderer={draw(target){target.useMediaCoordinateSpace(({context:ctx})=>{
    for(const {p,x,y} of coords()){
      ctx.beginPath();ctx.fillStyle=p.color;ctx.strokeStyle='#141d29';ctx.lineWidth=1
      const r=p.stage==='D3'?5:4
      if(p.stage==='D0')ctx.rect(x-r,y-r,r*2,r*2)
      else if(p.stage==='D1')ctx.arc(x,y,r,0,Math.PI*2)
      else if(p.stage==='D2'){ctx.moveTo(x,y-r);ctx.lineTo(x+r,y);ctx.lineTo(x,y+r);ctx.lineTo(x-r,y);ctx.closePath()}
      else {ctx.moveTo(x,y-r);ctx.lineTo(x+r,y+r);ctx.lineTo(x-r,y+r);ctx.closePath()}
      ctx.fill();ctx.stroke()
    }
  })}}
  return {attached(p){attached=p;p.requestUpdate()},detached(){attached=undefined},
    paneViews:()=>[{renderer:()=>renderer}],
    hitTest(x,y){const hits=coords().map(v=>({...v,d:Math.hypot(v.x-x,v.y-y)})).filter(v=>v.d<=8).sort((a,b)=>a.d-b.d)
      return hits[0]?{externalId:hits[0].p.id,zOrder:'normal',cursorStyle:'pointer'}:null},
  }
}
