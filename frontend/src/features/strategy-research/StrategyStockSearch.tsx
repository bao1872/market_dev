import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/api/client'
import styles from './strategyResearchPage.module.scss'

type Result = { symbol: string; name: string; in_snapshot: boolean }
type SearchResult = { items: Result[]; scope: 'STRATEGY_RESEARCH'; available_count: number }

export default function StrategyStockSearch({
  symbol, onChoose,
}: { symbol: string; onChoose: (symbol: string)=>void }) {
  const [value,setValue] = useState(symbol)
  const [delayed,setDelayed] = useState('')
  const [open,setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(()=>setValue(symbol),[symbol])
  useEffect(()=>{
    const timer = setTimeout(()=>setDelayed(value.trim()),180)
    return ()=>clearTimeout(timer)
  },[value])
  useEffect(()=>{
    const onOutside=(event:MouseEvent)=>{
      if(ref.current && !ref.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown',onOutside)
    return ()=>document.removeEventListener('mousedown',onOutside)
  },[])
  const q = useQuery({
    queryKey: ['strategy','stock-search',delayed],
    queryFn:async()=>{
      const {data}=await apiClient.get<SearchResult>(
        '/v1/strategy-research/stocks/search', {params:{keyword:delayed}},
      )
      return data
    },
    enabled:open && delayed.length>0,
    staleTime:60_000,
  })
  return <div className={styles.strategySearch} ref={ref}>
    <label htmlFor="strategy-stock-query">股票搜索（名称 / 代码 / 拼音首字母）</label>
    <input id="strategy-stock-query" autoComplete="off" role="combobox"
      aria-expanded={open} aria-controls="strategy-stock-results"
      placeholder="如 浦发 / PFYH / 600000 / SH:600000"
      value={value} onFocus={()=>setOpen(true)}
      onChange={e=>{setValue(e.target.value);setOpen(true)}}
      onKeyDown={e=>{if(e.key==='Escape')setOpen(false)}} />
    {open && delayed && <div className={styles.strategySearchResults}
      id="strategy-stock-results" role="listbox">
      {q.isLoading && <div>查询中…</div>}
      {q.isError && <div>股票搜索暂不可用</div>}
      {q.data?.items.map(item=>
        <button key={item.symbol} role="option" aria-selected={item.symbol===symbol}
          type="button" onClick={()=>{
            setValue(item.symbol);setOpen(false);onChoose(item.symbol)
          }}>
          <strong>{item.name}</strong> <span>{item.symbol}</span>
          <small>{item.in_snapshot?'有历史研究数据':'暂未发布历史研究'}</small>
        </button>
      )}
      {q.data && q.data.items.length===0 && <div>无匹配股票</div>}
    </div>}
  </div>
}
