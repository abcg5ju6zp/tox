from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tox.execute import Outcome
from tox.session.status import SESSION_STATE_FILE, EnvRecord, EnvStatus, SessionStatus
from tox.version import version

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / ".tox" / SESSION_STATE_FILE


def _write_state(path: Path, envs: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "toxversion": version, "envs": envs}), encoding="utf-8")


def test_starts_with_every_environment_queued(state_path: Path) -> None:
    status = SessionStatus.open(state_path, resume=True)
    status.begin(["a", "b"])

    assert status.status_of("a") is EnvStatus.QUEUED
    assert status.status_of("b") is EnvStatus.QUEUED
    assert set(json.loads(state_path.read_text(encoding="utf-8"))["envs"]) == {"a", "b"}


def test_running_to_terminal_transitions(state_path: Path) -> None:
    status = SessionStatus.open(state_path, resume=True)
    status.begin(["a", "b"])

    status.mark_running("a")
    assert status.status_of("a") is EnvStatus.RUNNING

    assert (
        status.finish("a", exit_code=Outcome.OK, skipped=False, ignore_outcome=False, duration=1.0, interrupted=False)
        is EnvStatus.SUCCESS
    )
    status.mark_running("b")
    assert (
        status.finish("b", exit_code=3, skipped=False, ignore_outcome=False, duration=1.0, interrupted=False)
        is EnvStatus.FAILED
    )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))["envs"]
    assert persisted["a"]["status"] == "success"
    assert persisted["b"]["status"] == "failed"
    assert persisted["b"]["exit_code"] == 3


def test_ignored_outcome_and_skipped_count_as_success(state_path: Path) -> None:
    status = SessionStatus.open(state_path, resume=True)
    status.begin(["a", "b"])
    status.mark_running("a")
    assert (
        status.finish("a", exit_code=5, skipped=False, ignore_outcome=True, duration=0, interrupted=False)
        is EnvStatus.SUCCESS
    )
    status.mark_running("b")
    assert (
        status.finish("b", exit_code=Outcome.OK, skipped=True, ignore_outcome=False, duration=0, interrupted=False)
        is EnvStatus.SUCCESS
    )


def test_finish_during_interrupt_is_cancelled(state_path: Path) -> None:
    status = SessionStatus.open(state_path, resume=True)
    status.begin(["a"])
    status.mark_running("a")
    result = status.finish("a", exit_code=Outcome.OK, skipped=False, ignore_outcome=False, duration=0, interrupted=True)
    assert result is EnvStatus.CANCELLED
    assert status.records["a"].reason == "interrupted"


def test_failure_propagates_to_dependents(state_path: Path) -> None:
    status = SessionStatus.open(state_path, resume=True)
    status.begin(["a", "b", "c"])
    todo = {"a": set(), "b": {"a"}, "c": {"b"}}

    status.mark_running("a")
    status.finish("a", exit_code=1, skipped=False, ignore_outcome=False, duration=0, interrupted=False)

    assert status.propagate(todo) == [
        ("b", "dependency a failed"),
        ("c", "dependency b cancelled"),
    ]
    assert status.status_of("b") is EnvStatus.CANCELLED
    assert status.status_of("c") is EnvStatus.CANCELLED
    assert status.records["b"].skipped is True
    # independent environments keep waiting
    assert status.records["b"].exit_code == -2


def test_propagation_leaves_independent_and_running_envs_alone(state_path: Path) -> None:
    status = SessionStatus.open(state_path, resume=True)
    status.begin(["a", "b", "c"])
    todo = {"a": set(), "b": set(), "c": {"b"}}
    status.mark_running("a")
    status.finish("a", exit_code=1, skipped=False, ignore_outcome=False, duration=0, interrupted=False)

    assert status.propagate(todo) == []
    assert status.status_of("b") is EnvStatus.QUEUED
    assert status.status_of("c") is EnvStatus.QUEUED


def test_cancelled_dependency_propagates_as_well(state_path: Path) -> None:
    status = SessionStatus.open(state_path, resume=True)
    status.begin(["a", "b"])
    todo = {"a": set(), "b": {"a"}}
    assert status.cancel_queued(["a"], exit_code=-2, skipped=False, reason="interrupted") == ["a"]

    assert status.propagate(todo) == [("b", "dependency a cancelled")]
    assert status.status_of("b") is EnvStatus.CANCELLED


def test_cancel_queued_keeps_order_and_skips_other_states(state_path: Path) -> None:
    status = SessionStatus.open(state_path, resume=True)
    status.begin(["a", "b", "c"])
    status.mark_running("b")

    assert status.cancel_queued(["c", "a", "b"], exit_code=-2, skipped=True, reason="fail fast") == ["c", "a"]


def test_only_successful_environments_are_reused(state_path: Path) -> None:
    _write_state(
        state_path,
        {
            "ok": {
                "status": "success",
                "exit_code": 0,
                "skipped": False,
                "ignore_outcome": False,
                "duration": 1.5,
                "reason": None,
            },
            "failed": {
                "status": "failed",
                "exit_code": 1,
                "skipped": False,
                "ignore_outcome": False,
                "duration": 2.0,
                "reason": None,
            },
            "running": {
                "status": "running",
                "exit_code": None,
                "skipped": False,
                "ignore_outcome": False,
                "duration": None,
                "reason": None,
            },
            "queued": {
                "status": "queued",
                "exit_code": None,
                "skipped": False,
                "ignore_outcome": False,
                "duration": None,
                "reason": None,
            },
            "cancelled": {
                "status": "cancelled",
                "exit_code": -2,
                "skipped": True,
                "ignore_outcome": False,
                "duration": 0.01,
                "reason": "dependency failed failed",
            },
        },
    )
    status = SessionStatus.open(state_path, resume=True)

    reused = status.reusable(["ok", "failed", "running", "queued", "cancelled", "never-seen"])

    assert list(reused) == ["ok"]
    assert reused["ok"].duration == pytest.approx(1.5)


def test_reused_success_stays_persisted_when_session_begins(state_path: Path) -> None:
    _write_state(
        state_path,
        {
            "a": {
                "status": "success",
                "exit_code": 0,
                "skipped": False,
                "ignore_outcome": False,
                "duration": 1.0,
                "reason": None,
            },
            "b": {
                "status": "running",
                "exit_code": None,
                "skipped": False,
                "ignore_outcome": False,
                "duration": None,
                "reason": None,
            },
        },
    )
    status = SessionStatus.open(state_path, resume=True)
    reused = status.reusable(["a", "b"])
    status.begin(["b"], reused=reused)

    assert status.status_of("a") is EnvStatus.SUCCESS
    assert status.status_of("b") is EnvStatus.QUEUED
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["envs"]
    assert persisted["a"]["status"] == "success"
    assert persisted["b"]["status"] == "queued"


def test_no_resume_removes_stale_state(state_path: Path) -> None:
    _write_state(
        state_path,
        {
            "a": {
                "status": "success",
                "exit_code": 0,
                "skipped": False,
                "ignore_outcome": False,
                "duration": 1.0,
                "reason": None,
            }
        },
    )
    status = SessionStatus.open(state_path, resume=False)

    assert not state_path.exists()
    assert status.reusable(["a"]) == {}


def test_corrupt_state_is_ignored_and_removed(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not json", encoding="utf-8")
    status = SessionStatus.open(state_path, resume=True)

    assert status.reusable(["a"]) == {}
    assert not state_path.exists()


def test_incompatible_state_version_is_ignored(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"version": 99, "toxversion": version, "envs": {}}), encoding="utf-8")

    status = SessionStatus.open(state_path, resume=True)

    assert status.reusable(["a"]) == {}
    assert not state_path.exists()


def test_discard_removes_the_state_file(state_path: Path) -> None:
    status = SessionStatus.open(state_path, resume=True)
    status.begin(["a"])
    assert state_path.exists()

    status.discard()

    assert not state_path.exists()


def test_record_json_round_trip() -> None:
    record = EnvRecord(EnvStatus.FAILED, exit_code=7, duration=2.5, reason=None)
    assert EnvRecord.from_json(record.to_json()) == record


@pytest.mark.parametrize("raw", ["x", 42, {"status": "bogus"}, {"status": 1}])
def test_record_from_invalid_json(raw: object) -> None:
    assert EnvRecord.from_json(raw) is None
