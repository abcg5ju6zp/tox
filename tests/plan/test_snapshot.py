from __future__ import annotations

from functools import partial
from pathlib import Path

from tox.config.cli.parser import Parsed
from tox.config.main import Config
from tox.config.set_env import SetEnv
from tox.config.source.discover import discover_source
from tox.config.types import Command, EnvList
from tox.plan.files import collect_referenced_files
from tox.plan.snapshot import snapshot_config
from tox.tox_env.python.pip.req_file import PythonDeps


def _make_config(root: Path) -> Config:
    config_file = root / "tox.ini"
    return Config.make(
        Parsed(work_dir=root, override=[], config_file=config_file, root_dir=None),
        pos_args=(),
        source=discover_source(config_file, None),
        extra_envs=(),
    )


def test_config_snapshot_round_trip_with_changed_source(tmp_path: Path) -> None:
    (tmp_path / "tox.ini").write_text(
        "[tox]\nenv_list = py\n[testenv:py]\nset_env = FOO=bar\ncommands = - python -c 'print(\"orig\")'\n",
        encoding="utf-8",
    )
    original = _make_config(tmp_path)
    original_env = original.get_env("py")
    original_env.add_config("commands", list[Command], default=[], desc="commands")
    assert original.core.get("env_list", EnvList).envs == ["py"]
    assert original_env.get("set_env", SetEnv).load("FOO") == "bar"
    assert original_env.get("commands", list[Command])[0].args == ["python", "-c", 'print("orig")']
    assert original_env.get("commands", list[Command])[0].ignore_exit_code is True
    snapshot = snapshot_config(original)

    (tmp_path / "tox.ini").write_text(
        "[tox]\nenv_list = changed\n[testenv:py]\nset_env = FOO=changed\ncommands = python -c 'print(\"changed\")'\n",
        encoding="utf-8",
    )
    replay = _make_config_with_snapshot(tmp_path, snapshot)
    replay_env = replay.get_env("py")
    replay_env.add_config("commands", list[Command], default=[], desc="commands")

    assert replay.core.get("env_list", EnvList).envs == ["py"]
    assert replay_env.get("set_env", SetEnv).load("FOO") == "bar"
    replay_commands = replay_env.get("commands", list[Command])
    assert replay_commands[0].args == ["python", "-c", 'print("orig")']
    assert replay_commands[0].ignore_exit_code is True


def test_config_snapshot_records_sources(tmp_path: Path) -> None:
    (tmp_path / "tox.ini").write_text(
        "[tox]\nenv_list = py\n[testenv:py]\nset_env = FOO=bar\ncommands = python -V\n",
        encoding="utf-8",
    )
    config = _make_config(tmp_path)
    env = config.get_env("py")
    env.add_config("commands", list[Command], default=[], desc="commands")
    env.get("commands", list[Command])
    snapshot = snapshot_config(config)

    env_snapshot = snapshot["envs"]["py"]
    assert isinstance(env_snapshot, dict)
    assert env_snapshot["sources"]["commands"] == {
        "type": "loader",
        "key": "commands",
        "loader": "IniLoader",
        "role": "ToxIni",
        "section": "testenv:py",
        "path": str((tmp_path / "tox.ini").resolve()),
        "overrides": [],
    }


def test_snapshot_collects_set_env_files(tmp_path: Path) -> None:
    (tmp_path / "tox.ini").write_text(
        "[testenv:py]\nset_env = file|.env\ncommands = python -V\n",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\n", encoding="utf-8")
    config = _make_config(tmp_path)
    env = config.get_env("py")
    set_env = env.get("set_env", SetEnv)
    assert set_env.load("FOO") == "bar"

    files = collect_referenced_files(env, config.core.get("tox_root", Path))

    assert files == [
        {
            "role": "set_env:set_env",
            "path": str(env_file.resolve()),
            "relative_path": ".env",
            "sha256": __import__("hashlib").sha256(b"FOO=bar\n").hexdigest(),
            "size": len(b"FOO=bar\n"),
        }
    ]


def test_snapshot_collects_nested_requirement_files(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    constraints = tmp_path / "constraints.txt"
    requirements.write_text("-c constraints.txt\n", encoding="utf-8")
    constraints.write_text("demo==1\n", encoding="utf-8")
    (tmp_path / "tox.ini").write_text("[testenv:py]\ndeps = -r requirements.txt\n", encoding="utf-8")
    config = _make_config(tmp_path)
    env = config.get_env("py")
    env.add_config(
        "deps",
        PythonDeps,
        factory=partial(PythonDeps.factory, tmp_path),
        default=PythonDeps("", tmp_path),
        desc="deps",
    )
    env.get("deps", PythonDeps)

    files = collect_referenced_files(env, tmp_path)

    assert [(path.name, role) for path, role in ((Path(f["path"]), f["role"]) for f in files)] == [
        ("requirements.txt", "requirements:deps"),
        ("constraints.txt", "requirements:deps"),
    ]


def test_snapshot_collects_pylock_files(tmp_path: Path) -> None:
    pylock = tmp_path / "pylock.toml"
    pylock.write_text("[build-system]\n", encoding="utf-8")
    (tmp_path / "tox.ini").write_text("[testenv:py]\npylock = pylock.toml\n", encoding="utf-8")
    config = _make_config(tmp_path)
    env = config.get_env("py")
    env.add_config("pylock", str, default="", desc="pylock")
    env.get("pylock", str)

    files = collect_referenced_files(env, config.core.get("tox_root", Path))

    assert files[0]["role"] == "pylock"
    assert files[0]["relative_path"] == "pylock.toml"


def _make_config_with_snapshot(root: Path, snapshot: dict[str, object]) -> Config:
    config_file = root / "tox.ini"
    return Config.make(
        Parsed(work_dir=root, override=[], config_file=config_file, root_dir=None),
        pos_args=(),
        source=discover_source(config_file, None),
        extra_envs=(),
        plan_snapshot=snapshot,  # type: ignore[arg-type]
    )
