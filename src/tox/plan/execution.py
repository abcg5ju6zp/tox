from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tox.report import HandledError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tox.util.json_types import JsonValue

_GUARDED_PREFIXES = ("commands_pre[", "commands[", "commands_post[", "extra_setup_commands[", "recreate_commands[")


def assert_execution_in_plan(
    plan: dict[str, JsonValue] | None,
    environment: str,
    run_id: str,
    argv: Sequence[str | Path],
    cwd: Path,
) -> None:
    if plan is None or not run_id.startswith(_GUARDED_PREFIXES):
        return
    environments = plan.get("environments")
    if not isinstance(environments, dict):
        msg = "execution plan has no environment commands inventory"
        raise HandledError(msg)
    env_plan = environments.get(environment)
    if not isinstance(env_plan, dict) or not isinstance(env_plan.get("commands"), list):
        msg = f"execution plan does not contain commands for environment {environment}"
        raise HandledError(msg)
    command_plan = next(
        (item for item in env_plan["commands"] if isinstance(item, dict) and item.get("run_id") == run_id),
        None,
    )
    if command_plan is None:
        msg = f"execution plan does not contain {run_id} for environment {environment}"
        raise HandledError(msg)
    actual_argv = [str(argument) for argument in argv]
    if actual_argv != [str(argument) for argument in command_plan.get("argv", [])]:
        msg = f"command {run_id} for environment {environment} differs from execution plan"
        raise HandledError(msg)
    if command_plan.get("cwd") is not None and Path(str(command_plan["cwd"])).resolve() != cwd.resolve():
        msg = f"command {run_id} for environment {environment} uses a cwd outside the execution plan"
        raise HandledError(msg)
