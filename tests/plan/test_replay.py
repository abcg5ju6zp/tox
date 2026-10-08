from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tox.pytest import ToxProjectCreator


def test_replay_rejects_changed_config_before_environment_setup(
    tox_project: ToxProjectCreator,
    tmp_path: Path,
) -> None:
    original = """
    [tox]
    env_list = py
    [testenv:py]
    package = skip
    commands = python -V
    """
    project = tox_project({"tox.ini": original})
    plan_path = tmp_path / "plan.json"
    project.run("r", "--save-plan", str(plan_path)).assert_success()

    (project.path / "tox.ini").write_text(
        """
        [tox]
        env_list = py
        [testenv:py]
        package = skip
        commands = python -c 'print("changed")'
        """,
        encoding="utf-8",
    )

    result = project.run("r", "--plan", str(plan_path))

    result.assert_failed(code=-2)
    assert "source" in result.out
    assert "changed" in result.out or "sha256" in result.out
    assert not (project.path / ".tox").exists()


def test_replay_executes_saved_plan(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
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
    project.run("r", "--save-plan", str(plan_path)).assert_success()

    result_json = tmp_path / "result.json"
    result = project.run("r", "--plan", str(plan_path), "--result-json", str(result_json))

    result.assert_success()
    assert "replaying execution plan" in result.out
    assert "finished execution plan" in result.out
    assert "commands[0]" in result.out
    assert "py: OK" in result.out
    report = json.loads(result_json.read_text(encoding="utf-8"))
    assert report["plan"]["id"].startswith("sha256:")
    assert report["plan"]["path"] == str(plan_path)
    command_log = next(path for path in (project.path / ".tox" / "py" / "log").iterdir() if "commands[0]" in path.name)
    assert f"plan_id: {report['plan']['id']}" in command_log.read_text(encoding="utf-8")
    assert "plan_schema_version: 1" in command_log.read_text(encoding="utf-8")


def test_replay_rejects_extra_execution_arguments(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
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
    project.run("r", "--save-plan", str(plan_path)).assert_success()

    result = project.run("r", "--plan", str(plan_path), "-e", "py")

    result.assert_failed(code=-2)
    assert "execution-affecting argument" in result.out


def test_replay_rejects_changed_installpkg(tox_project: ToxProjectCreator, tmp_path: Path) -> None:
    package = tmp_path / "demo.whl"
    package.write_bytes(b"wheel")
    project = tox_project({
        "tox.ini": """
            [testenv:py]
            commands = python -V
            """
    })
    plan_path = tmp_path / "plan.json"
    project.run("r", "--installpkg", str(package), "--save-plan", str(plan_path)).assert_success()
    package.write_bytes(b"changed")

    result = project.run("r", "--plan", str(plan_path))

    result.assert_failed(code=-2)
    assert "install package" in result.out
    assert not (project.path / ".tox").exists()


def test_replay_rejects_changed_secret_environment(
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
    project.run("r", "--save-plan", str(plan_path)).assert_success()

    monkeypatch.delenv("API_TOKEN")
    result = project.run("r", "--plan", str(plan_path))

    result.assert_failed(code=-2)
    assert "secret environment variable API_TOKEN changed or is missing" in result.out


def test_replay_restores_inline_config_secret_from_verified_source(
    tox_project: ToxProjectCreator,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "inline-super-secret"  # ruff: ignore[hardcoded-password-string]
    monkeypatch.delenv("API_TOKEN", raising=False)
    project = tox_project({
        "tox.ini": f"""
            [tox]
            env_list = py
            [testenv:py]
            package = skip
            set_env = API_TOKEN={secret}
            commands = python -V
            """
    })
    plan_path = tmp_path / "plan.json"

    project.run("r", "--save-plan", str(plan_path)).assert_success()
    assert secret not in plan_path.read_text(encoding="utf-8")

    result = project.run("r", "--plan", str(plan_path))

    result.assert_success()
    assert "py: OK" in result.out


def test_replay_restores_toml_config_secret_from_verified_source(
    tox_project: ToxProjectCreator,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "toml-super-secret"  # ruff: ignore[hardcoded-password-string]
    monkeypatch.delenv("API_TOKEN", raising=False)
    project = tox_project({
        "pyproject.toml": f"""
            [tool.tox]
            env_list = ["py"]

            [tool.tox.env.py]
            package = "skip"
            set_env = {{ API_TOKEN = "{secret}" }}
            commands = [["python", "-V"]]
            """
    })
    plan_path = tmp_path / "plan.json"

    project.run("r", "--save-plan", str(plan_path)).assert_success()
    assert secret not in plan_path.read_text(encoding="utf-8")

    result = project.run("r", "--plan", str(plan_path))

    result.assert_success()
    assert "py: OK" in result.out


def test_replay_restores_env_file_secret_from_verified_source(
    tox_project: ToxProjectCreator,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "file-super-secret"  # ruff: ignore[hardcoded-password-string]
    monkeypatch.delenv("API_TOKEN", raising=False)
    project = tox_project({
        "tox.ini": """
            [tox]
            env_list = py
            [testenv:py]
            package = skip
            set_env = file|.env
            commands = python -V
            """,
        ".env": f"API_TOKEN={secret}\n",
    })
    plan_path = tmp_path / "plan.json"

    project.run("r", "--save-plan", str(plan_path)).assert_success()
    raw_plan = plan_path.read_text(encoding="utf-8")
    assert secret not in raw_plan

    result = project.run("r", "--plan", str(plan_path))

    result.assert_success()
    assert "py: OK" in result.out

    # drift in the hash-pinned env file is rejected before environments are set up again
    shutil.rmtree(project.path / ".tox")
    (project.path / ".env").write_text("API_TOKEN=changed-secret\n", encoding="utf-8")
    drifted = project.run("r", "--plan", str(plan_path))
    drifted.assert_failed(code=-2)
    assert ".env changed" in drifted.out
    assert not (project.path / ".tox").exists()
