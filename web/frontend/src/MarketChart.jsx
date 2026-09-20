import { useEffect, useRef } from 'react'
import { ColorType, CrosshairMode, createChart } from 'lightweight-charts'

const shanghaiTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
})

const chartTime = (bar) => bar.time ?? bar.date

function movingAverage(bars, window) {
  let sum = 0
  return bars.flatMap((bar, index) => {
    sum += bar.close
    if (index >= window) sum -= bars[index - window].close
    return index + 1 < window ? [] : [{ time: chartTime(bar), value: sum / window }]
  })
}

export default function MarketChart({ bars, onHover, intraday = false }) {
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
      localization: {
        timeFormatter: (time) => typeof time === 'number' ? shanghaiTime.format(new Date(time * 1000)) : String(time),
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
      timeScale: {
        borderColor: '#263246', timeVisible: intraday, secondsVisible: false,
        rightOffset: 4, barSpacing: intraday ? 6 : 7,
        tickMarkFormatter: (time) => typeof time === 'number' ? shanghaiTime.format(new Date(time * 1000)) : String(time),
      },
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
    const averages = [
      [5, '#e8c15d'], [10, '#6da5df'], [20, '#b77bd1'],
    ].map(([window, color]) => {
      const series = chart.addLineSeries({
        color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
      })
      series.setData(movingAverage(bars, window))
      return series
    })
    void averages
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } })

    candles.setData(bars.map((bar) => ({ time: chartTime(bar), open: bar.open, high: bar.high, low: bar.low, close: bar.close })))
    volume.setData(bars.map((bar) => ({
      time: chartTime(bar),
      value: bar.volume,
      color: bar.close >= bar.open ? 'rgba(239,91,108,.48)' : 'rgba(34,183,139,.48)',
    })))
    chart.timeScale().fitContent()

    const barsByDate = new Map(bars.map((bar) => [String(chartTime(bar)), bar]))
    chart.subscribeCrosshairMove((param) => {
      const point = param.seriesData.get(candles)
      if (point && param.time) onHover(barsByDate.get(String(param.time)) || null)
      else onHover(null)
    })
    return () => chart.remove()
  }, [bars, onHover, intraday])

  return <div className="chart-wrap"><div className="ma-legend"><span>MA5</span><span>MA10</span><span>MA20</span></div><div className="chart-canvas" ref={containerRef} aria-label="K 线与成交量图" /></div>
}
