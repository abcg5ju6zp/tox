"""Replay coverage across run modes, packaging modes and positional arguments (TR-7.4, TR-8.2)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tox.plan import load_plan

if TYPE_CHECKING:
    from pathlib import Path

    from tox.pytest import ToxProjectCreator

_SKIP_INI = """
    [tox]
    env_list = a,b
    [testenv]
    package = skip
    commands = python -V
    """


def test_replay_parallel_executes_saved_plan(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({"tox.ini": _SKIP_INI})
    plan_path = tmp_path / "plan.json"
    project.run("p", "--save-plan", str(plan_path)).assert_success()
    plan = load_plan(plan_path)
    assert plan["mode"] == "run-parallel"

    result_json = tmp_path / "result.json"
    result = project.run("p", "--plan", str(plan_path), "--result-json", str(result_json))

    result.assert_success()
    assert "a: OK" in result.out
    assert "b: OK" in result.out
    report = json.loads(result_json.read_text(encoding="utf-8"))
    assert report["plan"]["id"].startswith("sha256:")
    assert set(report["testenvs"]) == {"a", "b"}


def test_replay_rejects_mode_mismatch(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({"tox.ini": _SKIP_INI})
    plan_path = tmp_path / "plan.json"
    project.run("r", "--save-plan", str(plan_path)).assert_success()

    result = project.run("p", "--plan", str(plan_path))

    result.assert_failed(code=-2)
    assert "execution plan mode mismatch" in result.out
    assert not (project.path / ".tox").exists()


def test_replay_round_trips_positional_arguments(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({
        "tox.ini": """
            [tox]
            env_list = py
            [testenv:py]
            package = skip
            commands = python {posargs}
            """
    })
    plan_path = tmp_path / "plan.json"

    project.run("r", "--save-plan", str(plan_path), "--", "-V").assert_success()
    assert load_plan(plan_path)["posargs"] == ["-V"]

    result = project.run("r", "--plan", str(plan_path), "--", "-V")

    result.assert_success()
    assert "python -V" in result.out


def test_replay_rejects_positional_argument_drift(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({
        "tox.ini": """
            [tox]
            env_list = py
            [testenv:py]
            package = skip
            commands = python {posargs}
            """
    })
    plan_path = tmp_path / "plan.json"
    project.run("r", "--save-plan", str(plan_path), "--", "-V").assert_success()

    result = project.run("r", "--plan", str(plan_path), "--", "-c")

    result.assert_failed(code=-2)
    assert "positional arguments differ" in result.out
    assert not (project.path / ".tox").exists()


def test_replay_shared_package_env_builds_wheel(
    tox_project: ToxProjectCreator, tmp_path: Path, demo_pkg_inline: Path
) -> None:
    project = tox_project(
        {
            "tox.ini": """
                [tox]
                env_list = a,b
                [testenv]
                package = wheel
                commands = python -V
                """
        },
        base=demo_pkg_inline,
    )
    plan_path = tmp_path / "plan.json"

    project.run("r", "--save-plan", str(plan_path)).assert_success()
    plan = load_plan(plan_path)
    assert plan["environments"][".pkg"]["kind"] == "package"
    assert plan["environments"]["a"]["packaging"]["package_envs"] == [".pkg"]

    result = project.run("r", "--plan", str(plan_path))

    result.assert_success()
    assert "build_wheel" in result.out
    assert "a: OK" in result.out
    assert "b: OK" in result.out


def test_replay_editable_package(tox_project: ToxProjectCreator, tmp_path: Path, demo_pkg_inline: Path) -> None:
    project = tox_project(
        {
            "tox.ini": """
                [testenv:.pkg]
                set_env = BACKEND_HAS_EDITABLE=1
                [testenv]
                package = editable
                commands = python -V
                """
        },
        base=demo_pkg_inline,
    )
    plan_path = tmp_path / "plan.json"

    project.run("r", "--save-plan", str(plan_path)).assert_success()
    assert load_plan(plan_path)["environments"]["py"]["packaging"]["package"] == "editable"

    result = project.run("r", "--plan", str(plan_path))

    result.assert_success()
    assert "build_editable" in result.out
    assert "py: OK" in result.out


def test_replay_legacy_mode(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({
        "tox.ini": """
            [tox]
            env_list = py
            [testenv:py]
            package = skip
            commands = python -V
            """
    })
    plan_path = tmp_path / "plan.json"

    project.run("legacy", "--save-plan", str(plan_path)).assert_success()
    assert load_plan(plan_path)["mode"] == "legacy"

    result = project.run("legacy", "--plan", str(plan_path))

    result.assert_success()
    assert "py: OK" in result.out


def test_save_plan_rejected_for_non_run_command(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({"tox.ini": _SKIP_INI})
    plan_path = tmp_path / "plan.json"

    result = project.run("c", "--save-plan", str(plan_path))

    result.assert_failed(code=-2)
    assert "only be used with" in result.out
    assert not plan_path.exists()


def test_plan_and_save_plan_are_mutually_exclusive(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({"tox.ini": _SKIP_INI})

    result = project.run("r", "--plan", str(tmp_path / "plan.json"), "--save-plan", str(tmp_path / "other.json"))

    result.assert_failed(code=2)
    assert "not allowed with argument --save-plan" in result.err


def test_result_json_without_plan_has_no_plan_section(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({
        "tox.ini": """
            [tox]
            env_list = py
            [testenv:py]
            package = skip
            commands = python -V
            """
    })
    result_json = tmp_path / "result.json"

    project.run("r", "--result-json", str(result_json)).assert_success()

    report = json.loads(result_json.read_text(encoding="utf-8"))
    assert "plan" not in report
