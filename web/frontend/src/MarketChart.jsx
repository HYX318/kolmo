import { useEffect, useRef } from 'react'
import { ColorType, CrosshairMode, createChart } from 'lightweight-charts'

export default function MarketChart({ bars, onHover }) {
  const containerRef = useRef(null)

  useEffect(() => {
    if (!containerRef.current) return undefined
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: '#0d1420' },
        textColor: '#7f8da3',
        fontFamily: 'IBM Plex Mono, SFMono-Regular, Consolas, monospace',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#192333' },
        horzLines: { color: '#192333' },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: '#6f8098', labelBackgroundColor: '#263449' },
        horzLine: { color: '#6f8098', labelBackgroundColor: '#263449' },
      },
      rightPriceScale: { borderColor: '#263246', scaleMargins: { top: 0.08, bottom: 0.28 } },
      timeScale: { borderColor: '#263246', timeVisible: false, rightOffset: 4, barSpacing: 7 },
      handleScroll: true,
      handleScale: true,
    })

    const candles = chart.addCandlestickSeries({
      upColor: '#ef5b6c',
      downColor: '#22b78b',
      borderUpColor: '#ef5b6c',
      borderDownColor: '#22b78b',
      wickUpColor: '#ef5b6c',
      wickDownColor: '#22b78b',
      priceLineColor: '#d7a84b',
    })
    const volume = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    })
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } })

    candles.setData(bars.map(({ date: time, open, high, low, close }) => ({ time, open, high, low, close })))
    volume.setData(bars.map((bar) => ({
      time: bar.date,
      value: bar.volume,
      color: bar.close >= bar.open ? 'rgba(239,91,108,.48)' : 'rgba(34,183,139,.48)',
    })))
    chart.timeScale().fitContent()

    const barsByDate = new Map(bars.map((bar) => [bar.date, bar]))
    chart.subscribeCrosshairMove((param) => {
      const point = param.seriesData.get(candles)
      if (point && param.time) onHover(barsByDate.get(String(param.time)) || null)
      else onHover(null)
    })
    return () => chart.remove()
  }, [bars, onHover])

  return <div className="chart-canvas" ref={containerRef} aria-label="K 线与成交量图" />
}
