"""Domain exceptions shared across layers.

The API layer maps these to HTTP responses:
  TickerNotFoundError    -> 404
  TickerNotCoveredError  -> 404 (data provider won't serve this symbol)
  UnsupportedSectorError -> 422
  NormalizationError     -> 502 (provider data unusable, not the caller's fault)
  ProviderAuthError      -> 500 (server misconfiguration)
  ProviderError          -> 503
  SnapshotStoreError     -> 503 (durable statement store unavailable)
"""


class ProviderError(Exception):
    """Upstream data provider failed (network, 5xx, rate limit exhausted)."""


class ProviderAuthError(ProviderError):
    """API key missing or rejected by the provider."""


class TickerNotFoundError(ProviderError):
    """The provider confirms it has no such symbol (HTTP 404 / empty result)."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(f"ticker not found: {ticker}")


class TickerNotCoveredError(ProviderError):
    """The provider refuses to serve this symbol under the current data plan
    (FMP answers HTTP 402 Payment Required). On restricted plans this is also
    returned for symbols that simply don't exist, so the two can't be told
    apart from the response alone — the customer-facing message says so.
    """

    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(
            f"financials for {ticker} are not available from the data provider "
            "under the current plan; the symbol may not exist or may fall outside "
            "the supported universe (non-financial US large caps)"
        )


class UnsupportedSectorError(Exception):
    """Banks/insurers: standard FCF DCF doesn't apply (v1 scope)."""

    def __init__(self, ticker: str, sector: str):
        self.ticker = ticker
        self.sector = sector
        super().__init__(
            f"{ticker} is in sector '{sector}'; standard FCF DCF does not apply "
            "to financial companies (v1 supports non-financial US large caps only)"
        )


class SnapshotStoreError(Exception):
    """The durable statement store (Supabase snapshots) failed. A store error
    is NOT a confirmed miss (ADR-006): customer traffic must never fall
    through to the provider on one, or an outage would break the once-daily
    provider guarantee and could spike FMP usage."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(f"durable statement store unavailable for {ticker}")


class NormalizationError(Exception):
    """Provider payload has missing or unusable canonical fields."""

    def __init__(self, ticker: str, missing: list[str]):
        self.ticker = ticker
        self.missing = missing
        super().__init__(f"cannot normalize {ticker}: missing or invalid fields {missing}")
