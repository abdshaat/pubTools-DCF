"""Build bundled demo snapshots from locally-cached raw FMP responses.

The public (Vercel) deployment serves a fixed allowlist of tickers from these
snapshots so it makes ZERO live provider calls — protecting the daily FMP
budget and working on stateless serverless (where the in-memory cache can't
persist). Run this only when refreshing the demo set; it reads the raw files
already in data/raw/ and makes no network requests.

    python scripts/build_demo_snapshots.py AAPL MSFT NVDA WMT

Output: app/demo_data/{TICKER}.json
"""

import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.normalization import normalize_fmp_fundamentals
from app.providers.fmp import FMPFundamentals

ROOT = Path(__file__).parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "app" / "demo_data"

_ENDPOINTS = {
    "income": "income-statement",
    "balance": "balance-sheet-statement",
    "cash_flow": "cash-flow-statement",
    "profile": "profile",
    "quote": "quote",
}


def _latest(ticker: str, endpoint: str) -> dict:
    """Newest recorded payload for one endpoint (files are name_<epoch>.json)."""
    candidates = sorted((RAW / ticker).glob(f"{endpoint}_*.json"))
    if not candidates:
        raise SystemExit(f"no raw {endpoint} for {ticker} — fetch it live first")
    payload = json.loads(candidates[-1].read_text(encoding="utf-8"))
    return payload[0] if isinstance(payload, list) else payload


def build(ticker: str) -> None:
    ticker = ticker.upper()
    sections = {key: _latest(ticker, ep) for key, ep in _ENDPOINTS.items()}
    fundamentals = FMPFundamentals(
        ticker=ticker,
        income=(sections["income"],),
        balance=(sections["balance"],),
        cash_flow=(sections["cash_flow"],),
        profile=sections["profile"],
        quote=sections["quote"],
    )
    base = normalize_fmp_fundamentals(fundamentals)

    OUT.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "ticker": ticker,
        "name": sections["profile"].get("companyName", ticker),
        "captured_at": date.today().isoformat(),
        "base_financials": asdict(base),
    }
    (OUT / f"{ticker}.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"  wrote app/demo_data/{ticker}.json  ({base.source_period})")


if __name__ == "__main__":
    tickers = sys.argv[1:] or ["AAPL", "MSFT", "NVDA", "WMT"]
    print(f"Building {len(tickers)} demo snapshot(s) from data/raw/ (no network):")
    for t in tickers:
        build(t)
