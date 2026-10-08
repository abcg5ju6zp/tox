from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

from python_discovery import get_interpreter

from tox.config.set_env import SetEnv
from tox.session.cmd.run.common import run_order
from tox.tox_env.errors import Fail
from tox.tox_env.python.virtual_env.api import VirtualEnv
from tox.tox_env.runner import RunToxEnv
from tox.util.redact import redact_argv

from .files import collect_referenced_files, hash_file
from .plugins import capture_plugins
from .secrets import (
    SecretBinding,
    is_secret_name,
    iter_secret_placeholders,
    redact_plan,
    secret_binding,
)
from .snapshot import snapshot_config

if TYPE_CHECKING:
    from tox.config.sets import EnvConfigSet
    from tox.session.state import State
    from tox.tox_env.package import PackageToxEnv
    from tox.util.json_types import JsonValue

_COMMAND_MODES = {"r": "run", "p": "run-parallel"}
_EXECUTION_OPTION_KEYS = (
    "no_test",
    "package_only",
    "fail_fast",
    "develop",
    "no_recreate_pkg",
    "skip_pkg_install",
    "skip_env_install",
    "recreate",
    "hash_seed",
    "discover",
    "parallel",
    "parallel_live",
    "parallel_no_spinner",
    "pre",
    "force_dep",
    "site_packages",
    "always_copy",
    "skip_missing_interpreters",
    "no_capture",
)

_COMMAND_KEYS = (
    "commands_pre",
    "commands",
    "commands_post",
    "extra_setup_commands",
    "recreate_commands",
)


def build_plan(state: State) -> dict[str, JsonValue]:
    options = state.conf.options
    active_run_envs = list(state.envs.iter())
    all_envs = _selected_envs_with_packages(state, active_run_envs)
    active_set = set(active_run_envs)
    order, dependencies = run_order(state, active_run_envs)
    source_files, config_snapshot = _collect_config_and_sources(state, all_envs)
    requires = state.conf.core.get("requires", list) if "requires" in state.conf.core else []
    plugins = capture_plugins([str(requirement) for requirement in requires])
    core_snapshot = cast("dict[str, JsonValue]", config_snapshot["core"])
    core_values = cast("dict[str, JsonValue]", core_snapshot["values"])
    environment_snapshots = config_snapshot.get("envs", {})
    environments = {}
    for name in all_envs:
        env_snapshot = environment_snapshots.get(name, {}) if isinstance(environment_snapshots, dict) else {}
        env_sources = env_snapshot.get("sources", {}) if isinstance(env_snapshot, dict) else {}
        environments[name] = _environment_plan(
            state.envs[name],
            active=name in active_set,
            sources=cast("dict[str, JsonValue]", env_sources),
        )
    plan: dict[str, JsonValue] = {
        "mode": _COMMAND_MODES.get(options.command, options.command),
        "argv": redact_argv([str(argument) for argument in state.args]),
        "posargs": [str(arg) for arg in (state._options.pos_args or ())],  # ruff:ignore[private-member-access]
        "root": str(state.conf.core.get("tox_root", Path)),
        "work_dir": str(state.conf.core.get("work_dir", Path)),
        "selection": {
            "run_envs": active_run_envs,
            "all_envs": all_envs,
            "unavailable": state.envs.unavailable_envs(),
            "env_list": core_values.get("env_list", []),
            "labels": options.labels,
            "factors": options.factors,
            "skip_env": options.skip_env,
        },
        "options": {
            key: _option_json(getattr(options, key)) for key in _EXECUTION_OPTION_KEYS if hasattr(options, key)
        },
        "graph": {"order": order, "dependencies": {key: sorted(value) for key, value in dependencies.items()}},
        "config": config_snapshot,
        "environments": environments,
        "sources": source_files,
        "plugins": plugins,
        "installpkg": _installpkg_plan(options),
    }
    _redact_secrets(state, all_envs, plan)
    return plan


def _option_json(value: object) -> JsonValue:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_option_json(item) if isinstance(item, (Path, list)) else _scalar_json(item) for item in value]
    return _scalar_json(value)


def _scalar_json(value: object) -> JsonValue:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return cast("JsonValue", value)
    # CLI options may carry parsed domain objects (e.g. packaging ``Requirement``); persist their canonical form.
    return str(value)


def _selected_envs_with_packages(state: State, run_envs: list[str]) -> list[str]:
    selected = list(run_envs)
    for name in run_envs:
        env = state.envs[name]
        if not isinstance(env, RunToxEnv) or env.package_env is None:
            continue
        for package_env in env.package_envs:
            if package_env.name not in selected:
                selected.append(package_env.name)
    return selected


def _redact_secrets(state: State, env_names: list[str], plan: dict[str, JsonValue]) -> None:
    bindings: dict[str, SecretBinding] = {}
    runtime_identifiers: set[str] = set()
    root = state.conf.core.get("tox_root", Path)
    config_path = state.conf.src_path.resolve()

    def add_binding(binding: SecretBinding, *, runtime: bool = False) -> None:
        if not binding.value or not is_secret_name(binding.name):
            return
        bindings[binding.identifier] = binding
        if runtime:
            runtime_identifiers.add(binding.identifier)

    for env_name in env_names:
        env = state.envs[env_name]
        environment_variables = env.environment_variables
        set_env = env.conf.get("set_env", SetEnv) if "set_env" in env.conf else None
        file_values = _set_env_file_values(set_env)
        for name, value in environment_variables.items():
            if set_env is not None and name in set_env:
                source = f"set_env:{env_name}"
                add_binding(
                    _set_env_secret_binding(source, env_name, name, value, root, config_path, file_values),
                    runtime=True,
                )
            else:
                add_binding(
                    secret_binding(f"environment:{env_name}", name, value, env_name=env_name),
                    runtime=True,
                )
    for name, value in os.environ.items():
        add_binding(secret_binding("environment", name, value))

    redact_plan(plan, bindings)
    used_identifiers = set(iter_secret_placeholders(plan)) | runtime_identifiers
    plan["secrets"] = [bindings[identifier].metadata() for identifier in sorted(used_identifiers)]


def _set_env_secret_binding(  # ruff:ignore[too-many-arguments]
    source: str,
    env_name: str,
    name: str,
    value: str,
    root: Path,
    config_path: Path,
    file_values: dict[str, tuple[Path, str]],
) -> SecretBinding:
    file_match = file_values.get(name)
    if file_match is not None:
        path, raw_value = file_match
        if raw_value == value:
            relative_path = _relative_path(path, root)
            return secret_binding(
                source,
                name,
                value,
                origin="env-file",
                env_name=env_name,
                file_path=str(path),
                relative_path=relative_path,
            )
    return secret_binding(
        source,
        name,
        value,
        origin="config",
        env_name=env_name,
        file_path=str(config_path),
        relative_path=_relative_path(config_path, root),
    )


def _relative_path(path: Path, root: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return None


def _set_env_file_values(set_env: SetEnv | None) -> dict[str, tuple[Path, str]]:
    """Map env-file keys to ``(path, raw value)`` using the same line parser as :class:`SetEnv`."""
    if set_env is None:
        return {}
    _ = list(set_env)  # force ``file|`` resolution so ``plan_file_paths`` is populated
    result: dict[str, tuple[Path, str]] = {}
    for path in set_env.plan_file_paths:
        for env_line in path.read_text(encoding="utf-8").splitlines():
            env_line = env_line.strip()  # ruff:ignore[redefined-loop-name]
            if not env_line or env_line.startswith("#"):
                continue
            try:
                key, raw_value = SetEnv._extract_key_value(env_line)  # ruff:ignore[private-member-access]
            except Fail:  # unparsable lines cannot carry a proven file value
                continue
            result.setdefault(key, (path, raw_value))
    return result


def _collect_config_and_sources(
    state: State,
    env_names: list[str],
) -> tuple[list[dict[str, JsonValue]], dict[str, JsonValue]]:
    root = state.conf.core.get("tox_root", Path)
    source_path = state.conf.src_path.resolve()
    files: dict[Path, dict[str, JsonValue]] = {source_path: hash_file(source_path, "tox-config", root=root)}
    for name in env_names:
        for item in collect_referenced_files(state.envs[name].conf, root):
            files[Path(str(item["path"]))] = item
    return list(files.values()), snapshot_config(state.conf)


def _environment_plan(
    env: RunToxEnv | PackageToxEnv,
    *,
    active: bool,
    sources: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    plan: dict[str, JsonValue] = {
        "name": env.conf.name,
        "type": type(env).__name__,
        "runner_id": env.id(),
        "kind": "run" if isinstance(env, RunToxEnv) else "package",
        "active": active,
        "commands": _commands_plan(env.conf, sources),
        "packaging": _packaging_plan(env),
    }
    if isinstance(env, VirtualEnv):
        plan["interpreter"] = _interpreter_plan(env)
    return plan


def _commands_plan(conf: EnvConfigSet, sources: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    result = []
    for key in _COMMAND_KEYS:
        if key not in conf:
            continue
        commands = conf.get(key, list)
        cwd = str(conf.get("change_dir", Path)) if "change_dir" in conf else None
        for index, command in enumerate(commands):
            result.append(
                {
                    "run_id": f"{key}[{index}]",
                    "phase": key,
                    "index": index,
                    "argv": list(command.args),
                    "shell": command.shell,
                    "ignore_exit_code": command.ignore_exit_code,
                    "invert_exit_code": command.invert_exit_code,
                    "cwd": cwd,
                    "source": sources.get(key),
                },
            )
    return result


def _packaging_plan(env: RunToxEnv | PackageToxEnv) -> dict[str, JsonValue]:
    conf = env.conf
    packaging: dict[str, JsonValue] = {}
    for key in ("package", "package_env", "package_tox_env_type", "package_root", "use_develop"):
        if key in conf:
            value = conf[key]
            packaging[key] = str(value) if isinstance(value, Path) else value
    if isinstance(env, RunToxEnv) and env.package_env is not None:
        packaging["package_envs"] = [package_env.name for package_env in env.package_envs]
    return packaging


def _interpreter_plan(env: VirtualEnv) -> dict[str, JsonValue]:
    candidates = env.conf.get("base_python", list[str])
    discover = [str(path) for path in getattr(env.options, "discover", [])]
    for candidate in candidates:
        info = get_interpreter(candidate, try_first_with=discover, cache=None, env=os.environ.copy())
        if info is None or info.system_executable is None:
            continue
        executable = Path(info.system_executable).resolve()
        version_info = info.version_info
        data = executable.read_bytes()
        return {
            "available": True,
            "candidate": candidate,
            "candidates": candidates,
            "executable": str(executable),
            "implementation": info.implementation,
            "version": info.version,
            "version_info": [getattr(version_info, key) for key in ("major", "minor", "micro")],
            "architecture": info.architecture,
            "machine": getattr(info, "machine", None),
            "platform": info.platform,
            "free_threaded": bool(info.free_threaded),
            "debug": bool(info.debug_build),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    return {"available": False, "candidates": candidates}


def _installpkg_plan(options: object) -> dict[str, JsonValue] | None:
    installpkg = getattr(options, "install_pkg", None)
    if installpkg is None:
        return None
    package = Path(cast("str", installpkg)).resolve()
    data = package.read_bytes()
    return {
        "path": str(package),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
