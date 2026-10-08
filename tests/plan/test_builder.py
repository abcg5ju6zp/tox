from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from tox.plan import load_plan

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tox.pytest import ToxProjectCreator


def test_save_plan_freezes_run_without_creating_environment(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
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

    result = project.run("r", "--save-plan", str(plan_path))

    result.assert_success()
    assert plan_path.exists()
    assert not (project.path / ".tox").exists()
    plan = load_plan(plan_path)
    assert plan["mode"] == "run"
    assert plan["selection"]["run_envs"] == ["py"]
    assert plan["graph"] == {"order": ["py"], "dependencies": {"py": []}}
    assert plan["environments"]["py"]["commands"] == [
        {
            "run_id": "commands[0]",
            "phase": "commands",
            "index": 0,
            "argv": ["python", "-V"],
            "shell": "python -V",
            "ignore_exit_code": False,
            "invert_exit_code": False,
            "cwd": str(project.path),
            "source": {
                "type": "loader",
                "key": "commands",
                "loader": "IniLoader",
                "role": "ToxIni",
                "section": "testenv:py",
                "path": str((project.path / "tox.ini").resolve()),
                "overrides": [],
            },
        },
    ]
    assert plan["environments"]["py"]["packaging"] == {"package": "skip", "use_develop": False}
    assert plan["environments"]["py"]["interpreter"]["available"] is True
    assert plan["sources"][0]["role"] == "tox-config"
    assert plan["sources"][0]["sha256"]
    assert isinstance(plan["plugins"]["tox_version"], str)
    assert any(item["role"] == "internal" for item in plan["plugins"]["plugins"])
    assert "python -V" in json.dumps(plan)
    assert "saved execution plan" in result.out


def test_save_plan_freezes_dependency_graph_without_environment_setup(
    tox_project: ToxProjectCreator, tmp_path: Path
) -> None:
    project = tox_project({
        "tox.ini": """
            [tox]
            env_list = a,b
            [testenv]
            package = skip
            [testenv:b]
            depends = a
            commands = python -V
            """
    })
    plan_path = tmp_path / "plan.json"

    result = project.run("r", "--save-plan", str(plan_path))

    result.assert_success()
    assert not (project.path / ".tox").exists()
    plan = load_plan(plan_path)
    assert plan["selection"]["run_envs"] == ["a", "b"]
    assert plan["graph"]["dependencies"] == {"a": [], "b": ["a"]}
    assert plan["graph"]["order"].index("a") < plan["graph"]["order"].index("b")


def test_save_plan_freezes_editable_package_environment(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({
        "setup.py": "",
        "tox.ini": """
            [testenv:py]
            package = editable
            commands = python -V
            """,
    })
    plan_path = tmp_path / "plan.json"

    result = project.run("r", "--save-plan", str(plan_path))

    result.assert_success()
    assert not (project.path / ".tox").exists()
    plan = load_plan(plan_path)
    assert plan["environments"]["py"]["kind"] == "run"
    assert plan["environments"]["py"]["packaging"]["package"] == "editable"
    assert plan["environments"]["py"]["packaging"]["package_envs"] == [".pkg"]
    assert plan["environments"][".pkg"]["kind"] == "package"


def test_save_plan_freezes_factor_selection(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({
        "tox.ini": """
            [tox]
            env_list = py{314,39}-{a,b}
            [testenv]
            package = skip
            """
    })
    plan_path = tmp_path / "plan.json"

    result = project.run("r", "-f", "py314", "--save-plan", str(plan_path))

    result.assert_success()
    plan = load_plan(plan_path)
    assert plan["selection"]["run_envs"] == ["py314-a", "py314-b"]
    assert plan["selection"]["factors"] == [["py314"]]


def test_save_plan_refuses_when_provision_requirements_missing(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    project = tox_project({
        "tox.ini": """
            [tox]
            requires = this-distribution-does-not-exist==1.0
            """
    })
    plan_path = tmp_path / "plan.json"

    result = project.run("r", "--save-plan", str(plan_path))

    result.assert_failed(code=-2)
    assert "cannot save a complete execution plan" in result.out
    assert not plan_path.exists()
    assert not (project.path / ".tox").exists()


def test_save_plan_redacts_secret_environment_values(
    tox_project: ToxProjectCreator,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "super-secret-token"  # ruff: ignore[hardcoded-password-string]
    monkeypatch.setenv("API_TOKEN", secret)
    project = tox_project({
        "tox.ini": """
            [testenv:py]
            package = skip
            commands = curl -H {env:API_TOKEN}
            """
    })
    plan_path = tmp_path / "plan.json"

    result = project.run("r", "--save-plan", str(plan_path))

    result.assert_success()
    raw_plan = plan_path.read_text(encoding="utf-8")
    assert secret not in raw_plan
    plan = load_plan(plan_path)
    command = plan["environments"]["py"]["commands"][0]
    secret_argument = str(command["argv"][2])
    assert secret_argument.startswith("__TOX_PLAN_SECRET_environment:API_TOKEN:")
    token_bindings = [item for item in plan["secrets"] if item["name"] == "API_TOKEN"]
    assert token_bindings
    assert all(item["sha256"] == hashlib.sha256(secret.encode()).hexdigest() for item in token_bindings)


def test_save_plan_hashes_external_installpkg(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    package = tmp_path / "demo.whl"
    package.write_bytes(b"wheel")
    project = tox_project({
        "tox.ini": """
            [testenv:py]
            commands = python -V
            """
    })
    plan_path = tmp_path / "plan.json"

    result = project.run("r", "--installpkg", str(package), "--save-plan", str(plan_path))

    result.assert_success()
    plan = load_plan(plan_path)
    assert plan["installpkg"] == {
        "path": str(package.resolve()),
        "sha256": hashlib.sha256(b"wheel").hexdigest(),
        "size": 5,
    }
    assert plan["environments"]["py"]["packaging"]["package"] == "external"
