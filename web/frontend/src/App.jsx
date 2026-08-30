import { useCallback, useEffect, useMemo, useState } from 'react'
import MarketChart from './MarketChart'
import { loadBars, searchInstruments } from './api'

const INTERVALS = [
  ['1d', '日'],
  ['5d', '5日'],
  ['1w', '周'],
  ['1mo', '月'],
]
const RANGES = ['1Y', '3Y', '5Y', 'ALL']
const LIMITS = {
  '1d': { '1Y': 252, '3Y': 756, '5Y': 1260, ALL: 0 },
  '5d': { '1Y': 51, '3Y': 152, '5Y': 252, ALL: 0 },
  '1w': { '1Y': 52, '3Y': 156, '5Y': 260, ALL: 0 },
  '1mo': { '1Y': 12, '3Y': 36, '5Y': 60, ALL: 0 },
}

const number = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const compact = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 2 })
const pct = (value) => `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`

function Metric({ label, value, tone = '' }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong className={tone}>{value}</strong>
    </div>
  )
}

function App() {
  const [symbol, setSymbol] = useState('AAPL')
  const [interval, setInterval] = useState('1d')
  const [range, setRange] = useState('5Y')
  const [price, setPrice] = useState('adjusted')
  const [market, setMarket] = useState('US')
  const [query, setQuery] = useState('')
  const [instruments, setInstruments] = useState([])
  const [payload, setPayload] = useState(null)
  const [hover, setHover] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    const timer = setTimeout(() => {
      searchInstruments(query, market, controller.signal)
        .then((result) => setInstruments(result.instruments))
        .catch((reason) => reason.name !== 'AbortError' && setError(reason.message))
    }, query ? 140 : 0)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [query, market])

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setLoading(true)
    setError('')
    loadBars({ symbol, interval, price, limit: LIMITS[interval][range] }, controller.signal)
      .then((result) => {
        if (!active) return
        setPayload(result)
        setHover(null)
      })
      .catch((reason) => reason.name !== 'AbortError' && setError(reason.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
      controller.abort()
    }
  }, [symbol, interval, range, price])

  const onHover = useCallback((bar) => setHover(bar), [])
  const quote = payload?.quote
  const shown = hover || quote
  const positive = (quote?.change_pct || 0) >= 0
  const chartBars = payload?.bars || []
  const title = payload?.instrument?.name || symbol
  const marketLabel = payload?.instrument?.market === 'CN' ? 'A股' : payload?.instrument?.asset_type || '美股'

  const status = useMemo(() => {
    if (!payload) return ''
    return `${payload.meta.rows.toLocaleString()} 根 · ${payload.meta.first_date} — ${payload.meta.last_date} · ${payload.meta.load_ms} ms`
  }, [payload])

  const chooseInstrument = (item) => {
    setSymbol(item.symbol)
    setMarket(item.market)
    if (item.market === 'CN') setPrice('adjusted')
    setQuery('')
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark">K</div>
          <div><b>KOLMO</b><span>MARKET TERMINAL</span></div>
        </div>

        <div className="market-switch" role="group" aria-label="市场筛选">
          {[['US', '美股'], ['CN', 'A股'], ['ALL', '全部']].map(([value, label]) => (
            <button key={value} className={market === value ? 'active' : ''} onClick={() => setMarket(value)}>{label}</button>
          ))}
        </div>

        <label className="search-box">
          <span>⌕</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="代码或公司名称" />
        </label>

        <div className="instrument-list">
          <div className="list-label">本地证券库 <span>{instruments.length}</span></div>
          {instruments.map((item) => (
            <button key={`${item.market}-${item.symbol}`} className={`instrument ${symbol === item.symbol ? 'selected' : ''}`} onClick={() => chooseInstrument(item)}>
              <span className="ticker">{item.symbol}</span>
              <span className="company">{item.name}</span>
              <span className={`market-tag ${item.market.toLowerCase()}`}>{item.market === 'CN' ? 'CN' : item.asset_type}</span>
            </button>
          ))}
        </div>
        <div className="sidebar-foot"><i /> 本地数据 · 只读模式</div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div>
            <div className="eyebrow">{marketLabel} / {payload?.instrument?.sector || 'MARKET DATA'}</div>
            <h1>{symbol} <span>{title}</span></h1>
          </div>
          <div className="latest-price">
            <strong>{quote ? number.format(quote.close) : '—'}</strong>
            <span className={positive ? 'up' : 'down'}>{quote ? `${quote.change >= 0 ? '+' : ''}${number.format(quote.change)}  ${pct(quote.change_pct)}` : '—'}</span>
          </div>
        </header>

        <section className="toolbar">
          <div className="segmented">
            {INTERVALS.map(([value, label]) => <button key={value} className={interval === value ? 'active' : ''} onClick={() => setInterval(value)}>{label}</button>)}
          </div>
          <div className="segmented subtle">
            {RANGES.map((value) => <button key={value} className={range === value ? 'active' : ''} onClick={() => setRange(value)}>{value}</button>)}
          </div>
          <div className="toolbar-spacer" />
          <label className="price-mode">价格口径<select value={price} disabled={payload?.instrument?.market === 'CN'} onChange={(event) => setPrice(event.target.value)}><option value="adjusted">复权</option><option value="raw">原始</option></select></label>
        </section>

        <section className="metrics-grid">
          <Metric label={hover ? `${hover.date} 开盘` : '今日开盘'} value={shown ? number.format(shown.open) : '—'} />
          <Metric label="最高" value={shown ? number.format(shown.high) : '—'} tone="up" />
          <Metric label="最低" value={shown ? number.format(shown.low) : '—'} tone="down" />
          <Metric label="收盘" value={shown ? number.format(shown.close) : '—'} />
          <Metric label="成交量" value={shown?.volume ? compact.format(shown.volume) : quote?.volume ? compact.format(quote.volume) : '—'} />
          <Metric label="52周回撤" value={quote ? pct(quote.drawdown_52w) : '—'} tone={quote?.drawdown_52w < -0.2 ? 'down' : ''} />
        </section>

        <section className="chart-panel">
          <div className="chart-heading"><div><b>价格走势</b><span>{INTERVALS.find(([value]) => value === interval)?.[1]}K · {price === 'adjusted' ? '复权价格' : '原始价格'}</span></div><span className="status">{status}</span></div>
          <div className="chart-stage">
            {chartBars.length > 0 && <MarketChart bars={chartBars} onHover={onHover} />}
            {loading && <div className="chart-message"><div className="loader" />正在读取本地行情…</div>}
            {error && !loading && <div className="chart-message error"><b>无法加载</b><span>{error}</span></div>}
          </div>
        </section>

        <footer><span>数据源：{payload?.instrument?.market === 'CN' ? 'BaoStock' : 'Tiingo EOD'}</span><span>滚轮缩放 · 拖动平移 · 十字线查看 OHLC</span></footer>
      </main>
    </div>
  )
}

export default App
