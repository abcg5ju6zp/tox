from __future__ import annotations

import contextlib
import hmac
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

from .errors import PlanError
from .identity import PLAN_SCHEMA_VERSION, PlanIdentity, plan_digest

if TYPE_CHECKING:
    from tox.util.json_types import JsonValue


def save_plan(path: Path, content: dict[str, JsonValue]) -> PlanIdentity:
    """Atomically publish a complete plan document."""
    target = path.resolve()
    plan_content = dict(content)
    plan_content["schema_version"] = PLAN_SCHEMA_VERSION
    identity = plan_digest(plan_content)
    document = {"schema_version": PLAN_SCHEMA_VERSION, "plan": plan_content, "identity": _identity_json(identity)}
    payload = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(target)
        _fsync_directory(target.parent)
    finally:
        if temporary_name:
            with contextlib.suppress(FileNotFoundError):
                Path(temporary_name).unlink()
    return identity


def load_plan(path: Path) -> dict[str, JsonValue]:
    """Load and verify a plan document without using any environment state."""
    if path.name.startswith(".") and path.name.endswith(".tmp"):
        msg = f"refusing to replay uncommitted plan temporary file {path}"
        raise PlanError(msg)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        msg = f"cannot read execution plan {path}: {exc}"
        raise PlanError(msg) from exc
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"execution plan {path} is not valid JSON: {exc}"
        raise PlanError(msg) from exc
    if not isinstance(document, dict):
        msg = "execution plan document must be a JSON object"
        raise PlanError(msg)

    schema = document.get("schema_version")
    if schema != PLAN_SCHEMA_VERSION:
        msg = f"unsupported execution plan schema version {schema!r}, expected {PLAN_SCHEMA_VERSION!r}"
        raise PlanError(msg)
    content = document.get("plan")
    if not isinstance(content, dict):
        msg = "execution plan is missing a plan object"
        raise PlanError(msg)
    content_schema = content.get("schema_version")
    if content_schema != PLAN_SCHEMA_VERSION:
        msg = f"unsupported plan content schema version {content_schema!r}, expected {PLAN_SCHEMA_VERSION!r}"
        raise PlanError(msg)

    expected = plan_digest(content)
    actual = _coerce_identity(document.get("identity"), path)
    if actual.algorithm != expected.algorithm or not hmac.compare_digest(actual.digest, expected.digest):
        msg = f"execution plan {path} content identity mismatch: expected {expected}, found {actual}"
        raise PlanError(msg)
    return content


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _identity_json(identity: PlanIdentity) -> dict[str, str]:
    return {"algorithm": identity.algorithm, "digest": identity.digest}


def _coerce_identity(value: object, path: Path) -> PlanIdentity:
    if not isinstance(value, dict):
        msg = f"execution plan {path} is missing its content identity"
        raise PlanError(msg)
    algorithm = value.get("algorithm")
    digest = value.get("digest")
    if not isinstance(algorithm, str) or not isinstance(digest, str):
        msg = f"execution plan {path} has an invalid content identity"
        raise PlanError(msg)
    return PlanIdentity(algorithm, digest)
