"""Domain exceptions shared across layers.

The API layer maps these to HTTP responses:
  TickerNotFoundError    -> 404
  UnsupportedSectorError -> 422
  NormalizationError     -> 502 (provider data unusable, not the caller's fault)
  ProviderAuthError      -> 500 (server misconfiguration)
  ProviderError          -> 503
"""


class ProviderError(Exception):
    """Upstream data provider failed (network, 5xx, rate limit exhausted)."""


class ProviderAuthError(ProviderError):
    """API key missing or rejected by the provider."""


class TickerNotFoundError(ProviderError):
    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(f"ticker not found: {ticker}")


class UnsupportedSectorError(Exception):
    """Banks/insurers: standard FCF DCF doesn't apply (v1 scope)."""

    def __init__(self, ticker: str, sector: str):
        self.ticker = ticker
        self.sector = sector
        super().__init__(
            f"{ticker} is in sector '{sector}'; standard FCF DCF does not apply "
            "to financial companies (v1 supports non-financial US large caps only)"
        )


class NormalizationError(Exception):
    """Provider payload is missing fields the canonical schema requires."""

    def __init__(self, ticker: str, missing: list[str]):
        self.ticker = ticker
        self.missing = missing
        super().__init__(f"cannot normalize {ticker}: missing fields {missing}")
