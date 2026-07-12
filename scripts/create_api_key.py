"""Create a customer API key in Supabase.

This is an admin-only helper. Run it from a trusted machine with
SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY set. It prints the full API key once;
only a versioned hash is stored in Supabase.
"""

import argparse
import asyncio
import os
import secrets
import string
from typing import Any

import httpx

from app.auth import APIKeyAuthenticator

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


def _generate_prefix() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


async def _post_returning(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        path,
        headers={"Prefer": "return=representation"},
        json=payload,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise RuntimeError(f"unexpected Supabase response from {path}")
    return data[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a pubTools-DCF API key")
    customer = parser.add_mutually_exclusive_group(required=True)
    customer.add_argument("--customer-id", help="Existing api_customers.id UUID")
    customer.add_argument("--customer-name", help="Create a new customer with this name")
    parser.add_argument("--prefix", default=_generate_prefix(), help="Public key prefix")
    parser.add_argument("--daily-quota", type=int, default=100)
    parser.add_argument("--scope", action="append", default=["valuation:read"])
    parser.add_argument("--expires-at", help="Optional ISO timestamp")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.daily_quota < 1:
        raise RuntimeError("--daily-quota must be positive")

    supabase_url = _required_env("SUPABASE_URL")
    service_key = _required_env("SUPABASE_SERVICE_ROLE_KEY")
    headers = {
        "apikey": service_key,
        "authorization": f"Bearer {service_key}",
    }
    async with httpx.AsyncClient(base_url=supabase_url, headers=headers, timeout=10.0) as client:
        customer_id = args.customer_id
        if customer_id is None:
            customer = await _post_returning(
                client,
                "/rest/v1/api_customers",
                {"name": args.customer_name},
            )
            customer_id = str(customer["id"])

        full_key = f"dcf_{args.prefix}_{secrets.token_urlsafe(32)}"
        api_key = await _post_returning(
            client,
            "/rest/v1/api_keys",
            {
                "customer_id": customer_id,
                "prefix": args.prefix,
                "secret_hash": APIKeyAuthenticator.hash_secret(full_key),
                "scopes": args.scope,
                "daily_quota": args.daily_quota,
                "expires_at": args.expires_at,
            },
        )
        await client.post(
            "/rest/v1/audit_events",
            json={
                "customer_id": customer_id,
                "api_key_id": api_key["id"],
                "action": "api_key.created",
                "metadata": {
                    "prefix": args.prefix,
                    "daily_quota": args.daily_quota,
                    "scopes": args.scope,
                    "expires_at": args.expires_at,
                },
            },
        )

    print("Created API key. Copy it now; it is not stored in plaintext.")
    print(full_key)
    return 0


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
