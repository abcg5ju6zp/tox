"""Inline plugin identity is part of the plan and drift is rejected before execution (TR-6.1)."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from tox.plan import load_plan

if TYPE_CHECKING:
    from pathlib import Path

    from tox.pytest import ToxProjectCreator
    from tox.tox_env.api import ToxEnv

pytestmark = pytest.mark.plugin_test

_INI = """
[tox]
env_list = py
[testenv:py]
package = skip
commands = python -V
"""


def _plugin() -> None:  # pragma: no cover # the source is copied into a python file
    from tox.plugin import impl  # ruff:ignore[import-outside-top-level]

    @impl
    def tox_before_run_commands(tox_env: ToxEnv) -> None:  # ruff:ignore[unused-function-argument]
        return None


_HOOK_CHANGED_TOXFILE = """
from tox.plugin import impl

@impl
def tox_after_run_commands(tox_env, exit_code, outcomes) -> None:
    return None
"""


def test_plan_records_inline_plugin_identity(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({"tox.ini": _INI, "toxfile.py": _plugin})
    plan_path = tmp_path / "plan.json"

    project.run("r", "--save-plan", str(plan_path)).assert_success()

    plugins = load_plan(plan_path)["plugins"]["plugins"]
    inline = [item for item in plugins if item.get("role") == "inline"]
    assert len(inline) == 1
    assert inline[0]["sha256"]
    assert "tox_before_run_commands" in inline[0]["hooks"]


def test_replay_rejects_changed_inline_plugin(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({"tox.ini": _INI, "toxfile.py": _plugin})
    plan_path = tmp_path / "plan.json"
    project.run("r", "--save-plan", str(plan_path)).assert_success()

    project.run("r", "--plan", str(plan_path)).assert_success()

    shutil.rmtree(project.path / ".tox")
    tox_file = project.path / "toxfile.py"
    tox_file.write_text(tox_file.read_text(encoding="utf-8") + "\n# drifted\n", encoding="utf-8")

    result = project.run("r", "--plan", str(plan_path))

    result.assert_failed(code=-2)
    assert "content changed" in result.out
    assert not (project.path / ".tox").exists()


def test_replay_rejects_inline_plugin_hook_change(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({"tox.ini": _INI, "toxfile.py": _plugin})
    plan_path = tmp_path / "plan.json"
    project.run("r", "--save-plan", str(plan_path)).assert_success()

    (project.path / "toxfile.py").write_text(_HOOK_CHANGED_TOXFILE, encoding="utf-8")

    result = project.run("r", "--plan", str(plan_path))

    result.assert_failed(code=-2)
    assert "hooks changed" in result.out
