from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tox.plan.execution import assert_execution_in_plan
from tox.report import HandledError

if TYPE_CHECKING:
    from pathlib import Path


def test_execution_guard_accepts_planned_command(tmp_path: Path) -> None:
    plan = {
        "environments": {
            "py": {
                "commands": [
                    {
                        "run_id": "commands[0]",
                        "argv": ["python", "-V"],
                        "cwd": str(tmp_path),
                    },
                ],
            },
        },
    }

    assert_execution_in_plan(plan, "py", "commands[0]", ["python", "-V"], tmp_path)


def test_execution_guard_rejects_command_drift(tmp_path: Path) -> None:
    plan = {
        "environments": {
            "py": {
                "commands": [
                    {
                        "run_id": "commands[0]",
                        "argv": ["python", "-V"],
                        "cwd": str(tmp_path),
                    },
                ],
            },
        },
    }

    with pytest.raises(HandledError, match="differs from execution plan"):
        assert_execution_in_plan(plan, "py", "commands[0]", ["python", "-c", "1"], tmp_path)
