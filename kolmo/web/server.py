#!/usr/bin/env python3
"""Local HTTP API and production frontend host for Kolmo Market Terminal."""

from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kolmo.paths import data_path
from kolmo.web.fundamentals import FundamentalStore
from kolmo.web.market_data import MarketStore, _iso_date


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FRONTEND = PROJECT_ROOT / "web" / "frontend" / "dist"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the Kolmo market-data API and web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Default binds to localhost only.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-root", default="", help="Defaults to KOLMO_DATA_ROOT.")
    parser.add_argument("--frontend-root", default="", help="Defaults to web/frontend/dist.")
    parser.add_argument("--open-browser", action="store_true", help="Open the terminal in the default browser.")
    return parser.parse_args(argv)


def _one(query: dict[str, list[str]], key: str, default: str = "") -> str:
    return query.get(key, [default])[0]


def _integer(value: str, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError as exc:
        raise ValueError("invalid integer query parameter") from exc
    if not low <= parsed <= high:
        raise ValueError(f"integer query parameter must be between {low} and {high}")
    return parsed


def handler_factory(store: MarketStore, fundamentals: FundamentalStore, frontend_root: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KolmoMarket/1.0"

        def _cors(self) -> None:
            origin = self.headers.get("Origin", "")
            if origin.startswith(("http://127.0.0.1:", "http://localhost:")):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin, Accept-Encoding")

        def send_json(self, status: int, payload: object) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if "gzip" in self.headers.get("Accept-Encoding", "") and len(encoded) >= 1024:
                encoded = gzip.compress(encoded, compresslevel=5, mtime=0)
                content_encoding = "gzip"
            else:
                content_encoding = ""
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if content_encoding:
                self.send_header("Content-Encoding", content_encoding)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def send_static(self, request_path: str, head_only: bool = False) -> None:
            relative = request_path.lstrip("/") or "index.html"
            candidate = (frontend_root / relative).resolve()
            root = frontend_root.resolve()
            if root not in candidate.parents and candidate != root:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not candidate.is_file():
                candidate = frontend_root / "index.html"
            if not candidate.is_file():
                self.send_json(503, {"error": "frontend is not built; run npm install && npm run build"})
                return
            encoded = candidate.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(encoded)))
            cache = "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable"
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if not head_only:
                self.wfile.write(encoded)

        def do_HEAD(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
                return
            self.send_static(parsed.path, head_only=True)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/v1/health":
                    self.send_json(200, {"ok": True, "instruments": len(store.instruments())})
                    return
                if parsed.path == "/api/v1/instruments":
                    market = _one(query, "market", "ALL").upper()
                    if market not in {"ALL", "CN", "US"}:
                        raise ValueError("market must be ALL, CN, or US")
                    limit = _integer(_one(query, "limit"), 30, 1, 100)
                    self.send_json(200, {"instruments": store.search(_one(query, "q"), market, limit)})
                    return
                if parsed.path == "/api/v1/bars":
                    start = _iso_date(_one(query, "start")) if _one(query, "start") else ""
                    end = _iso_date(_one(query, "end")) if _one(query, "end") else ""
                    limit = _integer(_one(query, "limit"), 0, 0, 20000)
                    payload = store.bars(
                        _one(query, "symbol"),
                        _one(query, "interval", "1d"),
                        _one(query, "price", "adjusted"),
                        start,
                        end,
                        limit,
                    )
                    self.send_json(200, payload)
                    return
                if parsed.path == "/api/v1/fundamentals/summary":
                    self.send_json(200, fundamentals.summary(_one(query, "symbol")))
                    return
                if parsed.path == "/api/v1/fundamentals/facts":
                    limit = _integer(_one(query, "limit"), 100, 1, 250)
                    offset = _integer(_one(query, "offset"), 0, 0, 1000000)
                    self.send_json(200, fundamentals.facts(
                        _one(query, "symbol"), _one(query, "q"),
                        _one(query, "taxonomy"), _one(query, "unit"),
                        _one(query, "form"), limit, offset,
                    ))
                    return
                if parsed.path == "/api/v1/fundamentals/filings":
                    limit = _integer(_one(query, "limit"), 100, 1, 250)
                    offset = _integer(_one(query, "offset"), 0, 0, 1000000)
                    self.send_json(200, fundamentals.filings(
                        _one(query, "symbol"), _one(query, "form"), limit, offset,
                    ))
                    return
                if parsed.path == "/api/v1/fundamentals/series":
                    self.send_json(200, fundamentals.series(
                        _one(query, "symbol"), _one(query, "taxonomy"),
                        _one(query, "tag"), _one(query, "unit"),
                    ))
                    return
                if parsed.path.startswith("/api/"):
                    self.send_json(404, {"error": "API route not found"})
                    return
                self.send_static(parsed.path)
            except (FileNotFoundError, ValueError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover
                self.send_json(500, {"error": f"server error: {exc}"})

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.data_root) if args.data_root else data_path()
    frontend = Path(args.frontend_root) if args.frontend_root else DEFAULT_FRONTEND
    store = MarketStore(root, PROJECT_ROOT)
    fundamentals = FundamentalStore(root)
    server = ThreadingHTTPServer(
        (args.host, args.port), handler_factory(store, fundamentals, frontend)
    )
    browser_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{browser_host}:{args.port}"
    print(f"Kolmo Market Terminal: {url}  data={root}", flush=True)
    if args.open_browser:
        threading.Timer(0.25, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
