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

export function loadBars({ symbol, interval, price, limit }, signal) {
  const params = new URLSearchParams({
    symbol,
    interval,
    price,
    limit: String(limit),
  })
  return request(`/api/v1/bars?${params}`, signal)
}
