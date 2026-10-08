"""Track and persist the per-environment state of a tox run session.

Every environment selected for a session moves through the states
:attr:`EnvStatus.QUEUED`, :attr:`EnvStatus.RUNNING` and one of the terminal states
:attr:`EnvStatus.SUCCESS`, :attr:`EnvStatus.FAILED` or :attr:`EnvStatus.CANCELLED`. The state is persisted atomically
under the tox work directory after every transition, so when a session ends early (a cancellation, a lost terminal or
a failed dependency) the next invocation can reuse the environments already confirmed as successful and only run the
unfinished ones again.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tox.execute import Outcome
from tox.version import version as tox_version

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tox.util.json_types import JsonValue

logger = logging.getLogger(__name__)

#: name of the session state file inside the tox work directory
SESSION_STATE_FILE = "session.json"
_STATE_VERSION = 1


class EnvStatus(str, Enum):
    """Lifecycle state of a tox environment within a session."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL = frozenset({EnvStatus.SUCCESS, EnvStatus.FAILED, EnvStatus.CANCELLED})
_FAILED_OR_CANCELLED = frozenset({EnvStatus.FAILED, EnvStatus.CANCELLED})


@dataclass
class EnvRecord:
    """Persisted state of a single environment."""

    status: EnvStatus
    exit_code: int | None = None
    skipped: bool = False
    ignore_outcome: bool = False
    duration: float | None = None
    reason: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """:returns: a JSON serializable representation of the record"""
        return {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "skipped": self.skipped,
            "ignore_outcome": self.ignore_outcome,
            "duration": self.duration,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, data: Any) -> EnvRecord | None:
        """Build a record from persisted JSON data, returning ``None`` for unreadable records.

        :param data: the raw JSON data

        """
        if not isinstance(data, dict):
            return None
        try:
            status = EnvStatus(str(data["status"]))
        except (KeyError, ValueError):
            return None
        exit_code = data.get("exit_code")
        duration = data.get("duration")
        raw_reason = data.get("reason")
        return cls(
            status=status,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            skipped=bool(data.get("skipped", False)),
            ignore_outcome=bool(data.get("ignore_outcome", False)),
            duration=duration if isinstance(duration, (int, float)) else None,
            reason=raw_reason if isinstance(raw_reason, str) else None,
        )


class SessionStatus:
    """In-memory and persisted state of the environments scheduled in a session."""

    def __init__(self, path: Path, previous: dict[str, EnvRecord] | None = None) -> None:
        self._path = path
        self._previous: dict[str, EnvRecord] = previous or {}
        self._records: dict[str, EnvRecord] = {}

    @classmethod
    def open(cls, path: Path, *, resume: bool) -> SessionStatus:
        """Open the session state, loading an interrupted session's records when present.

        :param path: location of the persisted state
        :param resume: reuse results from an earlier interrupted session; when ``False`` any stale state is removed

        """
        previous: dict[str, EnvRecord] = {}
        if path.exists():
            if resume:
                previous = cls._read(path)
            if not resume or not previous:
                # explicit re-run, or a state file we cannot understand: start the session clean
                _unlink(path)
        return cls(path, previous)

    @staticmethod
    def _read(path: Path) -> dict[str, EnvRecord]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("could not read session state %s, starting a new session", path, exc_info=True)
            return {}
        if not isinstance(data, dict) or data.get("version") != _STATE_VERSION or data.get("toxversion") != tox_version:
            logger.warning("ignoring incompatible session state %s", path)
            return {}
        envs = data.get("envs")
        if not isinstance(envs, dict):
            return {}
        result: dict[str, EnvRecord] = {}
        for name, raw in envs.items():
            if isinstance(name, str) and (record := EnvRecord.from_json(raw)) is not None:
                result[name] = record
        return result

    @property
    def records(self) -> dict[str, EnvRecord]:
        """:returns: the current session's records by environment name"""
        return self._records

    def reusable(self, names: Iterable[str]) -> dict[str, EnvRecord]:
        """Return the records of environments already confirmed successful in an earlier session.

        :param names: the environments selected for the current session

        """
        return {
            name: self._previous[name]
            for name in names
            if (record := self._previous.get(name)) is not None and record.status is EnvStatus.SUCCESS
        }

    def begin(self, names: Sequence[str], reused: dict[str, EnvRecord] | None = None) -> None:
        """Initialize the session: reused environments stay successful, every other one starts queued.

        :param names: the environments that still have to run
        :param reused: successful records carried over from an earlier session

        """
        self._records = dict(reused or {})
        for name in names:
            self._records[name] = EnvRecord(EnvStatus.QUEUED)
        self._flush()

    def status_of(self, name: str) -> EnvStatus | None:
        """:returns: the state of an environment, or ``None`` if it is not part of the session"""
        record = self._records.get(name)
        return None if record is None else record.status

    def is_terminal(self, name: str) -> bool:
        """:returns: a flag indicating if the environment already reached a terminal state"""
        return self.status_of(name) in _TERMINAL

    def mark_running(self, name: str) -> None:
        """Move an environment from queued to running.

        :param name: the environment name

        """
        record = self._require(name)
        if record.status is EnvStatus.QUEUED:
            record.status = EnvStatus.RUNNING
            record.reason = None
            self._flush()
        elif record.status is not EnvStatus.RUNNING:  # pragma: no branch # defensive, terminal states never resubmit
            msg = f"environment {name} cannot run from state {record.status.value}"
            raise RuntimeError(msg)

    def finish(  # ruff:ignore[too-many-arguments]
        self,
        name: str,
        *,
        exit_code: int,
        skipped: bool,
        ignore_outcome: bool,
        duration: float,
        interrupted: bool,
    ) -> EnvStatus:
        """Record the outcome of a finished environment and persist the session.

        :param name: the environment name
        :param exit_code: the environment exit code
        :param skipped: a flag indicating the environment was skipped
        :param ignore_outcome: a flag marking failures as ignored
        :param duration: the run duration in seconds
        :param interrupted: a flag indicating the session was being interrupted

        :returns: the resulting terminal state

        """
        record = self._require(name)
        if interrupted:
            status = EnvStatus.CANCELLED
            reason = "interrupted"
        elif skipped or ignore_outcome or exit_code == Outcome.OK:
            status = EnvStatus.SUCCESS
            reason = None
        else:
            status = EnvStatus.FAILED
            reason = None
        record.status = status
        record.exit_code = exit_code
        record.skipped = skipped
        record.ignore_outcome = ignore_outcome
        record.duration = duration
        record.reason = reason
        self._flush()
        return status

    def cancel(
        self,
        name: str,
        *,
        exit_code: int,
        skipped: bool,
        reason: str,
    ) -> EnvStatus:
        """Mark a non-terminal environment as cancelled.

        :param name: the environment name
        :param exit_code: the synthetic exit code used for reporting
        :param skipped: the synthetic skipped flag used for reporting
        :param reason: why the environment was cancelled

        :returns: the cancelled state

        """
        record = self._require(name)
        if record.status in _FAILED_OR_CANCELLED:  # pragma: no cover # terminal states are never cancelled again
            return record.status
        record.status = EnvStatus.CANCELLED
        record.exit_code = exit_code
        record.skipped = skipped
        record.reason = reason
        self._flush()
        return EnvStatus.CANCELLED

    def cancel_queued(
        self,
        names: Iterable[str],
        *,
        exit_code: int,
        skipped: bool,
        reason: str,
    ) -> list[str]:
        """Cancel every still-queued environment found in ``names``, preserving the given iteration order.

        :param names: candidate environment names (typically in execution order)
        :param exit_code: the synthetic exit code used for reporting
        :param skipped: the synthetic skipped flag used for reporting
        :param reason: why the environments were cancelled

        :returns: the names of the environments that were cancelled

        """
        cancelled: list[str] = []
        for name in names:
            record = self._records.get(name)
            if record is not None and record.status is EnvStatus.QUEUED:
                record.status = EnvStatus.CANCELLED
                record.exit_code = exit_code
                record.skipped = skipped
                record.reason = reason
                cancelled.append(name)
        if cancelled:
            self._flush()
        return cancelled

    def propagate(self, todo: dict[str, set[str]]) -> list[tuple[str, str]]:
        """Deterministically cancel queued environments that depend on failed or cancelled ones.

        Propagation is transitive: an environment cancelled because of a dependency also cancels its own successors.

        :param todo: mapping of environment name to the names of its dependencies within the current selection

        :returns: the cancelled ``(name, reason)`` pairs in cancellation order

        """
        cancelled: list[tuple[str, str]] = []
        changed = True
        while changed:
            changed = False
            for name, deps in todo.items():
                record = self._records.get(name)
                if record is None or record.status is not EnvStatus.QUEUED:
                    continue
                blocker = next(
                    (
                        dep
                        for dep in deps
                        if (dep_record := self._records.get(dep)) is not None
                        and dep_record.status in _FAILED_OR_CANCELLED
                    ),
                    None,
                )
                if blocker is None:
                    continue
                blocker_status = self._records[blocker].status.value
                record.status = EnvStatus.CANCELLED
                record.exit_code = -2
                record.skipped = True
                record.reason = f"dependency {blocker} {blocker_status}"
                cancelled.append((name, record.reason))
                changed = True
        if cancelled:
            self._flush()
        return cancelled

    def discard(self) -> None:
        """Remove the persisted state once the session reached its natural end."""
        _unlink(self._path)

    def _require(self, name: str) -> EnvRecord:
        try:
            return self._records[name]
        except KeyError:  # pragma: no cover # every scheduled name is registered via begin()
            msg = f"unknown session environment {name}"
            raise RuntimeError(msg) from None

    def _flush(self) -> None:
        payload: dict[str, JsonValue] = {
            "version": _STATE_VERSION,
            "toxversion": tox_version,
            "envs": {name: record.to_json() for name, record in self._records.items()},
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._path, payload)
        except OSError:
            logger.warning("could not persist session state at %s", self._path, exc_info=True)


def _atomic_write_json(path: Path, payload: dict[str, JsonValue]) -> None:
    descriptor, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handler:
            json.dump(payload, file_handler, ensure_ascii=False)
        Path(tmp_name).replace(path)
    except BaseException:  # never leave the temporary file behind
        _unlink(Path(tmp_name))
        raise


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("could not remove session state %s", path, exc_info=True)


__all__ = ("EnvRecord", "EnvStatus", "SESSION_STATE_FILE", "SessionStatus")
