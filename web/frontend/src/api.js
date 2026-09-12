const API_BASE = import.meta.env.VITE_API_BASE || ''

async function request(path, signal) {
  const response = await fetch(`${API_BASE}${path}`, { signal })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`)
  return payload
}

export function searchInstruments(query = '', market = 'ALL', signal) {
  const params = new URLSearchParams({ q: query, market, limit: '40' })
  return request(`/api/v1/instruments?${params}`, signal)
}

export function loadBars({ symbol, interval, price, limit, start = '', end = '' }, signal) {
  const params = new URLSearchParams({
    symbol,
    interval,
    price,
    limit: String(limit),
  })
  if (start) params.set('start', start)
  if (end) params.set('end', end)
  return request(`/api/v1/bars?${params}`, signal)
}

export function loadFundamentalSummary(symbol, signal) {
  return request(`/api/v1/fundamentals/summary?${new URLSearchParams({ symbol })}`, signal)
}

export function loadFacts({ symbol, q = '', taxonomy = '', unit = '', form = '', limit = 50, offset = 0 }, signal) {
  const params = new URLSearchParams({ symbol, q, taxonomy, unit, form, limit: String(limit), offset: String(offset) })
  return request(`/api/v1/fundamentals/facts?${params}`, signal)
}

export function loadFilings({ symbol, form = '', limit = 50, offset = 0 }, signal) {
  const params = new URLSearchParams({ symbol, form, limit: String(limit), offset: String(offset) })
  return request(`/api/v1/fundamentals/filings?${params}`, signal)
}

export function loadFactSeries({ symbol, taxonomy, tag, unit }, signal) {
  const params = new URLSearchParams({ symbol, taxonomy, tag, unit })
  return request(`/api/v1/fundamentals/series?${params}`, signal)
}
