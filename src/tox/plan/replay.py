from __future__ import annotations

import configparser
import hashlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .identity import plan_digest
from .plugins import capture_plugins
from .secrets import bind_plan_secrets
from .store import load_plan

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tox.config.cli.parser import Parsed
    from tox.util.json_types import JsonValue

_COMMAND_MODES = {"r": "run", "p": "run-parallel"}
_ENVIRONMENT_ORIGIN = "environment"
_FILE_ORIGINS = {"env-file", "config"}


def load_and_verify_replay(
    parsed: Parsed,
    argv: Sequence[str],
    posargs: Sequence[str] | None = None,
) -> tuple[dict[str, JsonValue], str]:
    if parsed.plan is None:
        msg = "execution plan path is required"
        raise ValueError(msg)
    plan = load_plan(parsed.plan)
    identity = str(plan_digest(plan))
    _validate_argv(list(argv), plan, list(posargs or ()))
    _validate_mode(parsed, plan)
    # Environment-sourced secrets need no disk reads, so bind them first: source/interpreter paths inside the plan may
    # carry their placeholders and must be substituted before hashing.
    environment_values, environment_differences = _secret_values(plan, {_ENVIRONMENT_ORIGIN})
    verified_plan = bind_plan_secrets(plan, environment_values)
    if not isinstance(verified_plan, dict):
        msg = "bound execution plan must be a JSON object"
        raise TypeError(msg)
    differences = [
        *environment_differences,
        *_source_differences(verified_plan),
        *_interpreter_differences(verified_plan),
        *_installpkg_differences(verified_plan),
        *_plugin_differences(verified_plan),
    ]
    if differences:
        msg = "execution plan is incompatible with the current runtime:\n" + "\n".join(
            f"- {item}" for item in differences
        )
        raise ValueError(msg)
    # Every recorded source now has a matching digest; only here is it safe to read secrets stored in env files or in
    # the verified configuration itself.
    file_values, file_differences = _file_secret_values(plan)
    if file_differences:
        msg = "execution plan is incompatible with the current runtime:\n" + "\n".join(
            f"- {item}" for item in file_differences
        )
        raise ValueError(msg)
    bound_plan = bind_plan_secrets(plan, {**environment_values, **file_values})
    if not isinstance(bound_plan, dict):
        msg = "bound execution plan must be a JSON object"
        raise TypeError(msg)
    return cast("dict[str, JsonValue]", bound_plan), identity


def replay_selection(plan: dict[str, JsonValue]) -> list[str]:
    selection = plan.get("selection")
    if not isinstance(selection, dict) or not isinstance(selection.get("run_envs"), list):
        msg = "execution plan has no valid run environment selection"
        raise TypeError(msg)
    return [str(name) for name in selection["run_envs"]]


def _validate_argv(argv: list[str], plan: dict[str, JsonValue], posargs: list[str]) -> None:
    # ``argv`` reaches us after argparse preprocessing, so the ``--`` separator and positional arguments have
    # already been split off and arrive separately as ``posargs``.
    arguments = argv[1:] if argv and argv[0] in {"r", "run", "p", "run-parallel", "legacy"} else argv
    permitted_values = {"--plan", "--result-json", "--colored", "--stderr-color"}
    verbosity_flag = re.compile(r"-[vq]+")
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":  # defensive: a direct caller may still pass the un-split form
            msg = "execution replay received an unexpected positional argument separator"
            raise ValueError(msg)
        if argument in permitted_values:
            index += 2
            continue
        if verbosity_flag.fullmatch(argument):
            index += 1
            continue
        if argument.startswith(("--plan=", "--result-json=", "--colored=", "--stderr-color=")):
            index += 1
            continue
        msg = f"execution replay does not allow execution-affecting argument {argument!r}"
        raise ValueError(msg)
    expected_posargs = plan.get("posargs", [])
    if not isinstance(expected_posargs, list) or [str(arg) for arg in expected_posargs] != [
        str(arg) for arg in posargs
    ]:
        msg = "execution replay positional arguments differ from the execution plan"
        raise ValueError(msg)


def _validate_mode(parsed: Parsed, plan: dict[str, JsonValue]) -> None:
    current = _COMMAND_MODES.get(parsed.command, parsed.command)
    expected = str(plan.get("mode"))
    if current != expected:
        msg = f"execution plan mode mismatch: plan={expected}, current={current}"
        raise ValueError(msg)


def _source_differences(plan: dict[str, JsonValue]) -> list[str]:
    sources = plan.get("sources")
    if not isinstance(sources, list):
        return ["execution plan has no valid source inventory"]
    differences = []
    for item in sources:
        if not isinstance(item, dict):
            differences.append("source inventory entry is not an object")
            continue
        path = Path(str(item["path"]))
        actual = _sha256(path)
        expected = str(item.get("sha256"))
        if actual != expected:
            differences.append(f"source {path} changed: expected sha256={expected}, found {actual or 'missing'}")
    return differences


def _interpreter_differences(plan: dict[str, JsonValue]) -> list[str]:
    environments = plan.get("environments")
    if not isinstance(environments, dict):
        return ["execution plan has no valid environments"]
    differences = []
    for name, item in environments.items():
        if not isinstance(item, dict) or not isinstance(item.get("interpreter"), dict):
            continue
        interpreter = item["interpreter"]
        if interpreter.get("available") is not True:
            continue
        path = Path(str(interpreter["executable"]))
        actual = _sha256(path)
        expected = str(interpreter.get("sha256"))
        if actual != expected:
            differences.append(
                f"interpreter for {name} changed: expected sha256={expected}, found {actual or 'missing'}"
            )
    return differences


def _installpkg_differences(plan: dict[str, JsonValue]) -> list[str]:
    installpkg = plan.get("installpkg")
    if not isinstance(installpkg, dict):
        return []
    path = Path(str(installpkg["path"]))
    actual = _sha256(path)
    expected = str(installpkg.get("sha256"))
    if actual == expected:
        return []
    return [f"install package {path} changed: expected sha256={expected}, found {actual or 'missing'}"]


def _plugin_differences(plan: dict[str, JsonValue]) -> list[str]:
    expected_plugin_data = plan.get("plugins")
    if not isinstance(expected_plugin_data, dict):
        return ["execution plan has no plugin inventory"]
    current_plugin_data = capture_plugins()
    differences = []
    if current_plugin_data.get("tox_version") != expected_plugin_data.get("tox_version") and not _is_required(
        "tox", expected_plugin_data
    ):
        differences.append(
            f"tox version changed: expected {expected_plugin_data.get('tox_version')}, "
            f"found {current_plugin_data.get('tox_version')}"
        )
    expected_plugins = _plugin_index(expected_plugin_data)
    current_plugins = _plugin_index(current_plugin_data)
    for module, expected in expected_plugins.items():
        if expected.get("role") == "internal":
            continue
        current = current_plugins.get(module)
        if current is None:
            if expected.get("role") == "external" and _is_required(
                str(expected.get("distribution")), expected_plugin_data
            ):
                continue
            differences.append(f"plugin {module} is missing")
            continue
        differences.extend(_present_plugin_differences(module, expected, current))
    for module, current in current_plugins.items():
        if current.get("role") == "external" and module not in expected_plugins:
            differences.append(f"unexpected plugin {module} is loaded")
    return differences


def _present_plugin_differences(
    module: str,
    expected: dict[str, JsonValue],
    current: dict[str, JsonValue],
) -> list[str]:
    differences = []
    if current.get("sha256") != expected.get("sha256"):
        differences.append(f"plugin {module} content changed")
    if current.get("hooks") != expected.get("hooks"):
        differences.append(f"plugin {module} hooks changed")
    return differences


def _plugin_index(data: JsonValue) -> dict[str, dict[str, JsonValue]]:
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        return {}
    return {
        str(item["module"]): item
        for item in data["plugins"]
        if isinstance(item, dict) and isinstance(item.get("module"), str)
    }


def _is_required(distribution: str | None, plugin_data: dict[str, JsonValue]) -> bool:
    requires = plugin_data.get("requires")
    if distribution is None or not isinstance(requires, list):
        return False
    return any(str(requirement).split("==", 1)[0].strip() == distribution for requirement in requires)


def _secret_values(plan: dict[str, JsonValue], origins: set[str]) -> tuple[dict[str, str], list[str]]:
    secrets = plan.get("secrets")
    if not isinstance(secrets, list):
        return {}, []
    values: dict[str, str] = {}
    differences: list[str] = []
    for item in secrets:
        if not isinstance(item, dict):
            continue
        origin = str(item.get("origin", _ENVIRONMENT_ORIGIN))
        if origin not in origins:
            continue
        identifier = str(item["id"])
        name = str(item["name"])
        value = os.environ.get(name)
        actual = hashlib.sha256(value.encode()).hexdigest() if value is not None else None
        if value is None or actual != item.get("sha256"):
            differences.append(f"secret environment variable {name} changed or is missing")
            continue
        values[identifier] = value
    return values, differences


def _file_secret_values(plan: dict[str, JsonValue]) -> tuple[dict[str, str], list[str]]:
    secrets = plan.get("secrets")
    if not isinstance(secrets, list):
        return {}, []
    values: dict[str, str] = {}
    differences: list[str] = []
    for item in secrets:
        if not isinstance(item, dict):
            continue
        origin = str(item.get("origin"))
        if origin not in _FILE_ORIGINS:
            continue
        identifier = str(item["id"])
        name = str(item["name"])
        file = item.get("file")
        if not isinstance(file, dict):
            differences.append(f"secret {name} has no verifiable source file")
            continue
        path = Path(str(file["path"]))
        if origin == "env-file":
            value = _read_env_file_secret(path, name)
        else:
            value = _read_config_secret(path, name, str(item.get("env", "")))
        if value is None or hashlib.sha256(value.encode()).hexdigest() != item.get("sha256"):
            differences.append(f"secret {name} from {path} changed or is missing")
            continue
        values[identifier] = value
    return values, differences


def _read_env_file_secret(path: Path, name: str) -> str | None:
    from tox.config.set_env import SetEnv  # ruff:ignore[import-outside-top-level]
    from tox.tox_env.errors import Fail  # ruff:ignore[import-outside-top-level]

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for env_line in lines:
        env_line = env_line.strip()  # ruff:ignore[redefined-loop-name]
        if not env_line or env_line.startswith("#"):
            continue
        try:
            key, value = SetEnv._extract_key_value(env_line)  # ruff:ignore[private-member-access]
        except Fail:
            continue
        if key == name:
            return value
    return None


def _read_config_secret(path: Path, name: str, env_name: str) -> str | None:
    if path.suffix == ".toml":
        return _read_toml_config_secret(path, name, env_name)
    return _read_ini_config_secret(path, name, env_name)


def _read_ini_config_secret(path: Path, name: str, env_name: str) -> str | None:
    from tox.config.set_env import SetEnv  # ruff:ignore[import-outside-top-level]

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as file_handler:
            parser.read_file(file_handler)
    except (OSError, configparser.Error):
        return None
    set_env = SetEnv("", "set_env", env_name or None, path.parent)
    if parser.has_section("testenv") and parser.has_option("testenv", "set_env"):
        set_env = SetEnv(parser.get("testenv", "set_env"), "set_env", env_name or None, path.parent)
    section = f"testenv:{env_name}" if env_name else "testenv"
    if env_name and parser.has_section(section) and parser.has_option(section, "set_env"):
        set_env.extend(SetEnv(parser.get(section, "set_env"), "set_env", env_name, path.parent))
    return set_env.load(name) if name in set_env else None


def _read_toml_config_secret(path: Path, name: str, env_name: str) -> str | None:
    import tomllib  # ruff:ignore[import-outside-top-level]

    try:
        with path.open("rb") as file_handler:
            data = tomllib.load(file_handler)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    env = data.get("tool", {}).get("tox", {}).get("env", {}).get(env_name or "py", {})
    set_env = env.get("set_env") if isinstance(env, dict) else None
    if not isinstance(set_env, dict) or name not in set_env:
        return None
    entry = set_env[name]
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("value"), str):
        return str(entry["value"])
    return None


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
