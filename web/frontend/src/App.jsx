import { useCallback, useEffect, useMemo, useState } from 'react'
import MarketChart from './MarketChart'
import Fundamentals from './Fundamentals'
import { loadBars, searchInstruments } from './api'

const MINUTE_INTERVALS = [
  ['1m', '1分'],
  ['5m', '5分'],
  ['15m', '15分'],
  ['30m', '30分'],
  ['60m', '60分'],
]
const DAILY_INTERVALS = [
  ['1d', '日'],
  ['5d', '5日'],
  ['1w', '周'],
  ['1mo', '月'],
]
const DAILY_RANGES = ['1Y', '3Y', '5Y', 'ALL']
const MINUTE_RANGES = ['1D', '5D', '1M', '3M', '1Y']
const LIMITS = {
  '1m': { '1D': 241, '5D': 1205, '1M': 5200, '3M': 18000, '1Y': 60000 },
  '5m': { '1D': 49, '5D': 245, '1M': 1100, '3M': 3500, '1Y': 12000 },
  '15m': { '1D': 17, '5D': 85, '1M': 380, '3M': 1200, '1Y': 4200 },
  '30m': { '1D': 9, '5D': 45, '1M': 200, '3M': 650, '1Y': 2200 },
  '60m': { '1D': 5, '5D': 25, '1M': 110, '3M': 350, '1Y': 1250 },
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
  const [view, setView] = useState('market')
  const [symbol, setSymbol] = useState('AAPL')
  const [selectedName, setSelectedName] = useState('Apple Inc.')
  const [interval, setInterval] = useState('1d')
  const [range, setRange] = useState('5Y')
  const [price, setPrice] = useState('adjusted')
  const [minuteYear, setMinuteYear] = useState('latest')
  const [market, setMarket] = useState('US')
  const [query, setQuery] = useState('')
  const [instruments, setInstruments] = useState([])
  const [payload, setPayload] = useState(null)
  const [hover, setHover] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const isMinute = MINUTE_INTERVALS.some(([value]) => value === interval)
  const selectedMarket = payload?.instrument?.market || market
  const intervals = selectedMarket === 'CN' ? [...MINUTE_INTERVALS, ...DAILY_INTERVALS] : DAILY_INTERVALS
  const ranges = isMinute ? MINUTE_RANGES : DAILY_RANGES

  useEffect(() => {
    const controller = new AbortController()
    const timer = setTimeout(() => {
      searchInstruments(query, view === 'fundamentals' ? 'US' : market, controller.signal)
        .then((result) => setInstruments(view === 'fundamentals' ? result.instruments.filter((item) => item.has_fundamentals) : result.instruments))
        .catch((reason) => reason.name !== 'AbortError' && setError(reason.message))
    }, query ? 140 : 0)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [query, market, view])

  useEffect(() => {
    if (view !== 'market') return undefined
    const controller = new AbortController()
    let active = true
    setLoading(true)
    setError('')
    const dated = isMinute && minuteYear !== 'latest'
    loadBars({
      symbol, interval, price: isMinute ? 'raw' : price,
      limit: LIMITS[interval]?.[range] || 0,
      start: dated ? `${minuteYear}-01-01` : '',
      end: dated ? `${minuteYear}-12-31` : '',
    }, controller.signal)
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
  }, [symbol, interval, range, price, view, isMinute, minuteYear])

  const onHover = useCallback((bar) => setHover(bar), [])
  const quote = payload?.quote
  const shown = hover || quote
  const positive = (quote?.change_pct || 0) >= 0
  const chartBars = payload?.bars || []
  const title = payload?.instrument?.name || symbol
  const marketLabel = payload?.instrument?.market === 'CN' ? 'A股' : payload?.instrument?.asset_type || '美股'
  const intervalLabel = [...MINUTE_INTERVALS, ...DAILY_INTERVALS].find(([value]) => value === interval)?.[1]

  const status = useMemo(() => {
    if (!payload) return ''
    return `${payload.meta.rows.toLocaleString()} 根 · ${payload.meta.first_date} — ${payload.meta.last_date} · ${payload.meta.load_ms} ms`
  }, [payload])

  const chooseInstrument = (item) => {
    setSymbol(item.symbol)
    setSelectedName(item.name)
    setMarket(item.market)
    setMinuteYear('latest')
    if (item.market === 'CN') setPrice(isMinute ? 'raw' : 'adjusted')
    if (item.market === 'US' && isMinute) {
      setInterval('1d')
      setRange('5Y')
      setPrice('adjusted')
    }
    setQuery('')
  }

  const chooseInterval = (value) => {
    const nextMinute = MINUTE_INTERVALS.some(([candidate]) => candidate === value)
    setInterval(value)
    if (nextMinute !== isMinute) setRange(nextMinute ? '1M' : '5Y')
    if (nextMinute) {
      setPrice('raw')
      setMinuteYear('latest')
    }
  }

  const chooseView = (next) => {
    setView(next)
    setError('')
    if (next === 'fundamentals' && (market === 'CN' || !instruments.find((item) => item.symbol === symbol && item.has_fundamentals))) {
      setSymbol('AAPL')
      setSelectedName('Apple Inc.')
      setMarket('US')
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark">K</div>
          <div><b>KOLMO</b><span>MARKET TERMINAL</span></div>
        </div>

        <nav className="workspace-nav" aria-label="工作区">
          <button className={view === 'market' ? 'active' : ''} onClick={() => chooseView('market')}><span>⌁</span>行情图表</button>
          <button className={view === 'fundamentals' ? 'active' : ''} onClick={() => chooseView('fundamentals')}><span>▤</span>SEC 基本面</button>
        </nav>

        {view === 'market' && <div className="market-switch" role="group" aria-label="市场筛选">
          {[['US', '美股'], ['CN', 'A股'], ['ALL', '全部']].map(([value, label]) => (
            <button key={value} className={market === value ? 'active' : ''} onClick={() => setMarket(value)}>{label}</button>
          ))}
        </div>}

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
              <span className={`market-tag ${item.market.toLowerCase()}`}>{view === 'fundamentals' ? 'SEC' : item.market === 'CN' ? 'CN' : item.asset_type}</span>
            </button>
          ))}
        </div>
        <div className="sidebar-foot"><i /> 本地数据 · 只读模式</div>
      </aside>

      {view === 'fundamentals' ? <main className="workspace"><Fundamentals symbol={symbol} title={selectedName} /></main> : <main className="workspace">
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
            {intervals.map(([value, label]) => <button key={value} className={interval === value ? 'active' : ''} onClick={() => chooseInterval(value)}>{label}</button>)}
          </div>
          <div className="segmented subtle">
            {ranges.map((value) => <button key={value} className={range === value ? 'active' : ''} onClick={() => setRange(value)}>{value}</button>)}
          </div>
          <div className="toolbar-spacer" />
          {isMinute && <label className="price-mode">年份<select value={minuteYear} onChange={(event) => { setMinuteYear(event.target.value); if (event.target.value !== 'latest') setRange('1Y') }}><option value="latest">最新</option>{[...(payload?.meta?.available_years || [])].reverse().map((year) => <option value={year} key={year}>{year}</option>)}</select></label>}
          <label className="price-mode">价格口径<select value={isMinute ? 'raw' : price} disabled={payload?.instrument?.market === 'CN'} onChange={(event) => setPrice(event.target.value)}><option value="adjusted">复权</option><option value="raw">原始</option></select></label>
        </section>

        <section className="metrics-grid">
          <Metric label={hover ? `${hover.date} 开盘` : isMinute ? '当前开盘' : '今日开盘'} value={shown ? number.format(shown.open) : '—'} />
          <Metric label="最高" value={shown ? number.format(shown.high) : '—'} tone="up" />
          <Metric label="最低" value={shown ? number.format(shown.low) : '—'} tone="down" />
          <Metric label="收盘" value={shown ? number.format(shown.close) : '—'} />
          <Metric label="成交量" value={shown?.volume != null ? compact.format(shown.volume) : '—'} />
          <Metric label={isMinute ? '成交额' : '52周回撤'} value={isMinute ? shown?.amount != null ? compact.format(shown.amount) : '—' : quote ? pct(quote.drawdown_52w) : '—'} tone={!isMinute && quote?.drawdown_52w < -0.2 ? 'down' : ''} />
        </section>

        <section className="chart-panel">
          <div className="chart-heading"><div><b>价格走势</b><span>{intervalLabel}K · {isMinute ? '未复权' : price === 'adjusted' ? '复权价格' : '原始价格'}</span>{payload?.meta?.zero_volume_rows > 0 && <em className="quality-flag">零量 {payload.meta.zero_volume_rows.toLocaleString()}</em>}</div><span className="status">{status}</span></div>
          <div className="chart-stage">
            {chartBars.length > 0 && <MarketChart bars={chartBars} onHover={onHover} intraday={isMinute} />}
            {loading && <div className="chart-message"><div className="loader" />正在读取本地行情…</div>}
            {error && !loading && <div className="chart-message error"><b>无法加载</b><span>{error}</span></div>}
          </div>
        </section>

        <footer><span>数据源：{payload?.meta?.source === 'vendor_parquet' ? 'Vendor Parquet / raw' : payload?.instrument?.market === 'CN' ? 'BaoStock' : 'Tiingo EOD'}</span><span>滚轮缩放 · 拖动平移 · 十字线查看 OHLC · MA5/10/20</span></footer>
      </main>}
    </div>
  )
}

export default App
