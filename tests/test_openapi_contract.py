"""Stable OpenAPI snapshot for intentional API-contract review."""

import hashlib
import json
from pathlib import Path

from app.api import app


def test_openapi_contract_matches_reviewed_snapshot():
    canonical = json.dumps(app.openapi(), sort_keys=True, separators=(",", ":")).encode()
    actual = hashlib.sha256(canonical).hexdigest()
    expected = (
        (Path(__file__).parent / "snapshots" / "openapi.sha256").read_text(encoding="utf-8").strip()
    )
    assert actual == expected, (
        "OpenAPI changed. Review the generated contract and update the snapshot "
        "only when the change is intentional and documented."
    )
