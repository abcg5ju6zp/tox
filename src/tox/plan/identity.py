from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tox.util.json_types import JsonValue

PLAN_SCHEMA_VERSION = "1"
_PLAN_ID_PREFIX = "sha256"


@dataclass(frozen=True)
class PlanIdentity:
    algorithm: str
    digest: str

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.digest}"


def canonical_json(content: JsonValue) -> bytes:
    """Return deterministic JSON bytes used for plan content identity."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def plan_digest(content: JsonValue) -> PlanIdentity:
    return PlanIdentity(_PLAN_ID_PREFIX, hashlib.sha256(canonical_json(content)).hexdigest())

