"""The provision subprocess must re-enter tox with the same execution plan (TR-6.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from packaging.requirements import Requirement

import tox.provision as provision_module

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

    from tox.execute.request import ExecuteRequest
    from tox.pytest import ToxProjectCreator


def test_provision_subprocess_receives_same_plan(
    tox_project: ToxProjectCreator, tmp_path: Path, mocker: MockerFixture
) -> None:
    # ``packaging`` is importable while saving, so the complete plan can be generated; during replay we pretend the
    # declared requirement is missing, driving the approved provision path without a real second tox install.
    project = tox_project({
        "tox.ini": """
            [tox]
            requires = packaging
            [testenv:py]
            package = skip
            commands = python -V
            """
    })
    plan_path = tmp_path / "plan.json"
    project.run("r", "--save-plan", str(plan_path)).assert_success()

    captured_cmd: list[str] | None = None

    def handle_provision(request: ExecuteRequest) -> int:
        nonlocal captured_cmd
        if "provision" in str(request.run_id):
            captured_cmd = [str(part) for part in request.cmd]
        return 0

    project.patch_execute(handle_provision)
    mocker.patch.object(provision_module, "_get_missing", return_value=[(Requirement("packaging"), None)])

    outcome = project.run("r", "--plan", str(plan_path))

    outcome.assert_success()
    assert captured_cmd is not None, "provision subprocess was not invoked"
    assert "--plan" in captured_cmd
    assert str(plan_path) in captured_cmd
