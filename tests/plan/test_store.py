from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tox.plan import PLAN_SCHEMA_VERSION, PlanError, canonical_json, load_plan, plan_digest, save_plan

if TYPE_CHECKING:
    from pathlib import Path


def test_save_and_load_plan_has_identity(tmp_path: Path) -> None:
    target = tmp_path / "plan.json"
    content = {"b": [2, 1], "a": "value"}

    identity = save_plan(target, content)

    document = json.loads(target.read_text(encoding="utf-8"))
    saved_content = document["plan"]
    assert identity == plan_digest(saved_content)
    assert (
        str(identity)
        == f"sha256:{plan_digest({'a': 'value', 'b': [2, 1], 'schema_version': PLAN_SCHEMA_VERSION}).digest}"
    )
    assert load_plan(target) == saved_content


def test_plan_identity_is_canonical(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    first_identity = save_plan(first, {"z": 1, "a": [1, 2]})
    second_identity = save_plan(second, {"a": [1, 2], "z": 1})

    assert first_identity == second_identity
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_temporary_plan_is_not_replayable_when_publish_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "plan.json"

    def fail_replace(_source: str, _destination: str) -> None:
        message = "boom"
        raise OSError(message)

    monkeypatch.setattr("tox.plan.store.Path.replace", lambda self, target: fail_replace(str(self), str(target)))

    with pytest.raises(OSError, match="boom"):
        save_plan(target, {"a": 1})

    assert not target.exists()


def test_load_rejects_uncommitted_temporary_file(tmp_path: Path) -> None:
    temporary = tmp_path / ".plan.json.123.tmp"
    save_plan(temporary, {"a": 1})

    with pytest.raises(PlanError, match="uncommitted plan temporary file"):
        load_plan(temporary)


def test_load_rejects_tampered_plan(tmp_path: Path) -> None:
    target = tmp_path / "plan.json"
    save_plan(target, {"a": 1})
    document = json.loads(target.read_text(encoding="utf-8"))
    document["plan"]["a"] = 2
    target.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PlanError, match="content identity mismatch"):
        load_plan(target)


def test_load_rejects_unsupported_schema(tmp_path: Path) -> None:
    target = tmp_path / "plan.json"
    target.write_text(json.dumps({"schema_version": "0", "plan": {}, "identity": {}}), encoding="utf-8")

    with pytest.raises(PlanError, match="unsupported execution plan schema version"):
        load_plan(target)
