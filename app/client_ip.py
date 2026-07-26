"""Which address is the client's, and when may a header be believed?

Phase 11 Slice 4, closing the finding raised in the 2026-07-12 security review.
Per-IP login rate limiting keyed off `request.client.host`, which on Vercel is
the platform's proxy, not the customer — so the daily sign-in cap behaved as one
shared bucket for the whole internet rather than a per-IP limit. Trusting
`X-Forwarded-For` unconditionally would be worse: anyone could then mint a fresh
quota per request by inventing a header value.

The rule here is the standard one, made explicit and configurable:

- `TRUSTED_PROXY_HOPS = 0` (the default off-platform): believe nothing, use the
  socket peer address.
- `TRUSTED_PROXY_HOPS = N`: exactly N proxies stand between the client and this
  process, so the last N entries of `X-Forwarded-For` were appended by
  infrastructure we trust. The client is the Nth entry counted from the right;
  everything to its left is caller-supplied and ignored. A spoofed prefix
  therefore changes nothing.
- A chain shorter than N proxies means the request did not arrive the way the
  configuration claims, so the header is discarded and the peer address is used.
  Failing back to a *narrower* identity is the safe direction: it can group
  callers together, never split one caller into unlimited buckets.

On Vercel the platform is exactly one hop, which is the default when `VERCEL` is
set.
"""

from dataclasses import dataclass

UNKNOWN_CLIENT = "unknown"

FORWARDED_FOR_HEADER = "x-forwarded-for"

# Where the address came from. Logged instead of the address itself: the source
# is what proves the configuration is right, while the address is personal data.
SOURCE_PEER = "peer"
SOURCE_FORWARDED = "forwarded"
SOURCE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClientIdentity:
    address: str
    source: str


def resolve_client_identity(
    *,
    peer: str | None,
    forwarded_for: str | None,
    trusted_proxy_hops: int,
) -> ClientIdentity:
    """Resolve the caller's address under an explicit trust configuration."""
    peer_address = (peer or "").strip()
    if trusted_proxy_hops > 0 and forwarded_for:
        chain = [_normalize(entry) for entry in forwarded_for.split(",")]
        chain = [entry for entry in chain if entry]
        if len(chain) >= trusted_proxy_hops:
            return ClientIdentity(chain[-trusted_proxy_hops], SOURCE_FORWARDED)
    if peer_address:
        return ClientIdentity(peer_address, SOURCE_PEER)
    return ClientIdentity(UNKNOWN_CLIENT, SOURCE_UNKNOWN)


def _normalize(entry: str) -> str:
    """Strip whitespace, brackets, and any trailing port from one XFF entry.

    Proxies emit `1.2.3.4`, `1.2.3.4:51234`, `[2001:db8::1]`, and
    `[2001:db8::1]:443`. Without normalization the same caller on two source
    ports would look like two callers and get two quotas.
    """
    value = entry.strip()
    if not value:
        return ""
    if value.startswith("["):
        host, _, _ = value.partition("]")
        return host[1:]
    # A bare IPv6 address has several colons; only strip a port from what looks
    # like host:port.
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host
    return value
