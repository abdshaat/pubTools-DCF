"""Inspect, replay, and prune the raw provider evidence in data/raw/.

Phase 10 tooling. Every subcommand is offline — nothing here makes a provider
call, so replaying a normalization bug costs nothing against the FMP daily
budget.

    python scripts/raw_captures.py stats
    python scripts/raw_captures.py list AAPL
    python scripts/raw_captures.py replay AAPL
    python scripts/raw_captures.py replay AAPL --index -2      # previous capture
    python scripts/raw_captures.py prune --retention-days 30 --max-per-endpoint 25

`replay` renormalizes stored payloads through the real normalization layer and
prints the base financials the API would have derived from them.
"""

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.normalization import normalize_fmp_fundamentals
from app.providers.fmp import FMPFundamentals
from app.raw_store import (
    FileRawSink,
    StoredCapture,
    iter_capture_paths,
    load_capture,
    load_captures,
    usage_report,
)

ROOT = Path(__file__).parent.parent
RAW = ROOT / "data" / "raw"

_SECTIONS = {
    "income": "income-statement",
    "balance": "balance-sheet-statement",
    "cash_flow": "cash-flow-statement",
    "profile": "profile",
}


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GB"  # pragma: no cover - unreachable, loop returns


def _capture_at(root: Path, ticker: str, endpoint: str, index: int) -> StoredCapture:
    paths = iter_capture_paths(root, ticker, endpoint)
    if not paths:
        raise SystemExit(f"no stored {endpoint} capture for {ticker} under {root}")
    try:
        return load_capture(paths[index])
    except IndexError:
        raise SystemExit(
            f"{ticker}/{endpoint}: index {index} out of range ({len(paths)} captures)"
        ) from None


def command_stats(args: argparse.Namespace) -> int:
    entries = usage_report(args.root)
    if not entries:
        print(f"no captures under {args.root}")
        return 0
    total_bytes = sum(entry.bytes for entry in entries)
    total_captures = sum(entry.captures for entry in entries)
    print(f"{'TICKER':<8} {'ENDPOINT':<24} {'CAPTURES':>8} {'SIZE':>10}  NEWEST")
    for entry in entries:
        newest = entry.newest.isoformat(timespec="seconds") if entry.newest else "-"
        print(
            f"{entry.ticker:<8} {entry.endpoint:<24} {entry.captures:>8} "
            f"{_human_bytes(entry.bytes):>10}  {newest}"
        )
    print(f"\ntotal: {total_captures} captures, {_human_bytes(total_bytes)}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    found = False
    for endpoint in _SECTIONS.values():
        for capture in load_captures(args.root, args.ticker, endpoint):
            found = True
            print(
                f"{capture.captured_at.isoformat(timespec='seconds')}  {endpoint:<24} "
                f"{capture.content_sha256[:12] or '-':<12} "
                f"request={capture.document.get('request_id') or '-'}  {capture.path.name}"
            )
    if not found:
        print(f"no captures for {args.ticker.upper()} under {args.root}")
    return 0


def command_replay(args: argparse.Namespace) -> int:
    ticker = args.ticker.upper()
    sections = {
        key: _capture_at(args.root, ticker, endpoint, args.index)
        for key, endpoint in _SECTIONS.items()
    }
    for key, capture in sections.items():
        print(
            f"  {key:<10} {capture.captured_at.isoformat(timespec='seconds')}  {capture.path.name}"
        )

    def records(capture: StoredCapture) -> tuple[dict, ...]:
        payload = capture.payload
        return tuple(payload) if isinstance(payload, list) else (payload,)

    profile = sections["profile"].payload
    fundamentals = FMPFundamentals(
        ticker=ticker,
        income=records(sections["income"]),
        balance=records(sections["balance"]),
        cash_flow=records(sections["cash_flow"]),
        profile=profile[0] if isinstance(profile, list) else profile,
    )
    base = normalize_fmp_fundamentals(fundamentals)
    print(f"\nnormalized {ticker} from stored evidence (no provider calls):")
    for name, value in asdict(base).items():
        print(f"  {name:<24} {value}")
    return 0


def command_prune(args: argparse.Namespace) -> int:
    sink = FileRawSink(
        args.root,
        retention_days=args.retention_days,
        max_captures_per_endpoint=args.max_per_endpoint,
    )
    before = sum(entry.bytes for entry in usage_report(args.root))
    result = sink.prune_all()
    after = sum(entry.bytes for entry in usage_report(args.root))
    print(
        f"removed {result.captures_removed} captures "
        f"({_human_bytes(result.bytes_removed)}); {_human_bytes(before)} -> {_human_bytes(after)}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--root", type=Path, default=RAW, help="capture root (default data/raw)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    stats = subparsers.add_parser("stats", help="capture counts and disk usage per endpoint")
    stats.set_defaults(handler=command_stats)

    listing = subparsers.add_parser("list", help="captures stored for one ticker")
    listing.add_argument("ticker")
    listing.set_defaults(handler=command_list)

    replay = subparsers.add_parser("replay", help="renormalize stored payloads offline")
    replay.add_argument("ticker")
    replay.add_argument(
        "--index",
        type=int,
        default=-1,
        help="which capture per endpoint, newest-last (default -1)",
    )
    replay.set_defaults(handler=command_replay)

    prune = subparsers.add_parser("prune", help="apply the retention policy now")
    prune.add_argument("--retention-days", type=int, default=30)
    prune.add_argument("--max-per-endpoint", type=int, default=25)
    prune.set_defaults(handler=command_prune)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = args.handler
    result: int = handler(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
