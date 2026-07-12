"""Print recent Supabase usage events for admin review."""

import argparse
import asyncio
import json
import os
from typing import Any

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value.rstrip("/") if name == "SUPABASE_URL" else value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report recent pubTools-DCF usage events")
    parser.add_argument("--customer-id")
    parser.add_argument("--api-key-id")
    parser.add_argument("--limit", type=int, default=100)
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.limit < 1 or args.limit > 1000:
        raise RuntimeError("--limit must be between 1 and 1000")

    params: dict[str, str] = {
        "select": (
            "recorded_at,request_id,customer_id,api_key_id,ticker,status_code,"
            "quota_consumed,rate_limited"
        ),
        "order": "recorded_at.desc",
        "limit": str(args.limit),
    }
    if args.customer_id:
        params["customer_id"] = f"eq.{args.customer_id}"
    if args.api_key_id:
        params["api_key_id"] = f"eq.{args.api_key_id}"

    supabase_url = _required_env("SUPABASE_URL")
    service_key = _required_env("SUPABASE_SERVICE_ROLE_KEY")
    headers = {
        "apikey": service_key,
        "authorization": f"Bearer {service_key}",
    }
    async with httpx.AsyncClient(base_url=supabase_url, headers=headers, timeout=10.0) as client:
        response = await client.get("/rest/v1/usage_events", params=params)
        response.raise_for_status()
        events: Any = response.json()

    print(json.dumps(events, indent=2, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
