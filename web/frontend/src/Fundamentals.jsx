import { useEffect, useMemo, useState } from 'react'
import { loadFacts, loadFactSeries, loadFilings, loadFundamentalSummary } from './api'
import FactSeriesChart from './FactSeriesChart'

const PAGE_SIZE = 50
const integer = new Intl.NumberFormat('zh-CN')

function SummaryCard({ label, value, detail }) {
  return <div className="fund-card"><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>
}

function Pager({ offset, total, onChange }) {
  const page = Math.floor(offset / PAGE_SIZE) + 1
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  return <div className="pager"><span>第 {page} / {pages} 页 · {integer.format(total)} 条</span><button disabled={!offset} onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))}>上一页</button><button disabled={offset + PAGE_SIZE >= total} onClick={() => onChange(offset + PAGE_SIZE)}>下一页</button></div>
}

export default function Fundamentals({ symbol, title }) {
  const [summary, setSummary] = useState(null)
  const [facts, setFacts] = useState(null)
  const [filings, setFilings] = useState(null)
  const [series, setSeries] = useState(null)
  const [section, setSection] = useState('facts')
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [taxonomy, setTaxonomy] = useState('')
  const [unit, setUnit] = useState('')
  const [form, setForm] = useState('')
  const [offset, setOffset] = useState(0)
  const [filingOffset, setFilingOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 180)
    return () => clearTimeout(timer)
  }, [query])

  useEffect(() => {
    setSummary(null); setSeries(null); setTaxonomy(''); setUnit(''); setForm(''); setOffset(0); setFilingOffset(0)
    const controller = new AbortController()
    setLoading(true); setError('')
    loadFundamentalSummary(symbol, controller.signal)
      .then((result) => {
        setSummary(result)
        const first = result.popular_concepts?.[0]
        if (first) return loadFactSeries({ symbol, ...first }, controller.signal).then(setSeries)
      })
      .catch((reason) => reason.name !== 'AbortError' && setError(reason.message))
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [symbol])

  useEffect(() => {
    const controller = new AbortController()
    loadFacts({ symbol, q: debouncedQuery, taxonomy, unit, form, limit: PAGE_SIZE, offset }, controller.signal)
      .then(setFacts).catch((reason) => reason.name !== 'AbortError' && setError(reason.message))
    return () => controller.abort()
  }, [symbol, debouncedQuery, taxonomy, unit, form, offset])

  useEffect(() => {
    const controller = new AbortController()
    loadFilings({ symbol, form, limit: PAGE_SIZE, offset: filingOffset }, controller.signal)
      .then(setFilings).catch((reason) => reason.name !== 'AbortError' && setError(reason.message))
    return () => controller.abort()
  }, [symbol, form, filingOffset])

  const selectSeries = (row) => {
    const controller = new AbortController()
    loadFactSeries({ symbol, taxonomy: row.taxonomy, tag: row.tag, unit: row.unit }, controller.signal)
      .then(setSeries).catch((reason) => setError(reason.message))
  }
  const setFilter = (setter) => (event) => { setter(event.target.value); setOffset(0); setFilingOffset(0) }
  const coverage = useMemo(() => summary ? `${summary.period_start} — ${summary.period_end}` : '—', [summary])

  return (
    <div className="fundamentals-workspace">
      <header className="fund-header">
        <div><div className="eyebrow">SEC EDGAR / POINT-IN-TIME FUNDAMENTALS</div><h1>{symbol} <span>{title}</span></h1></div>
        <div className="cik-badge"><span>CIK</span><b>{summary?.cik || '—'}</b></div>
      </header>

      {error && <div className="fund-error">{error}</div>}
      <section className="fund-summary">
        <SummaryCard label="结构化事实" value={summary ? integer.format(summary.facts_rows) : '—'} detail={`${summary?.concepts || '—'} 个 tag / unit 组合`} />
        <SummaryCard label="申报文件" value={summary ? integer.format(summary.filings_rows) : '—'} detail="10-K · 10-Q · 8-K · 20-F · 40-F · 6-K" />
        <SummaryCard label="报告期覆盖" value={coverage} detail="end_date，不代表当时可用" />
        <SummaryCard label="最近可用日" value={summary?.available_end || '—'} detail="策略 as-of 应使用 available_date" />
      </section>

      <section className="pit-note"><b>三个时间不能混用</b><span><i>报告期</i> end_date</span><span><i>公开时间</i> accepted_at / available_date</span><span><i>抓取时间</i> retrieved_at</span></section>

      <section className="series-panel">
        <FactSeriesChart series={series} />
        <div className="popular-concepts"><span>高频原始概念（仅按记录数排列）</span><div>{summary?.popular_concepts.map((item) => <button key={`${item.taxonomy}-${item.tag}-${item.unit}`} onClick={() => selectSeries(item)}><b>{item.tag}</b><small>{item.taxonomy} · {item.unit} · {item.count}</small></button>)}</div></div>
      </section>

      <section className="data-panel">
        <div className="data-tabs"><button className={section === 'facts' ? 'active' : ''} onClick={() => setSection('facts')}>事实浏览器</button><button className={section === 'filings' ? 'active' : ''} onClick={() => setSection('filings')}>申报历史</button><span>所有 amendment 与 restatement 均独立保留</span></div>
        <div className="fact-filters">
          {section === 'facts' && <label className="fact-search"><span>⌕</span><input value={query} onChange={setFilter(setQuery)} placeholder="搜索 tag、标签、描述或 accession" /></label>}
          {section === 'facts' && <select value={taxonomy} onChange={setFilter(setTaxonomy)}><option value="">全部 taxonomy</option>{summary?.taxonomies.map((item) => <option key={item.value} value={item.value}>{item.value} ({item.count})</option>)}</select>}
          {section === 'facts' && <select value={unit} onChange={setFilter(setUnit)}><option value="">全部 unit</option>{summary?.units.map((item) => <option key={item.value} value={item.value}>{item.value} ({item.count})</option>)}</select>}
          <select value={form} onChange={setFilter(setForm)}><option value="">全部 form</option>{summary?.filing_forms.map((item) => <option key={item.value} value={item.value}>{item.value} ({item.count})</option>)}</select>
        </div>

        <div className="table-scroll">
          {section === 'facts' ? <table className="data-table facts-table"><thead><tr><th>概念</th><th>数值 / 单位</th><th>报告期</th><th>可用日</th><th>申报</th><th>FY / FP</th></tr></thead><tbody>{facts?.facts.map((row) => <tr key={row.row_id} onClick={() => selectSeries(row)}><td><b>{row.tag}</b><span>{row.taxonomy} · {row.label}</span></td><td><code>{row.value}</code><span>{row.unit}</span></td><td>{row.start_date ? <span>{row.start_date}<br />→ {row.end_date}</span> : row.end_date}</td><td><b>{row.available_date}</b><span>{row.accepted_at || 'accepted_at 缺失'}</span></td><td><span className={row.form.endsWith('/A') ? 'form amended' : 'form'}>{row.form}</span><small>{row.accession_number}</small></td><td>{row.fiscal_year || '—'} / {row.fiscal_period || '—'}<span>{row.frame || '无 frame'}</span></td></tr>)}</tbody></table> : <table className="data-table filings-table"><thead><tr><th>Form</th><th>Filing date</th><th>Report date</th><th>SEC accepted_at</th><th>Accession</th><th>XBRL</th></tr></thead><tbody>{filings?.filings.map((row) => <tr key={row.accession_number}><td><span className={row.form.endsWith('/A') ? 'form amended' : 'form'}>{row.form}</span></td><td>{row.filing_date}</td><td>{row.report_date || '—'}</td><td>{row.accepted_at || '—'}</td><td><code>{row.accession_number}</code><span>{row.primary_document || '—'}</span></td><td>{row.is_inline_xbrl === '1' ? 'Inline' : row.is_xbrl === '1' ? 'XBRL' : '—'}</td></tr>)}</tbody></table>}
          {loading && <div className="table-loading">正在读取 SEC canonical 数据…</div>}
        </div>
        {section === 'facts' ? <Pager offset={offset} total={facts?.total || 0} onChange={setOffset} /> : <Pager offset={filingOffset} total={filings?.total || 0} onChange={setFilingOffset} />}
      </section>
      <footer><span>数据源：SEC EDGAR Company Submissions + Company Facts</span><span>展示原始 XBRL taxonomy / tag，不生成投资判断</span></footer>
    </div>
  )
}
