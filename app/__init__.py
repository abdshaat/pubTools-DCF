"""DCF Valuation API — core package.

Public surface is the pure DCF engine and its data types. The API layer
(FastAPI routes) and data-provider clients will live in submodules and
should depend on these, never the other way around.
"""

from .dcf_engine import DCFValidationError, compute_dcf
from .models import Assumptions, BaseFinancials, Valuation, YearProjection

MODEL_VERSION = "0.1.0"

__all__ = [
    "Assumptions",
    "BaseFinancials",
    "DCFValidationError",
    "MODEL_VERSION",
    "Valuation",
    "YearProjection",
    "compute_dcf",
]
