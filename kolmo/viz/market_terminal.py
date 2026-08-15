#!/usr/bin/env python3
"""Serve a local, interactive A-share market terminal from Kolmo data files.

The browser never receives the whole historical market at once: it requests one
symbol's full raw cache or one exchange/date cross-section when the user asks.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kolmo.paths import data_path


SYMBOL_RE = re.compile(r"^\d{6}\.(SZ|SH)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a local K-line and daily-market terminal.")
    parser.add_argument("--host", default="127.0.0.1", help="Default binds to localhost only.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-root", default="", help="Defaults to KOLMO_DATA_ROOT.")
    return parser.parse_args()


def number(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def normalize_date(value: str) -> str:
    return value.replace("-", "")[:8]


def exchange_from_symbol(symbol: str) -> str:
    return "sh" if symbol.endswith(".SH") else "sz"


class MarketData:
    def __init__(self, root: Path):
        self.root = root
        self._symbols: dict[str, list[str]] = {}

    def profile_path(self, exchange: str, trade_date: str) -> Path:
        return self.root / "profile" / "daily" / exchange / trade_date[:4] / trade_date[4:6] / f"{trade_date}.csv"

    def dates(self, exchange: str) -> list[str]:
        root = self.root / "profile" / "daily" / exchange
        return [path.stem for path in sorted(root.glob("*/*/*.csv")) if re.fullmatch(r"\d{8}", path.stem)]

    def latest_date(self, exchange: str) -> str:
        dates = self.dates(exchange)
        if not dates:
            raise FileNotFoundError(f"no profile files for {exchange}")
        return dates[-1]

    def symbols(self, query: str, exchange: str) -> list[str]:
        """Search the latest local stock universe without touching network data."""
        exchanges = [exchange] if exchange in {"sz", "sh"} else ["sz", "sh"]
        candidates: list[str] = []
        for item in exchanges:
            if item not in self._symbols:
                path = self.profile_path(item, self.latest_date(item))
                with path.open("r", encoding="utf-8", newline="") as file:
                    self._symbols[item] = sorted({row.get("symbol", "").upper() for row in csv.DictReader(file) if row.get("symbol")})
            candidates.extend(self._symbols[item])
        text = query.upper().strip().replace(" ", "")
        if not text:
            return candidates[:30]
        prefix = [symbol for symbol in candidates if symbol.startswith(text)]
        contains = [symbol for symbol in candidates if text in symbol and symbol not in prefix]
        return (prefix + contains)[:30]

    def market(self, exchange: str, requested_date: str) -> dict[str, object]:
        dates = self.dates(exchange)
        if not dates:
            raise FileNotFoundError(f"no profile files for {exchange}")
        date = normalize_date(requested_date) if requested_date else dates[-1]
        chosen = next((candidate for candidate in reversed(dates) if candidate <= date), dates[0])
        rows: list[dict[str, object]] = []
        total_amount = 0.0
        up = down = flat = traded = 0
        with self.profile_path(exchange, chosen).open("r", encoding="utf-8", newline="") as file:
            for row in csv.DictReader(file):
                amount = number(row.get("amount", "")) or 0.0
                change = number(row.get("pct_change", "")) or 0.0
                status = row.get("trade_status", "") == "1"
                total_amount += amount
                traded += int(status)
                up += int(change > 0)
                down += int(change < 0)
                flat += int(change == 0)
                rows.append({
                    "symbol": row.get("symbol", ""), "close": number(row.get("close", "")),
                    "pct_change": change, "amount": amount, "turnover_rate": number(row.get("turnover_rate", "")),
                    "is_st": row.get("is_st", "") == "1", "trade_status": status,
                })
        rows.sort(key=lambda row: abs(float(row["pct_change"])), reverse=True)
        return {"date": chosen, "rows": rows, "summary": {"total": len(rows), "traded": traded, "up": up, "down": down, "flat": flat, "amount": total_amount}}

    def symbol(self, requested_symbol: str) -> dict[str, object]:
        symbol = requested_symbol.upper()
        if not SYMBOL_RE.fullmatch(symbol):
            raise ValueError("symbol format must be 000858.SZ or 600519.SH")
        path = self.root / "raw" / "ashare" / "baostock" / exchange_from_symbol(symbol) / "daily" / "qfq" / f"{symbol}.csv.gz"
        if not path.is_file():
            raise FileNotFoundError(f"raw cache not found: {path}")
        bars: list[dict[str, object]] = []
        with gzip.open(path, "rt", encoding="utf-8", newline="") as file:
            for row in csv.DictReader(file):
                if row.get("tradestatus") != "1":
                    continue
                values = [number(row.get(field, "")) for field in ("open", "high", "low", "close")]
                if any(value is None for value in values):
                    continue
                bars.append({
                    "date": row["date"], "open": values[0], "high": values[1], "low": values[2], "close": values[3],
                    "volume": number(row.get("volume", "")) or 0.0, "amount": number(row.get("amount", "")) or 0.0,
                    "pe_ttm": number(row.get("peTTM", "")), "pb_mrq": number(row.get("pbMRQ", "")),
                })
        return {"symbol": symbol, "bars": bars}


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kolmo 行情终端</title><style>
:root{color-scheme:dark;--bg:#0b111b;--panel:#121b29;--line:#28364a;--text:#e5edf8;--muted:#91a1b8;--red:#f05b67;--green:#27c494;--gold:#f2b84b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{height:56px;display:flex;align-items:center;gap:18px;padding:0 20px;border-bottom:1px solid var(--line);background:#0e1622}.brand{font-weight:650;letter-spacing:.06em}.brand b{color:var(--gold)}.tabs{display:flex;height:100%;gap:2px}.tab{background:transparent;border:0;border-bottom:2px solid transparent;color:var(--muted);padding:0 14px;cursor:pointer}.tab.active{color:var(--text);border-color:var(--gold)}main{padding:18px;max-width:1440px;margin:auto}.toolbar{display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-bottom:14px}.field{display:grid;gap:5px;color:var(--muted);font-size:12px}input,select,button{height:34px;border:1px solid var(--line);border-radius:4px;background:#0e1622;color:var(--text);padding:0 10px}button{cursor:pointer;background:#1a2a3d}button:hover{border-color:var(--gold)}.grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:6px}.chart-head{display:flex;justify-content:space-between;align-items:center;padding:11px 14px;border-bottom:1px solid var(--line)}.muted{color:var(--muted)}canvas{display:block;width:100%;height:520px}.quote{padding:12px 14px}.quote dl{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:0}.quote dt{color:var(--muted);font-size:12px}.quote dd{margin:3px 0 0;font-variant-numeric:tabular-nums}.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:14px}.stat{padding:12px}.stat small{display:block;color:var(--muted)}.stat strong{display:block;font-size:20px;margin-top:4px;font-variant-numeric:tabular-nums}.up{color:var(--red)}.down{color:var(--green)}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:right}th{position:sticky;top:0;background:var(--panel);color:var(--muted);font-size:12px;font-weight:500}th:first-child,td:first-child{text-align:left}.table-wrap{max-height:610px;overflow:auto}.hidden{display:none}@media(max-width:800px){.grid{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}canvas{height:400px}main{padding:10px}.brand{font-size:12px}}
</style></head><body><header><div class="brand"><b>KOLMO</b> 行情终端</div><div class="tabs"><button class="tab active" data-view="chart">个股 K 线</button><button class="tab" data-view="market">全市场</button></div></header>
<main><section id="chart"><div class="toolbar"><label class="field">证券代码<input id="symbol" value="000858.SZ" list="symbolOptions" autocomplete="off" spellcheck="false" aria-label="输入股票代码或从候选中选择"><datalist id="symbolOptions"></datalist></label><label class="field">显示区间<select id="range"><option value="22">近 1 个月</option><option value="60">近 3 个月</option><option value="120">近半年</option><option value="240">近 1 年</option><option value="500">近 2 年</option><option value="0" selected>全部</option></select></label><button id="loadSymbol">加载 K 线</button><span id="symbolStatus" class="muted"></span></div><div class="grid"><div class="panel"><div class="chart-head"><span id="chartTitle">K 线</span><span class="muted">滚轮缩放 · 指向查看</span></div><canvas id="kline" aria-label="股票 K 线图"></canvas></div><aside class="panel quote"><dl id="quote"><dt>等待数据</dt><dd>—</dd></dl></aside></div></section>
<section id="market" class="hidden"><div class="toolbar"><label class="field">交易所<select id="exchange"><option value="sz">深交所</option><option value="sh">上交所</option></select></label><label class="field">交易日<input id="tradeDate" type="date"></label><button id="loadMarket">加载全市场</button><span id="marketStatus" class="muted"></span></div><div class="stats panel" id="stats"></div><div class="panel table-wrap"><table><thead><tr><th>代码</th><th>收盘</th><th>涨跌幅</th><th>成交额</th><th>换手率</th><th>状态</th></tr></thead><tbody id="marketRows"></tbody></table></div></section></main>
<script>
const $=id=>document.getElementById(id);let bars=[],start=0,hover=-1;
const fmt=n=>n==null?'—':Number(n).toLocaleString('zh-CN',{maximumFractionDigits:2});
const api=async path=>{const r=await fetch(path);const v=await r.json();if(!r.ok)throw Error(v.error||'请求失败');return v};
function draw(){const c=$('kline'),box=c.getBoundingClientRect(),d=devicePixelRatio||1;c.width=box.width*d;c.height=box.height*d;const x=c.getContext('2d');x.scale(d,d);const W=box.width,H=box.height;x.fillStyle='#121b29';x.fillRect(0,0,W,H);const part=bars.slice(start);if(!part.length)return;const hi=Math.max(...part.map(b=>b.high)),lo=Math.min(...part.map(b=>b.low)),span=Math.max(hi-lo,hi*.01),top=30,bottom=H-105,plot=bottom-top,step=Math.max(2,(W-56)/part.length),cw=Math.max(1,step*.62);const y=v=>bottom-(v-lo)/span*plot;x.strokeStyle='#28364a';x.lineWidth=1;for(let i=0;i<5;i++){const yy=top+plot*i/4;x.beginPath();x.moveTo(48,yy);x.lineTo(W-8,yy);x.stroke();x.fillStyle='#91a1b8';x.font='11px sans-serif';x.fillText((hi-span*i/4).toFixed(2),3,yy+4)}part.forEach((b,i)=>{const px=50+i*step,up=b.close>=b.open,col=up?'#f05b67':'#27c494';x.strokeStyle=col;x.fillStyle=col;x.beginPath();x.moveTo(px+cw/2,y(b.high));x.lineTo(px+cw/2,y(b.low));x.stroke();const yy=y(Math.max(b.open,b.close)),h=Math.max(1,Math.abs(y(b.open)-y(b.close)));x.fillRect(px,yy,cw,h);const vol=b.volume||0,maxVol=Math.max(...part.map(q=>q.volume||0),1);x.globalAlpha=.65;x.fillRect(px,H-18-(vol/maxVol)*70,cw,(vol/maxVol)*70);x.globalAlpha=1});if(hover>=start&&hover<bars.length){const i=hover-start,px=50+i*step+cw/2,b=bars[hover];x.strokeStyle='#f2b84b';x.beginPath();x.moveTo(px,H-95);x.lineTo(px,top);x.stroke();$('quote').innerHTML=`<dt>${b.date}</dt><dd><b>${fmt(b.close)}</b></dd><dt>开 / 高 / 低</dt><dd>${fmt(b.open)} / ${fmt(b.high)} / ${fmt(b.low)}</dd><dt>成交额</dt><dd>${fmt((b.amount||0)/1e8)} 亿</dd><dt>PE TTM</dt><dd>${fmt(b.pe_ttm)}</dd><dt>PB MRQ</dt><dd>${fmt(b.pb_mrq)}</dd>`}else $('quote').innerHTML='<dt>指向 K 线查看</dt><dd>—</dd>'}
async function loadChoices(){try{const data=await api('/api/symbols?q='+encodeURIComponent($('symbol').value));$('symbolOptions').innerHTML=data.symbols.map(symbol=>`<option value="${symbol}"></option>`).join('')}catch(e){/* 输入联想失败不阻断手动加载。 */}}
async function loadSymbol(){try{$('symbolStatus').textContent='加载中…';const data=await api('/api/symbol?symbol='+encodeURIComponent($('symbol').value));bars=data.bars;const r=+$('range').value;start=r?Math.max(0,bars.length-r):0;hover=-1;$('chartTitle').textContent=`${data.symbol} · ${bars.length.toLocaleString()} 个交易日`;draw();$('symbolStatus').textContent=''}catch(e){$('symbolStatus').textContent=e.message}}
async function loadMarket(){try{$('marketStatus').textContent='加载中…';const ex=$('exchange').value,date=$('tradeDate').value.replaceAll('-','');const data=await api(`/api/market?exchange=${ex}&date=${date}`);$('tradeDate').value=`${data.date.slice(0,4)}-${data.date.slice(4,6)}-${data.date.slice(6)}`;const s=data.summary;$('stats').innerHTML=[['股票',s.total],['成交',s.traded],['上涨',s.up],['下跌',s.down],['成交额',fmt(s.amount/1e8)+' 亿']].map(([a,b])=>`<div class="stat"><small>${a}</small><strong>${b}</strong></div>`).join('');$('marketRows').innerHTML=data.rows.map(r=>`<tr><td>${r.symbol}</td><td>${fmt(r.close)}</td><td class="${r.pct_change>=0?'up':'down'}">${fmt(r.pct_change)}%</td><td>${fmt(r.amount/1e8)} 亿</td><td>${fmt(r.turnover_rate)}%</td><td>${r.trade_status?'交易':'停牌'}${r.is_st?' · ST':''}</td></tr>`).join('');$('marketStatus').textContent=`${data.date} · 按绝对涨跌幅排序`}catch(e){$('marketStatus').textContent=e.message}}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(q=>q.classList.remove('active'));b.classList.add('active');$('chart').classList.toggle('hidden',b.dataset.view!=='chart');$('market').classList.toggle('hidden',b.dataset.view!=='market');if(b.dataset.view==='market'&&!$('marketRows').children.length)loadMarket()});$('loadSymbol').onclick=loadSymbol;$('symbol').oninput=loadChoices;$('symbol').onfocus=loadChoices;$('symbol').onkeydown=e=>{if(e.key==='Enter')loadSymbol()};$('range').onchange=()=>{const r=+$('range').value;start=r?Math.max(0,bars.length-r):0;draw()};$('loadMarket').onclick=loadMarket;$('kline').onmousemove=e=>{const box=e.currentTarget.getBoundingClientRect(),part=bars.slice(start),step=Math.max(2,(box.width-56)/Math.max(part.length,1));hover=Math.min(bars.length-1,Math.max(start,Math.round((e.clientX-box.left-50)/step)+start));draw()};$('kline').onmouseleave=()=>{hover=-1;draw()};$('kline').addEventListener('wheel',e=>{if(!bars.length)return;e.preventDefault();const current=bars.length-start;const next=Math.max(30,Math.min(bars.length,Math.round(current*(e.deltaY>0?1.25:.8))));start=bars.length-next;draw()},{passive:false});loadChoices();loadSymbol();
</script></body></html>'''


def handler_factory(store: MarketData):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, payload: object) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path); query = parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    encoded = PAGE.encode("utf-8")
                    self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded); return
                if parsed.path == "/api/symbol":
                    self.send_json(200, store.symbol(query.get("symbol", [""])[0])); return
                if parsed.path == "/api/symbols":
                    exchange = query.get("exchange", [""])[0].lower()
                    if exchange not in {"", "sz", "sh"}:
                        raise ValueError("exchange must be sz, sh, or blank")
                    self.send_json(200, {"symbols": store.symbols(query.get("q", [""])[0], exchange)}); return
                if parsed.path == "/api/market":
                    exchange = query.get("exchange", ["sz"])[0].lower()
                    if exchange not in {"sz", "sh"}:
                        raise ValueError("exchange must be sz or sh")
                    self.send_json(200, store.market(exchange, query.get("date", [""])[0])); return
                self.send_json(404, {"error": "not found"})
            except (FileNotFoundError, ValueError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover - keeps the local UI usable on malformed source rows.
                self.send_json(500, {"error": f"server error: {exc}"})

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return
    return Handler


def main() -> int:
    args = parse_args()
    root = Path(args.data_root) if args.data_root else data_path()
    server = ThreadingHTTPServer((args.host, args.port), handler_factory(MarketData(root)))
    print(f"Kolmo terminal: http://{args.host}:{args.port}  data={root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
