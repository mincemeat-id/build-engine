"""Pinned contract snapshot checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "contracts" / "openapi" / "build-engine.openapi.json"
OPENAPI_SHA = ROOT / "contracts" / "openapi" / "build-engine.openapi.sha256"


def test_build_engine_openapi_snapshot_matches_pinned_sha() -> None:
    expected = OPENAPI_SHA.read_text(encoding="utf-8").split()[0]
    actual = hashlib.sha256(OPENAPI.read_bytes()).hexdigest()

    assert actual == expected, (
        "contracts/openapi/build-engine.openapi.json drifted from the pinned SHA. "
        "Run `make contracts-sync`, review the API diff, and update "
        "contracts/openapi/build-engine.openapi.sha256 when the contract change is intentional."
    )
